"""Descoberta automática de novos tokens BSC.

Fontes:
  - GeckoTerminal (API pública, sem chave) — pools BSC ordenados por volume 24h,
    filtrados por idade e liquidez
  - GoPlus Security (API pública, sem chave) — honeypot / rug pull flags

Nota: usamos /pools?sort=h24_volume_usd_desc em vez de /new_pools porque o
endpoint new_pools só devolve pools criados nos últimos minutos, que por
definição ainda não têm volume suficiente para passar os filtros.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import requests

from utils.logger import get_logger

logger = get_logger(__name__)

_GECKO_POOLS_URL = "https://api.geckoterminal.com/api/v2/networks/bsc/pools"
_GOPLUS_URL = "https://api.gopluslabs.io/api/v1/token_security/56"
_WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
# USDT e USDC também são quotes válidas em pools BSC com liquidez real
_STABLE_QUOTES = {
    "0x55d398326f99059ff775485246999027b3197955",  # USDT BSC
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",  # USDC BSC
}
# Mapa símbolo → endereço para resolver quote_symbol passado pelo sniper
_SYMBOL_TO_ADDR: dict[str, str] = {
    "WBNB": _WBNB,
    "BNB":  _WBNB,
    "USDT": "0x55d398326f99059ff775485246999027b3197955",
    "USDC": "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",
    "BUSD": "0xe9e7cea3dedca5984780bafc599bd69add087d56",
}
_TIMEOUT = 12


def _gecko_pools_page(page: int = 1) -> list[dict]:
    for attempt in range(3):
        try:
            r = requests.get(
                _GECKO_POOLS_URL,
                params={"page": page, "sort": "h24_volume_usd_desc"},
                headers={"Accept": "application/json"},
                timeout=_TIMEOUT,
            )
            if r.status_code == 429:
                wait = 2 ** attempt * 3
                logger.warning("GeckoTerminal rate limit (page=%d) — aguardar %ds", page, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json().get("data", [])
        except requests.HTTPError:
            raise
        except Exception:
            logger.exception("GeckoTerminal fetch falhou (page=%d, attempt=%d)", page, attempt + 1)
            if attempt < 2:
                time.sleep(2 ** attempt)
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


def _is_safe(sec: dict, require_data: bool = False) -> bool:
    """True se o token passa nos checks de segurança GoPlus.

    Se ``require_data=False`` (default) tokens sem dados GoPlus passam com
    aviso — tokens muito novos ainda não estão indexados. Se ``True``, a
    ausência de dados é tratada como rejeição.
    """
    if not sec:
        return not require_data  # sem dados: passa (com aviso) ou rejeita
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
        min_volume_usd          — volume 24h mínimo (default 100 000)
        min_liquidity_usd       — liquidez mínima (default 50 000)
        max_age_days            — idade máxima do pool em dias (default 7)
        safety_check            — activar GoPlus (default True)
        safety_require_goplus   — rejeitar tokens sem dados GoPlus (default False:
                                   tokens muito novos ainda não são indexados)
        blacklist               — endereços a ignorar sempre
        max_tokens              — máx. tokens a devolver (default 10)

    Devolve lista de endereços de contratos (lowercase).
    """
    min_vol = float(cfg.get("min_volume_usd", 100_000))
    min_liq = float(cfg.get("min_liquidity_usd", 50_000))
    max_age_days = int(cfg.get("max_age_days", 7))
    safety_check = bool(cfg.get("safety_check", True))
    require_goplus = bool(cfg.get("safety_require_goplus", False))
    blacklist = {a.lower() for a in cfg.get("blacklist", [])}
    max_tokens = int(cfg.get("max_tokens", 10))

    # Se o sniper passou o seu quote_symbol, só aceitar pools com esse quote
    quote_sym = cfg.get("quote_symbol", "").upper()
    if quote_sym and quote_sym in _SYMBOL_TO_ADDR:
        _valid_quotes: set[str] = {_SYMBOL_TO_ADDR[quote_sym]}
        logger.debug("Scanner: filtrar por quote=%s (%s)", quote_sym, _SYMBOL_TO_ADDR[quote_sym])
    else:
        _valid_quotes = {_WBNB} | _STABLE_QUOTES

    now = datetime.now(timezone.utc)
    candidates: list[str] = []

    for page in range(1, 6):  # até 100 pools por scan (5 páginas × 20)
        pools = _gecko_pools_page(page)
        if not pools:
            break

        page_youngest_age: list[int] = []
        for pool in pools:
            attr = pool.get("attributes", {})
            rels = pool.get("relationships", {})

            # Extrair endereços base/quote
            base_id = rels.get("base_token", {}).get("data", {}).get("id", "")
            quote_id = rels.get("quote_token", {}).get("data", {}).get("id", "")
            base_addr = base_id.split("_", 1)[-1].lower() if "_" in base_id else ""
            quote_addr = quote_id.split("_", 1)[-1].lower() if "_" in quote_id else ""

            # Aceitar pools com WBNB ou stablecoin como contraparte
            if quote_addr in _valid_quotes:
                token_addr = base_addr
            elif base_addr in _valid_quotes:
                token_addr = quote_addr
            else:
                continue

            if not token_addr or token_addr in blacklist or token_addr in candidates:
                continue

            # Idade do pool — tracking para early exit
            created_str = attr.get("pool_created_at", "")
            age_days = None
            if created_str:
                try:
                    created_at = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                    age_days = (now - created_at).days
                    page_youngest_age.append(age_days)
                    if age_days > max_age_days:
                        continue
                except ValueError:
                    pass

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

            candidates.append(token_addr)
            if len(candidates) >= max_tokens * 4:
                break

        if len(candidates) >= max_tokens * 4:
            break
        # Se todos os pools da página são mais velhos que max_age_days, parar
        if page_youngest_age and min(page_youngest_age) > max_age_days:
            break
        time.sleep(1.5)  # evitar rate limit entre páginas

    logger.info(
        "Scanner: %d candidatos (vol≥$%.0fk, liq≥$%.0fk, idade≤%dd)",
        len(candidates), min_vol / 1000, min_liq / 1000, max_age_days,
    )

    if not candidates:
        return []

    # GoPlus safety check
    if safety_check:
        sec_map = _goplus_check(candidates)
        unverified = [a for a in candidates if not sec_map.get(a)]
        if unverified and not require_goplus:
            logger.warning(
                "Scanner: %d token(s) sem dados GoPlus (ainda não indexados) — "
                "aceites com aviso. Activar safety_require_goplus: true para rejeitar.",
                len(unverified),
            )
        safe = [a for a in candidates if _is_safe(sec_map.get(a, {}), require_data=require_goplus)]
        rejected = len(candidates) - len(safe)
        if rejected:
            logger.info("Scanner: %d rejeitados por flags de segurança (honeypot/tax/rug/sem_dados)", rejected)
        candidates = safe

    result = candidates[:max_tokens]
    if result:
        logger.info("Scanner: %d tokens aprovados → %s", len(result), result)
    else:
        logger.info("Scanner: nenhum token passou todos os filtros")
    return result
