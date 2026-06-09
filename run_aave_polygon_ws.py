#!/usr/bin/env python3
"""Runner WebSocket para AaveLiquidatorPolygonBot.

Substitui o loop genérico run_bot.py + time.sleep(30).
Subscreve eth_subscribe("newHeads") via Alchemy WS → dispara tick() em cada
novo bloco (~2s na Polygon). Latência <1s vs até 30s com HTTP polling.

Diferenças face ao run_bot.py:
  - tick() corre numa thread (run_in_executor) para não bloquear o event loop
  - Se um tick ainda estiver em curso quando chega o próximo bloco, salta-o
  - Reconnect automático em caso de queda do WebSocket
"""
import asyncio
import json
import logging
import signal
import sys

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

sys.path.insert(0, "/opt/crypto_bsc")

from utils.config import get_env
from bots.aave_liquidator_polygon_bot import AaveLiquidatorPolygonBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("run_aave_polygon_ws")

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logger.info("run_aave_polygon_ws: sinal %d recebido — a encerrar", signum)
    _shutdown = True


async def _safe_tick(bot: AaveLiquidatorPolygonBot, lock: asyncio.Lock) -> None:
    """Corre bot.tick() numa thread. Salta bloco se tick anterior ainda em curso."""
    if lock.locked():
        logger.warning("run_aave_polygon_ws: tick anterior ainda em curso — bloco saltado")
        return
    async with lock:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, bot.tick)


async def run() -> None:
    ws_url = get_env("ALCHEMY_POLYGON_WS_URL")
    if not ws_url:
        logger.critical("run_aave_polygon_ws: ALCHEMY_POLYGON_WS_URL não definido no .env")
        sys.exit(1)

    logger.info("run_aave_polygon_ws: a inicializar AaveLiquidatorPolygonBot")
    bot = AaveLiquidatorPolygonBot()
    logger.info("run_aave_polygon_ws: bot pronto — a ligar ao WebSocket")

    tick_lock = asyncio.Lock()

    while not _shutdown:
        try:
            async with websockets.connect(
                ws_url,
                ping_interval=20,
                ping_timeout=30,
            ) as ws:
                await ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id":      1,
                    "method":  "eth_subscribe",
                    "params":  ["newHeads"],
                }))
                resp   = json.loads(await ws.recv())
                sub_id = resp.get("result")
                logger.info("run_aave_polygon_ws: subscrito newHeads (sub_id=%s)", sub_id)

                async for raw in ws:
                    if _shutdown:
                        break

                    msg = json.loads(raw)
                    if msg.get("method") != "eth_subscription":
                        continue

                    block_num = int(msg["params"]["result"].get("number", "0x0"), 16)
                    logger.info("run_aave_polygon_ws: bloco %d → tick", block_num)

                    asyncio.create_task(_safe_tick(bot, tick_lock))

        except (ConnectionClosed, WebSocketException, OSError) as exc:
            if _shutdown:
                break
            logger.warning("run_aave_polygon_ws: WS desligado (%s) — reconnect em 5s", exc)
            await asyncio.sleep(5)

    logger.info("run_aave_polygon_ws: encerrado")


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    asyncio.run(run())


if __name__ == "__main__":
    main()
