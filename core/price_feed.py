"""Agregação de preços: DEXs on-chain (via :mod:`core.dex`) + CEX (Binance/ccxt).

Reúne as cotações de todos os DEXs uniswap_v2 para um par e calcula o spread
entre eles, permitindo detectar oportunidades de arbitragem. Opcionalmente
inclui o preço spot da Binance (a leitura de ticker público não exige API key).
"""
from __future__ import annotations

import logging
from decimal import Decimal

from core.dex import load_v2_dexes
from core.wallet import Wallet
from utils.config import get_settings

logger = logging.getLogger(__name__)

# Mapeamento de símbolos on-chain -> símbolos da CEX.
_CEX_SYMBOL_MAP = {"WBNB": "BNB"}


def _cex_symbol(token: str) -> str:
    return _CEX_SYMBOL_MAP.get(token, token)


class PriceFeed:
    """Fonte unificada de preços DEX + CEX."""

    def __init__(self, wallet: Wallet | None = None, settings: dict | None = None):
        self.settings = settings or get_settings()
        self.wallet = wallet or Wallet(self.settings)
        self.dexes = load_v2_dexes(wallet=self.wallet, settings=self.settings)
        self._cex = None  # ccxt instanciado lazy

    # ----------------------------------------------------------------------- DEX
    def get_dex_price(self, dex_name: str, base: str, quote: str) -> Decimal:
        return self.dexes[dex_name].get_price(base, quote)

    def get_dex_prices(self, base: str, quote: str) -> dict[str, Decimal]:
        """Preço de ``base`` em ``quote`` em cada DEX (None se a query falhar)."""
        prices: dict[str, Decimal | None] = {}
        for name, dex in self.dexes.items():
            try:
                prices[name] = dex.get_price(base, quote)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Preço falhou em %s (%s/%s): %s", name, base, quote, exc)
                prices[name] = None
        return prices

    def compare_dex(self, base: str, quote: str) -> dict:
        """Resumo: preços por DEX, melhor compra/venda e spread em bps."""
        prices = {k: v for k, v in self.get_dex_prices(base, quote).items() if v is not None}
        if len(prices) < 2:
            return {"pair": f"{base}/{quote}", "prices": prices, "spread_bps": None}
        cheapest = min(prices, key=prices.get)   # onde 1 base custa menos quote -> comprar base
        dearest = max(prices, key=prices.get)    # onde 1 base vale mais quote   -> vender base
        lo, hi = prices[cheapest], prices[dearest]
        spread_bps = (hi - lo) / lo * Decimal(10_000)
        return {
            "pair": f"{base}/{quote}",
            "prices": prices,
            "buy_on": cheapest, "buy_price": lo,
            "sell_on": dearest, "sell_price": hi,
            "spread_bps": spread_bps,
        }

    # ----------------------------------------------------------------------- CEX
    @property
    def cex(self):
        if self._cex is None:
            import ccxt
            ex_cfg = self.settings.get("exchanges", {}).get("binance", {})
            params = {"enableRateLimit": bool(ex_cfg.get("rate_limit", True))}
            key, sec = ex_cfg.get("api_key", ""), ex_cfg.get("api_secret", "")
            if key and "YOUR_" not in key:
                params["apiKey"] = key
            if sec and "YOUR_" not in sec:
                params["secret"] = sec
            self._cex = ccxt.binance(params)
        return self._cex

    def get_cex_price(self, base: str, quote: str = "USDT") -> Decimal | None:
        """Preço spot 'last' na Binance (ticker público). None se indisponível."""
        symbol = f"{_cex_symbol(base)}/{_cex_symbol(quote)}"
        try:
            ticker = self.cex.fetch_ticker(symbol)
            return Decimal(str(ticker["last"]))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ticker CEX falhou (%s): %s", symbol, exc)
            return None

    # -------------------------------------------------------------------- combo
    def snapshot(self, base: str, quote: str) -> dict:
        """Visão combinada DEX + CEX para um par."""
        out = self.compare_dex(base, quote)
        out["cex_price"] = self.get_cex_price(base, quote)
        return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    pf = PriceFeed()
    snap = pf.snapshot("WBNB", "USDT")
    print("Par:", snap["pair"])
    for dex, price in snap["prices"].items():
        print(f"  {dex:16s} {price:.4f}")
    if snap["spread_bps"] is not None:
        print(f"  comprar em {snap['buy_on']} @ {snap['buy_price']:.4f} | "
              f"vender em {snap['sell_on']} @ {snap['sell_price']:.4f} | "
              f"spread {snap['spread_bps']:.1f} bps")
    print("  CEX (Binance) BNB/USDT:", snap["cex_price"])
    print("SMOKE OK")
