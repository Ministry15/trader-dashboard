"""Carregamento centralizado de configuração do crypto_bsc.

Lê o `.env` (segredos, via python-dotenv) e o `config/settings.yaml` (config
geral), resolvendo os placeholders ``${VAR}`` a partir do ambiente. Todos os
módulos devem obter configuração a partir daqui — nunca ler os ficheiros
directamente.
"""
from __future__ import annotations

import functools
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = BASE_DIR / ".env"
SETTINGS_PATH = BASE_DIR / "config" / "settings.yaml"

_env_loaded = False


def load_env() -> None:
    """Carrega o .env para o ambiente (idempotente)."""
    global _env_loaded
    if not _env_loaded:
        load_dotenv(ENV_PATH)
        _env_loaded = True


def get_env(key: str, default=None):
    """Devolve uma variável de ambiente, garantindo que o .env foi carregado."""
    load_env()
    return os.getenv(key, default)


def _expand(obj):
    """Resolve recursivamente ${VAR} em strings dentro de dicts/listas."""
    if isinstance(obj, str):
        return os.path.expandvars(obj)
    if isinstance(obj, dict):
        return {k: _expand(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand(v) for v in obj]
    return obj


@functools.lru_cache(maxsize=1)
def get_settings() -> dict:
    """Carrega e devolve o settings.yaml com placeholders já resolvidos.

    O resultado é cacheado; usar :func:`reload_settings` para forçar releitura.
    """
    load_env()
    with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return _expand(raw)


def reload_settings() -> dict:
    """Limpa a cache e relê o settings.yaml (útil em testes)."""
    get_settings.cache_clear()
    return get_settings()


if __name__ == "__main__":
    s = get_settings()
    print("BASE_DIR:", BASE_DIR)
    print("network.chain_id:", s["network"]["chain_id"])
    print("dexes:", list(s["dexes"].keys()))
    print("tokens:", list(s["tokens"].keys()))
    # confirmar que ${VAR} foi resolvido (sem mostrar valores sensíveis)
    rpc = s["network"]["rpc_url"]
    print("rpc_url resolvido:", rpc.startswith("http"))
