"""Flashbots bundle submission — eth_sendBundle para Base e Polygon.

Uso:
    from utils.flashbots import send_bundle, send_bundle_multi

    # Um único tx:
    bundle_hash = send_bundle(raw_tx_hex, target_block, endpoint, pk)

    # Múltiplos txs num único bundle (Phase 6):
    bundle_hash = send_bundle_multi([tx1_hex, tx2_hex, ...], target_block, endpoint, pk)

O chamador deve apanhar e fazer fallback para mempool em caso de excepção.
"""
from __future__ import annotations

import json
import logging

import requests
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

logger = logging.getLogger(__name__)


def send_bundle(
    raw_tx_hex: str,
    target_block: int,
    endpoint: str,
    pk: str,
) -> str | None:
    """
    Submete um Flashbots bundle com uma única transação assinada.

    raw_tx_hex   – transação assinada em hex (com ou sem prefixo 0x)
    target_block – número do bloco alvo para inclusão
    endpoint     – URL do relay (ex. https://relay.flashbots.net)
    pk           – private key hex para assinar o bundle request

    Devolve bundleHash (str) em sucesso, None se a API não devolver resultado.
    Lança excepção em erro de rede ou resposta HTTP não-2xx.
    O chamador deve apanhar e fazer fallback para mempool normal.
    """
    if not raw_tx_hex.startswith("0x"):
        raw_tx_hex = "0x" + raw_tx_hex

    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_sendBundle",
        "params": [{
            "txs": [raw_tx_hex],
            "blockNumber": hex(target_block),
        }],
    }, separators=(',', ':'))

    acct        = Account.from_key(pk)
    body_hash   = Web3.keccak(text=body)
    msg         = encode_defunct(body_hash)
    signed_msg  = Account.sign_message(msg, private_key=pk)
    signature   = f"{acct.address}:{signed_msg.signature.hex()}"

    resp = requests.post(
        endpoint,
        headers={
            "Content-Type": "application/json",
            "X-Flashbots-Signature": signature,
        },
        data=body.encode("utf-8"),
        timeout=5,
    )
    resp.raise_for_status()
    result = resp.json()
    if "error" in result:
        raise RuntimeError(f"eth_sendBundle erro: {result['error']}")
    return result.get("result", {}).get("bundleHash")


def send_bundle_multi(
    raw_tx_hexes: list[str],
    target_block: int,
    endpoint: str,
    pk: str,
) -> str | None:
    """
    Submete um Flashbots bundle com MÚLTIPLAS transações assinadas.

    raw_tx_hexes – lista de transações assinadas em hex (cada uma com ou sem 0x)
    target_block – número do bloco alvo; todas as txs executam nesse bloco em ordem
    endpoint     – URL do relay (ex. https://relay.flashbots.net)
    pk           – private key para assinar o pedido HTTP (não precisa ser o mesmo
                   endereço dos remetentes das txs)

    Se qualquer tx reverter, o bundle inteiro é rejeitado pelo relay (sem gas gasto).
    Devolve bundleHash em sucesso; None se a API não devolver resultado.
    Lança excepção em erro de rede/HTTP — o chamador deve fazer fallback.
    """
    txs = [("0x" + h if not h.startswith("0x") else h) for h in raw_tx_hexes]

    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_sendBundle",
        "params": [{
            "txs": txs,
            "blockNumber": hex(target_block),
        }],
    }, separators=(',', ':'))

    acct        = Account.from_key(pk)
    body_hash   = Web3.keccak(text=body)
    msg         = encode_defunct(body_hash)
    signed_msg  = Account.sign_message(msg, private_key=pk)
    signature   = f"{acct.address}:{signed_msg.signature.hex()}"

    resp = requests.post(
        endpoint,
        headers={
            "Content-Type": "application/json",
            "X-Flashbots-Signature": signature,
        },
        data=body.encode("utf-8"),
        timeout=5,
    )
    resp.raise_for_status()
    result = resp.json()
    if "error" in result:
        raise RuntimeError(f"eth_sendBundle (multi) erro: {result['error']}")
    return result.get("result", {}).get("bundleHash")
