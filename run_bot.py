#!/usr/bin/env python3
"""Runner genérico para bots de liquidação.

Importa dinamicamente a classe indicada, instancia-a e chama tick() em loop.
Cada processo é completamente isolado — um crash não afecta os restantes.

Uso:
    python run_bot.py <module> <class> [interval_seconds]

Exemplos:
    python run_bot.py bots.aave_liquidator_bot AaveLiquidatorBot 30
    python run_bot.py bots.morpho_liquidator_base_bot MorphoLiquidatorBaseBot 30
"""
import importlib
import logging
import signal
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("run_bot")

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logger.info("run_bot: sinal %d recebido — a encerrar após tick actual", signum)
    _shutdown = True


def main() -> None:
    if len(sys.argv) < 3:
        print("Uso: run_bot.py <module> <class> [interval_seconds]", file=sys.stderr)
        sys.exit(1)

    module_name   = sys.argv[1]
    class_name    = sys.argv[2]
    interval      = int(sys.argv[3]) if len(sys.argv) > 3 else 30

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT,  _handle_signal)

    logger.info("run_bot: a importar %s.%s (interval=%ds)", module_name, class_name, interval)
    try:
        mod      = importlib.import_module(module_name)
        BotClass = getattr(mod, class_name)
        bot      = BotClass()
    except Exception as exc:
        logger.critical("run_bot: falha ao inicializar %s.%s: %s", module_name, class_name, exc)
        sys.exit(1)

    logger.info("run_bot: %s pronto — loop iniciado", class_name)

    while not _shutdown:
        try:
            bot.tick()
        except Exception as exc:
            logger.error("run_bot: excepção no tick() de %s: %s", class_name, exc, exc_info=True)
        if not _shutdown:
            time.sleep(interval)

    logger.info("run_bot: %s encerrado", class_name)


if __name__ == "__main__":
    main()
