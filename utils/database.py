"""Persistência via SQLAlchemy 2.0.

Guarda o histórico de trades/oportunidades e snapshots de preço numa base de
dados definida por ``DATABASE_URL`` (.env) ou ``settings.database.url``. Por
omissão é um SQLite em ``data/crypto_bsc.db``.

Uso típico::

    from utils.database import init_db, record_trade
    init_db()
    record_trade(bot="arbitrage", base="WBNB", quote="USDT",
                 size_usd=500, profit_usd=3.2, dry_run=True, status="simulated")
"""
from __future__ import annotations

import datetime
import logging

from sqlalchemy import (Boolean, DateTime, Float, Integer, String, create_engine,
                        func, select)
from sqlalchemy.orm import (DeclarativeBase, Mapped, Session, mapped_column,
                            sessionmaker)

from utils.config import get_env, get_settings

logger = logging.getLogger(__name__)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class Base(DeclarativeBase):
    pass


class Trade(Base):
    """Um trade executado (ou simulado em dry-run)."""
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    bot: Mapped[str] = mapped_column(String(32), index=True)
    base: Mapped[str] = mapped_column(String(16))
    quote: Mapped[str] = mapped_column(String(16))
    dex_buy: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dex_sell: Mapped[str | None] = mapped_column(String(32), nullable=True)
    size_usd: Mapped[float] = mapped_column(Float, default=0.0)
    profit_usd: Mapped[float] = mapped_column(Float, default=0.0)
    profit_bps: Mapped[float] = mapped_column(Float, default=0.0)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(24), default="simulated")
    tx_buy: Mapped[str | None] = mapped_column(String(80), nullable=True)
    tx_sell: Mapped[str | None] = mapped_column(String(80), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<Trade {self.id} {self.bot} {self.base}/{self.quote} "
                f"{self.profit_usd:+.4f}USD dry={self.dry_run} {self.status}>")


class PriceSnapshot(Base):
    """Registo pontual de um preço observado (DEX ou CEX)."""
    __tablename__ = "price_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime.datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)   # ex.: pancakeswap_v2, binance
    pair: Mapped[str] = mapped_column(String(24), index=True)     # ex.: WBNB/USDT
    price: Mapped[float] = mapped_column(Float)


# --- engine / sessão (lazy singletons) ---------------------------------------
_engine = None
_SessionFactory: sessionmaker | None = None


def _db_url() -> str:
    url = get_env("DATABASE_URL")
    if url:
        return url
    return get_settings().get("database", {}).get("url", "sqlite:////opt/crypto_bsc/data/crypto_bsc.db")


def get_engine():
    global _engine
    if _engine is None:
        url = _db_url()
        echo = bool(get_settings().get("database", {}).get("echo", False))
        # check_same_thread só faz sentido em sqlite
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, echo=echo, future=True, connect_args=connect_args)
        logger.info("Engine de base de dados: %s", url)
    return _engine


def get_session() -> Session:
    """Devolve uma nova sessão (o chamador é responsável por fechar/commit)."""
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), future=True, expire_on_commit=False)
    return _SessionFactory()


def init_db() -> None:
    """Cria as tabelas se ainda não existirem."""
    Base.metadata.create_all(get_engine())
    logger.info("Tabelas garantidas: %s", ", ".join(Base.metadata.tables))


# --- helpers de alto nível ----------------------------------------------------
def record_trade(**kwargs) -> int:
    """Insere um Trade e devolve o id."""
    with get_session() as s:
        trade = Trade(**kwargs)
        s.add(trade)
        s.commit()
        return trade.id


def record_price(source: str, pair: str, price: float) -> int:
    with get_session() as s:
        snap = PriceSnapshot(source=source, pair=pair, price=float(price))
        s.add(snap)
        s.commit()
        return snap.id


def count_trades() -> int:
    with get_session() as s:
        return s.scalar(select(func.count()).select_from(Trade))


def recent_trades(limit: int = 10) -> list[Trade]:
    with get_session() as s:
        return list(s.scalars(select(Trade).order_by(Trade.ts.desc()).limit(limit)))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    init_db()
    before = count_trades()
    tid = record_trade(bot="arbitrage", base="WBNB", quote="USDT",
                       dex_buy="biswap", dex_sell="pancakeswap_v2",
                       size_usd=500.0, profit_usd=3.21, profit_bps=64.2,
                       dry_run=True, status="simulated")
    pid = record_price("binance", "WBNB/USDT", 703.11)
    after = count_trades()
    print(f"Trade inserido id={tid} | PriceSnapshot id={pid}")
    print(f"count_trades: {before} -> {after}")
    print("último trade:", recent_trades(1)[0])
    assert after == before + 1, "esperava-se +1 trade"
    print("SMOKE OK")
