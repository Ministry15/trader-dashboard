"""Orquestrador do crypto_bsc — corre vários bots em conjunto.

Cada bot corre na sua própria thread, executando um "passo" (uma iteração da
sua estratégia) em loop, com o intervalo definido na configuração. O
orquestrador trata do arranque, do encerramento limpo (SIGTERM/SIGINT) e de
isolar falhas: o erro de um bot não derruba os outros nem o processo.

Seleção de bots (por ordem de prioridade):
  1. argumentos da linha de comandos:  ``python main.py arbitrage dca``
  2. variável de ambiente:             ``ENABLED_BOTS=arbitrage,grid``
  3. settings.yaml:                    ``orchestrator.enabled_bots``

Flags:
  --once     executa um único passo de cada bot e termina (validação)
  --list     lista os bots disponíveis/seleccionados e sai
"""
from __future__ import annotations

import signal
import sys
import threading
import time

from bots.arbitrage_bot import ArbitrageBot
from bots.dca_bot import DCABot
from bots.funding_rate_bot import FundingRateBot
from bots.grid_bot import GridBot
from bots.pepe_grid_bot import PepeGridBot
from bots.sniper_bot import SniperBot
from bots.solana_grid_bot import SolanaGridBot
from bots.solana_sniper_bot import SolanaSniperBot
from utils.config import get_env, get_settings
from utils.logger import setup_logging
from utils.notifier import TelegramNotifier

logger = setup_logging()

# Registo: nome -> (factory, método-passo, função que extrai o intervalo do settings)
BOT_REGISTRY = {
    "arbitrage": (ArbitrageBot, "run_once",
                  lambda s: int(s.get("scheduler", {}).get("scan_interval_seconds", 5))),
    "grid":      (GridBot, "tick",
                  lambda s: int(s["bots"]["grid"].get("poll_seconds", 10))),
    "pepe_grid": (PepeGridBot, "tick",
                  lambda s: int(s["bots"]["pepe_grid"].get("poll_seconds", 10))),
    "dca":       (DCABot, "buy_once",
                  lambda s: int(s["bots"]["dca"].get("interval_seconds", 86400))),
    "sniper":    (SniperBot, "tick",
                  lambda s: int(s["bots"]["sniper"].get("poll_seconds", 5))),
    "funding_rate": (FundingRateBot, "tick",
                     lambda s: int(s.get("bots", {}).get("funding_rate", {}).get("poll_seconds", 30))),
    "solana_grid": (SolanaGridBot, "tick",
                    lambda s: int(s["bots"]["solana_grid"].get("poll_seconds", 10))),
    "solana_sniper": (SolanaSniperBot, "tick",
                      lambda s: int(s.get("bots", {}).get("solana_sniper", {}).get("poll_seconds", 15))),
}


def select_bots(argv: list[str], settings: dict) -> list[str]:
    """Resolve a lista de bots a correr segundo a prioridade documentada."""
    cli = [a for a in argv if not a.startswith("-")]
    if cli:
        chosen = cli
    elif get_env("ENABLED_BOTS"):
        chosen = [b.strip() for b in get_env("ENABLED_BOTS").split(",") if b.strip()]
    else:
        chosen = list(settings.get("orchestrator", {}).get("enabled_bots", []))

    invalid = [b for b in chosen if b not in BOT_REGISTRY]
    if invalid:
        raise SystemExit(f"Bots desconhecidos: {invalid}. Disponíveis: {list(BOT_REGISTRY)}")
    return chosen


class Orchestrator:
    def __init__(self, bot_names: list[str], settings: dict | None = None):
        self.settings = settings or get_settings()
        self.bot_names = bot_names
        self.stop_event = threading.Event()
        self.threads: list[threading.Thread] = []
        self.notifier = TelegramNotifier(self.settings)
        self._instances: dict[str, object] = {}

    # ------------------------------------------------------------ instâncias
    def _instance(self, name: str):
        """Instancia um bot a pedido (lazy — abre ligação RPC na criação)."""
        if name not in self._instances:
            factory, _, _ = BOT_REGISTRY[name]
            logger.info("A inicializar bot '%s'...", name)
            self._instances[name] = factory(self.settings)
        return self._instances[name]

    # ----------------------------------------------------------------- loops
    def _bot_loop(self, name: str) -> None:
        factory, step_name, interval_fn = BOT_REGISTRY[name]
        interval = interval_fn(self.settings)
        try:
            bot = self._instance(name)
        except Exception:  # noqa: BLE001 - falha de arranque não derruba os outros
            logger.exception("Falha ao inicializar '%s' — bot não arranca.", name)
            return
        step = getattr(bot, step_name)
        logger.info("Bot '%s' a correr (passo=%s, intervalo=%ss).", name, step_name, interval)
        while not self.stop_event.is_set():
            try:
                step()
            except Exception:  # noqa: BLE001 - isola falhas por iteração
                logger.exception("Erro na iteração do bot '%s'.", name)
            # espera interrompível: acorda imediatamente no shutdown
            self.stop_event.wait(interval)
        logger.info("Bot '%s' terminado.", name)

    # ------------------------------------------------------------- controlo
    def run_once(self) -> dict:
        """Executa um único passo de cada bot (sequencial) e devolve resultados."""
        results = {}
        for name in self.bot_names:
            _, step_name, _ = BOT_REGISTRY[name]
            try:
                bot = self._instance(name)
                out = getattr(bot, step_name)()
                results[name] = {"ok": True, "result": out}
                logger.info("[once] '%s' executado.", name)
            except Exception as exc:  # noqa: BLE001
                results[name] = {"ok": False, "error": str(exc)}
                logger.exception("[once] erro em '%s'.", name)
        return results

    def start(self) -> None:
        logger.info("Orquestrador a arrancar bots: %s", self.bot_names)
        self.notifier.notify("daily_summary",
                             f"crypto_bsc arrancou. Bots: {', '.join(self.bot_names)}")
        for name in self.bot_names:
            t = threading.Thread(target=self._bot_loop, args=(name,), name=f"bot:{name}", daemon=True)
            t.start()
            self.threads.append(t)

    def shutdown(self, *_args) -> None:
        if self.stop_event.is_set():
            return
        logger.info("Sinal de paragem recebido — a encerrar bots...")
        self.stop_event.set()

    def wait(self) -> None:
        """Bloqueia até receber sinal de paragem; depois junta as threads."""
        try:
            while not self.stop_event.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            self.shutdown()
        for t in self.threads:
            t.join(timeout=30)
        logger.info("Orquestrador encerrado.")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    settings = get_settings()
    bot_names = select_bots(argv, settings)

    if "--list" in argv:
        print("Disponíveis:", list(BOT_REGISTRY))
        print("Seleccionados:", bot_names)
        return 0

    orch = Orchestrator(bot_names, settings)

    if "--once" in argv:
        logger.info("Modo --once: um passo por bot.")
        results = orch.run_once()
        for name, r in results.items():
            print(f"  {name:10s} ok={r['ok']}" + ("" if r["ok"] else f"  erro={r['error']}"))
        return 0 if all(r["ok"] for r in results.values()) else 1

    # modo serviço: regista sinais e corre até SIGTERM/SIGINT
    signal.signal(signal.SIGTERM, orch.shutdown)
    signal.signal(signal.SIGINT, orch.shutdown)
    orch.start()
    orch.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
