"""Pool de wallets para execução paralela de liquidações em Polygon."""
from __future__ import annotations

import queue
from contextlib import contextmanager

from utils.config import get_env


class WalletPool:
    """Thread-safe pool of private keys.

    Keys are loaded from POLYGON_WALLET_1, POLYGON_WALLET_2, … (env vars).
    Falls back to BSC_PRIVATE_KEY if no POLYGON_WALLET_* are set.

    Usage::

        pool = WalletPool()
        with pool.borrow() as pk:
            signed = acct.sign_transaction(tx)
    """

    def __init__(
        self,
        env_prefix: str = "POLYGON_WALLET",
        fallback: str = "BSC_PRIVATE_KEY",
    ) -> None:
        pks: list[str] = []
        i = 1
        while True:
            raw = (get_env(f"{env_prefix}_{i}") or "").strip().strip('"').strip("'")
            if not raw:
                break
            pks.append(raw[2:] if raw.startswith("0x") else raw)
            i += 1
        if not pks:
            fb = (get_env(fallback) or "").strip().strip('"').strip("'")
            if fb:
                pks.append(fb[2:] if fb.startswith("0x") else fb)
        self._pks = pks
        self._queue: queue.Queue[str] = queue.Queue()
        for pk in pks:
            self._queue.put(pk)

    @property
    def size(self) -> int:
        return len(self._pks)

    @property
    def primary_pk(self) -> str:
        """Returns the first (primary) key — for balance checks only."""
        return self._pks[0] if self._pks else ""

    @contextmanager
    def borrow(self, timeout: float = 2.0):
        """Yield a free pk; block up to *timeout* seconds if all wallets are busy."""
        pk = self._queue.get(timeout=timeout)
        try:
            yield pk
        finally:
            self._queue.put(pk)
