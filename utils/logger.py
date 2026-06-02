"""Logging centralizado: consola colorida (colorlog) + ficheiro rotativo.

Configura o logger raiz a partir de ``settings.yaml > logging`` (com override
de nível via ``LOG_LEVEL`` no .env). Todos os módulos usam
``logging.getLogger(__name__)`` normalmente; basta chamar
:func:`setup_logging` uma vez no arranque (ou usar :func:`get_logger`).
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

import colorlog

from utils.config import get_env, get_settings

_configured = False

_CONSOLE_FMT = "%(log_color)s%(asctime)s %(levelname)-8s%(reset)s %(cyan)s%(name)s%(reset)s: %(message)s"
_FILE_FMT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_COLORS = {
    "DEBUG": "white",
    "INFO": "green",
    "WARNING": "yellow",
    "ERROR": "red",
    "CRITICAL": "bold_red",
}


def setup_logging(force: bool = False) -> logging.Logger:
    """Configura o logger raiz (idempotente). Devolve o logger 'crypto_bsc'."""
    global _configured
    root = logging.getLogger()
    if _configured and not force:
        return logging.getLogger("crypto_bsc")

    cfg = get_settings().get("logging", {})
    level_name = (get_env("LOG_LEVEL") or cfg.get("level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)

    # limpar handlers anteriores (evita duplicados em re-configuração)
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(level)

    # --- consola colorida ---
    if cfg.get("colored_console", True):
        console = colorlog.StreamHandler()
        console.setFormatter(colorlog.ColoredFormatter(
            _CONSOLE_FMT, datefmt=_DATEFMT, log_colors=_COLORS))
    else:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(_FILE_FMT, datefmt=_DATEFMT))
    console.setLevel(level)
    root.addHandler(console)

    # --- ficheiro rotativo ---
    log_dir = Path(cfg.get("dir", "/opt/crypto_bsc/data/logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    file_path = log_dir / cfg.get("file", "crypto_bsc.log")
    file_handler = logging.handlers.RotatingFileHandler(
        file_path,
        maxBytes=int(cfg.get("rotate_max_bytes", 10 * 1024 * 1024)),
        backupCount=int(cfg.get("rotate_backups", 5)),
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(_FILE_FMT, datefmt=_DATEFMT))
    file_handler.setLevel(level)
    root.addHandler(file_handler)

    _configured = True
    logging.getLogger("crypto_bsc").debug("Logging configurado (nível=%s, ficheiro=%s)",
                                          level_name, file_path)
    return logging.getLogger("crypto_bsc")


def get_logger(name: str) -> logging.Logger:
    """Garante a configuração e devolve um logger nomeado."""
    if not _configured:
        setup_logging()
    return logging.getLogger(name)


if __name__ == "__main__":
    log = setup_logging(force=True)
    for lvl, fn in (("debug", log.debug), ("info", log.info),
                    ("warning", log.warning), ("error", log.error)):
        fn("mensagem de teste nível %s", lvl)
    child = get_logger("crypto_bsc.teste")
    child.info("logger-filho a propagar para consola + ficheiro")

    cfg = get_settings().get("logging", {})
    fpath = Path(cfg.get("dir")) / cfg.get("file")
    print("ficheiro de log existe:", fpath.exists(), "->", fpath)
    print("últimas linhas no ficheiro:", fpath.stat().st_size, "bytes")
    print("SMOKE OK")
