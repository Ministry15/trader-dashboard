"""Descoberta automática de novos tokens BSC.

Fontes:
  - GeckoTerminal (API pública, sem chave) — novos pools BSC com volume/liquidez/idade
  - GoPlus Security (API pública, sem chave) — honeypot / rug pull flags
"""
from __future__ import annotations

from datetime import datetime, timezone

import requests

from utils.logger import get_logger

logger = get_logger(__name__)

_GECKO_URL = "https://api.geckoterminal.com/api/v2/networks/bsc/new_pools"
_GOPLUS_URL = "https://api.gopluslabs.io/api/v1/token_security/56"
_WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
_TIMEOUT = 12


def _gecko_new_pools(page: int = 1) -> list[dict]:
    try:
        r = requests.get(
            _GECKO_URL,
            params={"page": page},
            headers={"Accept": "application/json"},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception:
        logger.exception("GeckoTerminal fetch falhou (page=%d)", page)
        return []


def _goplus_check(addresses: list[str]) -> dict[str, dict]:
    """Devolve mapa address→resultado de segurança para até 50 tokens (GoPlus)."""
    if not addresses:
        return {}
    try:
        r = requests.get(
            _GOPLUS_URL,
            params={"contract_addresses": ",".join(addresses[:50])},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        result = r.json().get("result", {})
        return {k.lower(): v for k, v in result.items()}
    except Exception:
        logger.exception("GoPlus check falhou")
        return {}


def _is_safe(sec: dict) -> bool:
    """True se o token passa nos checks de segurança GoPlus."""
    if not sec:
        return False  # sem dados da API → rejeita por precaução
    if sec.get("is_honeypot") == "1":
        return False
    if sec.get("is_open_source") == "0":
        return False
    try:
        if float(sec.get("buy_tax") or 0) > 0.10:
            return False
        if float(sec.get("sell_tax") or 0) > 0.10:
            return False
    except (ValueError, TypeError):
        pass
    try:
        if float(sec.get("owner_percent") or 0) > 0.20:
            return False
        if float(sec.get("creator_percent") or 0) > 0.20:
            return False
    except (ValueError, TypeError):
        pass
    return True


def scan(cfg: dict) -> list[str]:
    """Descobre tokens BSC que passam todos os filtros configurados.

    Parâmetros lidos de cfg (bots.sniper.auto_discovery):
        min_volume_usd      — volume 24h mínimo (default 100 000)
        min_liquidity_usd   — liquidez mínima (default 50 000)
        max_age_days        — idade máxima do pool em dias (default 7)
        safety_check        — activar GoPlus (default True)
        blacklist           — endereços a ignorar sempre
        max_tokens          — máx. tokens a devolver (default 10)

    Devolve lista de endereços de contratos (lowercase).
    """
    min_vol = float(cfg.get("min_volume_usd", 100_000))
    min_liq = float(cfg.get("min_liquidity_usd", 50_000))
    max_age_days = int(cfg.get("max_age_days", 7))
    safety_check = bool(cfg.get("safety_check", True))
    blacklist = {a.lower() for a in cfg.get("blacklist", [])}
    max_tokens = int(cfg.get("max_tokens", 10))

    now = datetime.now(timezone.utc)
    candidates: list[str] = []

    for page in (1, 2):  # até ~40 pools por scan
        pools = _gecko_new_pools(page)
        for pool in pools:
            attr = pool.get("attributes", {})
            rels = pool.get("relationships", {})

            # Extrair endereços base/quote
            base_id = rels.get("base_token", {}).get("data", {}).get("id", "")
            quote_id = rels.get("quote_token", {}).get("data", {}).get("id", "")
            base_addr = base_id.split("_", 1)[-1].lower() if "_" in base_id else ""
            quote_addr = quote_id.split("_", 1)[-1].lower() if "_" in quote_id else ""

            # Só pools com WBNB como contraparte
            if quote_addr == _WBNB:
                token_addr = base_addr
            elif base_addr == _WBNB:
                token_addr = quote_addr
            else:
                continue

            if not token_addr or token_addr in blacklist or token_addr in candidates:
                continue

            # Volume 24h
            try:
                vol = float((attr.get("volume_usd") or {}).get("h24") or 0)
            except (ValueError, TypeError):
                vol = 0.0
            if vol < min_vol:
                continue

            # Liquidez (TVL)
            try:
                liq = float(attr.get("reserve_in_usd") or 0)
            except (ValueError, TypeError):
                liq = 0.0
            if liq < min_liq:
                continue

            # Idade do pool
            created_str = attr.get("pool_created_at", "")
            if created_str:
                try:
                    created_at = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                    if (now - created_at).days > max_age_days:
                        continue
                except ValueError:
                    pass

            candidates.append(token_addr)
            if len(candidates) >= max_tokens * 4:
                break

        if len(candidates) >= max_tokens * 4:
            break

    logger.info(
        "Scanner: %d candidatos (vol≥$%.0fk, liq≥$%.0fk, idade≤%dd)",
        len(candidates), min_vol / 1000, min_liq / 1000, max_age_days,
    )

    if not candidates:
        return []

    # GoPlus safety check
    if safety_check:
        sec_map = _goplus_check(candidates)
        safe = [a for a in candidates if _is_safe(sec_map.get(a, {}))]
        rejected = len(candidates) - len(safe)
        if rejected:
            logger.info("Scanner: %d rejeitados por flags de segurança (honeypot/tax/rug)", rejected)
        candidates = safe

    result = candidates[:max_tokens]
    if result:
        logger.info("Scanner: %d tokens aprovados → %s", len(result), result)
    else:
        logger.info("Scanner: nenhum token passou todos os filtros")
    return result
