"""Notificações via Telegram (síncronas, sobre a Bot API com requests).

Lê token/chat de ``settings.yaml > notifications.telegram`` (já resolvidos a
partir do .env). Optou-se por chamadas HTTP síncronas à Bot API em vez do
cliente assíncrono do python-telegram-bot, por ser mais simples e robusto de
usar a partir do código síncrono dos bots.

Filtra por tipo de evento conforme ``notify_on``. Falhas de rede nunca
rebentam o chamador — são registadas e devolvem ``False``.
"""
from __future__ import annotations

import logging

import requests

from utils.config import get_settings

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/{method}"


def _is_placeholder(v: str | None) -> bool:
    return not v or "YOUR_" in v or "_HERE" in v


class TelegramNotifier:
    """Envia mensagens para um chat de Telegram."""

    def __init__(self, settings: dict | None = None, timeout: int = 10):
        settings = settings or get_settings()
        tg = settings.get("notifications", {}).get("telegram", {})
        self.token = tg.get("bot_token")
        self.chat_id = tg.get("chat_id")
        self.notify_on = set(tg.get("notify_on", []))
        self.timeout = timeout
        # activo só se configurado e sem placeholders
        self.enabled = bool(tg.get("enabled", False)) and not (
            _is_placeholder(self.token) or _is_placeholder(self.chat_id)
        )
        if not self.enabled:
            logger.info("TelegramNotifier inactivo (desactivado ou sem credenciais).")

    # ------------------------------------------------------------------ baixo nível
    def _call(self, method: str, payload: dict) -> dict | None:
        url = _API.format(token=self.token, method=method)
        try:
            resp = requests.post(url, json=payload, timeout=self.timeout)
            data = resp.json()
            if not data.get("ok"):
                logger.warning("Telegram %s falhou: %s", method, data.get("description"))
                return None
            return data["result"]
        except Exception as exc:  # noqa: BLE001 - nunca propagar erro de rede
            logger.warning("Erro a chamar Telegram %s: %s", method, exc)
            return None

    # ------------------------------------------------------------------ alto nível
    def verify(self) -> dict | None:
        """Valida o token via getMe (não envia mensagem a ninguém)."""
        if _is_placeholder(self.token):
            logger.warning("Token Telegram em falta/placeholder.")
            return None
        return self._call("getMe", {})

    def send(self, text: str, parse_mode: str = "HTML",
             disable_preview: bool = True) -> bool:
        """Envia uma mensagem para o chat configurado."""
        if not self.enabled:
            logger.debug("send() ignorado: notifier inactivo.")
            return False
        result = self._call("sendMessage", {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_preview,
        })
        return result is not None

    def notify(self, event_type: str, text: str) -> bool:
        """Envia apenas se ``event_type`` constar de ``notify_on``."""
        if self.notify_on and event_type not in self.notify_on:
            logger.debug("Evento '%s' não está em notify_on; ignorado.", event_type)
            return False
        prefix = {
            "trade_executed": "✅",
            "error": "⛔",
            "daily_summary": "📊",
        }.get(event_type, "ℹ️")
        return self.send(f"{prefix} <b>{event_type}</b>\n{text}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    n = TelegramNotifier()
    print("enabled:", n.enabled, "| notify_on:", sorted(n.notify_on))
    info = n.verify()
    if info:
        # mostra só nome do bot (público), nunca o token
        print(f"getMe OK -> bot @{info.get('username')} (id={info.get('id')})")
    else:
        print("getMe falhou (token inválido ou sem rede).")
    print("NOTA: nenhum sendMessage foi disparado neste smoke test.")
    print("SMOKE OK")
