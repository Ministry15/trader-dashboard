"""Bot de arbitragem entre DEXs uniswap_v2 da BSC.

Para cada par ``base/quote`` e cada par de DEXs configurado em
``settings.yaml > trading.arbitrage.dex_pairs_to_scan``, simula o ciclo
completo com o tamanho real do trade — comprar ``base`` no DEX mais barato e
revendê-lo no mais caro — usando ``getAmountsOut`` nas duas pernas. Isto
incorpora o **price impact** real e as fees do DEX; ao resultado é ainda
descontado o custo de **gás**. Só oportunidades com lucro líquido positivo e
que passem o :class:`core.risk_manager.RiskManager` são consideradas.

A execução respeita sempre o modo dry-run da :class:`core.wallet.Wallet`:
por omissão nada é enviado para a rede.

Pressuposto: os tokens ``quote`` são stablecoins ~1 USD, pelo que o lucro
denominado em ``quote`` é tratado como USD.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from core.price_feed import PriceFeed
from core.risk_manager import RiskManager
from core.wallet import Wallet
from utils.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class Opportunity:
    """Uma oportunidade de arbitragem avaliada (ainda não executada)."""
    base: str
    quote: str
    buy_on: str
    sell_on: str
    size_quote: Decimal       # capital empregue, em quote (~USD)
    base_amount: Decimal      # base comprado na 1ª perna
    gross_quote: Decimal      # lucro bruto (sem gás), em quote
    gas_cost_quote: Decimal   # custo estimado de gás, em quote
    net_quote: Decimal        # lucro líquido, em quote (~USD)
    net_bps: Decimal          # lucro líquido em bps sobre o tamanho

    def summary(self) -> str:
        return (f"{self.base}/{self.quote}: comprar @ {self.buy_on} -> vender @ {self.sell_on} | "
                f"size={self.size_quote:.2f} net={self.net_quote:+.4f} {self.quote} "
                f"({self.net_bps:+.1f} bps, gás≈{self.gas_cost_quote:.4f})")


class ArbitrageBot:
    """Scanner + executor de arbitragem inter-DEX."""

    def __init__(self, settings: dict | None = None):
        self.settings = settings or get_settings()
        self.wallet = Wallet(self.settings)
        self.price_feed = PriceFeed(wallet=self.wallet, settings=self.settings)
        self.dexes = self.price_feed.dexes
        self.risk = RiskManager(settings=self.settings)

        t = self.settings["trading"]
        self.base = t.get("base_token", "WBNB")
        self.quotes = t.get("quote_tokens", ["USDT"])
        arb = t.get("arbitrage", {})
        self.dex_pairs = [tuple(p) for p in arb.get("dex_pairs_to_scan", [])]
        # tamanho de teste = maior trade permitido pelo risco
        self.size_quote = Decimal(str(arb.get("max_trade_size_usd", 100)))
        self.gas = t.get("gas", {})

    # ----------------------------------------------------------------- gás
    def _gas_cost_in_quote(self, quote: str, ref_dex: str) -> Decimal:
        """Custo estimado das DUAS pernas de swap, convertido para ``quote``."""
        gas_units = Decimal(int(self.gas.get("gas_limit", 350000))) * 2  # 2 swaps
        gas_price_gwei = Decimal(str(self.gas.get("max_gas_price_gwei", 5)))
        cost_bnb = gas_units * gas_price_gwei / Decimal(10) ** 9
        try:
            bnb_in_quote = self.dexes[ref_dex].get_price("WBNB", quote)
        except Exception:  # noqa: BLE001 - fallback para qualquer DEX disponível
            bnb_in_quote = next(iter(self.dexes.values())).get_price("WBNB", quote)
        return cost_bnb * bnb_in_quote

    # ----------------------------------------------------------- avaliação
    def evaluate(self, base: str, quote: str, buy_on: str, sell_on: str,
                 size_quote: Decimal) -> Opportunity | None:
        """Simula comprar ``base`` em ``buy_on`` e revendê-lo em ``sell_on``."""
        try:
            # perna 1: quote -> base no DEX de compra
            base_amount = self.dexes[buy_on].quote(quote, base, size_quote)
            if base_amount <= 0:
                return None
            # perna 2: base -> quote no DEX de venda
            quote_back = self.dexes[sell_on].quote(base, quote, base_amount)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Avaliação falhou (%s->%s, %s/%s): %s",
                           buy_on, sell_on, base, quote, exc)
            return None

        gross = quote_back - size_quote
        gas_cost = self._gas_cost_in_quote(quote, sell_on)
        net = gross - gas_cost
        net_bps = (net / size_quote) * Decimal(10_000) if size_quote else Decimal(0)
        return Opportunity(base, quote, buy_on, sell_on, size_quote,
                           base_amount, gross, gas_cost, net, net_bps)

    def scan_once(self) -> list[Opportunity]:
        """Avalia todas as combinações e devolve as oportunidades lucrativas, ordenadas."""
        found: list[Opportunity] = []
        for quote in self.quotes:
            for a, b in self.dex_pairs:
                if a not in self.dexes or b not in self.dexes:
                    continue
                for buy_on, sell_on in ((a, b), (b, a)):
                    opp = self.evaluate(self.base, quote, buy_on, sell_on, self.size_quote)
                    if opp and opp.net_quote > 0:
                        found.append(opp)
        found.sort(key=lambda o: o.net_quote, reverse=True)
        return found

    # ------------------------------------------------------------- execução
    def execute(self, opp: Opportunity) -> dict:
        """Constrói e (em modo real) envia as duas pernas. Dry-run-safe.

        Antes de cada swap garante a allowance do token de entrada ao router
        respectivo (ERC-20 ``approve``), também gated por dry-run.
        """
        buy_router = self.dexes[opp.buy_on].cfg["router"]
        a1 = self.wallet.ensure_allowance(opp.quote, buy_router, opp.size_quote)
        buy_tx = self.dexes[opp.buy_on].build_swap_tx(opp.quote, opp.base, opp.size_quote)
        r1 = self.wallet.send_transaction(buy_tx)

        sell_router = self.dexes[opp.sell_on].cfg["router"]
        a2 = self.wallet.ensure_allowance(opp.base, sell_router, opp.base_amount)
        sell_tx = self.dexes[opp.sell_on].build_swap_tx(opp.base, opp.quote, opp.base_amount)
        r2 = self.wallet.send_transaction(sell_tx)
        return {"approve_buy": a1, "buy": r1, "approve_sell": a2, "sell": r2}

    def act_on_best(self, opportunities: list[Opportunity]) -> dict | None:
        """Aplica o gate de risco à melhor oportunidade e, se passar, executa."""
        if not opportunities:
            logger.info("Sem oportunidades lucrativas nesta passagem.")
            return None
        best = opportunities[0]
        decision = self.risk.check_trade(
            trade_size_usd=best.size_quote,
            expected_profit_usd=best.net_quote,
            profit_bps=best.net_bps,
        )
        logger.info("Melhor: %s", best.summary())
        if not decision.allowed:
            logger.info("Risk manager recusou: %s", decision.reason)
            return {"opportunity": best, "executed": False, "reason": decision.reason}

        self.risk.register_open()
        result = self.execute(best)
        # Nota: em produção, o PnL real deve vir do recibo das txs; aqui usamos
        # a estimativa apenas para fechar o ciclo de contabilidade do risco.
        self.risk.register_close(best.net_quote)
        return {"opportunity": best, "executed": True,
                "dry_run": self.wallet.dry_run, "result": result}

    def run_once(self) -> dict | None:
        return self.act_on_best(self.scan_once())

    def run_forever(self) -> None:
        interval = int(self.settings.get("scheduler", {}).get("scan_interval_seconds", 5))
        logger.info("ArbitrageBot a correr (intervalo=%ss, dry_run=%s). Ctrl+C para parar.",
                    interval, self.wallet.dry_run)
        while True:
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - nunca deixar o loop morrer
                logger.exception("Erro na passagem de scan: %s", exc)
            time.sleep(interval)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    bot = ArbitrageBot()
    print(f"base={bot.base} quotes={bot.quotes} size={bot.size_quote} "
          f"pares={bot.dex_pairs} dry_run={bot.wallet.dry_run}")
    opps = bot.scan_once()
    print(f"Oportunidades lucrativas (líquidas) encontradas: {len(opps)}")
    for o in opps[:10]:
        print("  ", o.summary())
    if not opps:
        # mostrar também a melhor avaliação bruta para diagnóstico
        print("  (nenhuma passou o custo de gás — scan apenas informativo)")
    outcome = bot.run_once()
    print("Resultado run_once:", "nenhum" if outcome is None else
          {k: outcome[k] for k in outcome if k != "result"})
    print("SMOKE OK")
