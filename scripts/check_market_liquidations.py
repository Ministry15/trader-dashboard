#!/usr/bin/env python3
"""
check_market_liquidations.py — Diagnóstico read-only
Aave V3   : LiquidationCall  — Base, Arbitrum, Polygon
Compound V3: AbsorbDebt/AbsorbCollateral — Optimism (USDC+USDT), Arbitrum (USDC+USDT)
Filtro: $500 – $50,000 USD
"""

import os, sys, time
from datetime import datetime, timezone
from web3 import Web3
from eth_abi import decode as abi_decode

# ─── Carrega .env ─────────────────────────────────────────────────────────────
_ENV = '/opt/crypto_bsc/.env'
if os.path.exists(_ENV):
    with open(_ENV) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _, _v = _line.partition('=')
                os.environ.setdefault(_k.strip(), _v.strip())

# ─── Parâmetros ───────────────────────────────────────────────────────────────
DEBT_MIN_USD = 500.0
DEBT_MAX_USD = 50_000.0
DAYS_BACK    = 3
ORACLE_DEC   = 8       # Aave oracle: USD com 8 decimais

# keccak256("LiquidationCall(address,address,address,uint256,uint256,address,bool)")
# HexBytes.hex() não tem prefixo 0x — adicionamos manualmente
LIQ_TOPIC = '0x' + Web3.keccak(
    text='LiquidationCall(address,address,address,uint256,uint256,address,bool)'
).hex()

CHAINS = {
    'Base': {
        'rpcs': [
            'https://mainnet.base.org',
            'https://rpc.ankr.com/base',
            'https://base.publicnode.com',
        ],
        'pool':       '0xA238Dd80C259a72e81d7e4664a9801593F98d1c5',
        'oracle':     '0x2Cc0Fc26eD4563A5ce5e8bdcfe1a2878676Ae156',
        'block_time': 2.0,
        'chunk':      2_000,
    },
    'Arbitrum': {
        'rpcs': [
            'https://arb1.arbitrum.io/rpc',
            'https://arbitrum.publicnode.com',
            'https://rpc.ankr.com/arbitrum',
        ],
        'pool':       '0x794a61358D6845594F94dc1DB02A252b5b4814aD',
        'oracle':     '0xb56c2F0B653B2e0b10C9b928C8580Ac5Df02C7C7',
        'block_time': 0.25,
        'chunk':      10_000,
    },
    'Polygon': {
        'rpcs': [
            'https://rpc.ankr.com/polygon',
            'https://polygon-bor-rpc.publicnode.com',
            'https://polygon-rpc.com',
        ],
        'pool':       '0x794a61358D6845594F94dc1DB02A252b5b4814aD',
        'oracle':     '0xb023e699F5a33916Ea823A16485e259257cA8Bd1',
        'block_time': 2.0,
        'chunk':      2_000,
    },
}

# keccak256 para eventos Compound V3
ABSORB_DEBT_TOPIC   = '0x' + Web3.keccak(text='AbsorbDebt(address,address,uint256,uint256)').hex()
ABSORB_COLLAT_TOPIC = '0x' + Web3.keccak(text='AbsorbCollateral(address,address,address,uint256,uint256)').hex()

# ─── Compound V3: Comets por chain ────────────────────────────────────────────
COMPOUND_CHAINS = {
    'Optimism': {
        'rpcs': [
            'https://mainnet.optimism.io',
            'https://rpc.ankr.com/optimism',
            'https://optimism.publicnode.com',
        ],
        'block_time': 2.0,
        'chunk':      2_000,
        'comets': [
            {'key': 'OP-USDC',  'address': '0x2e44e174f7D53F0212823acC11C01A11d58c5bCb', 'base_token': 'USDC', 'base_dec': 6},
            {'key': 'OP-USDT',  'address': '0x995E394b8B2437aC8Ce61Ee0bC610D617962B214', 'base_token': 'USDT', 'base_dec': 6},
        ],
    },
    'Arbitrum': {
        'rpcs': [
            'https://arb1.arbitrum.io/rpc',
            'https://arbitrum.publicnode.com',
            'https://rpc.ankr.com/arbitrum',
        ],
        'block_time': 0.25,
        'chunk':      10_000,
        'comets': [
            {'key': 'ARB-USDC', 'address': '0xA5EDBDD9646f8dFF606d7448e414884C7d905dCA', 'base_token': 'USDC', 'base_dec': 6},
            {'key': 'ARB-USDT', 'address': '0xd98Be00b5D27fc98112BdE293e487f8D4cA57d07', 'base_token': 'USDT', 'base_dec': 6},
        ],
    },
}

ORACLE_ABI = [{
    "inputs":  [{"name": "asset",  "type": "address"}],
    "name":    "getAssetPrice",
    "outputs": [{"name": "", "type": "uint256"}],
    "stateMutability": "view", "type": "function",
}]
ERC20_ABI = [
    {"inputs":[], "name":"decimals", "outputs":[{"name":"","type":"uint8"}],  "stateMutability":"view","type":"function"},
    {"inputs":[], "name":"symbol",   "outputs":[{"name":"","type":"string"}], "stateMutability":"view","type":"function"},
]

# ─── Helpers ──────────────────────────────────────────────────────────────────

def connect(rpcs: list) -> Web3 | None:
    for rpc in rpcs:
        if not rpc:
            continue
        for attempt in range(2):
            try:
                w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={'timeout': 20}))
                bn = w3.eth.block_number  # call real instead of is_connected()
                if bn > 0:
                    print(f"    RPC: {rpc.split('/')[2]}")
                    return w3
            except Exception:
                if attempt == 0:
                    time.sleep(1)
    return None


_tok_cache: dict = {}

def token_info(w3: Web3, addr: str) -> dict:
    key = addr.lower()
    if key in _tok_cache:
        return _tok_cache[key]
    try:
        c   = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=ERC20_ABI)
        sym = c.functions.symbol().call()
        dec = c.functions.decimals().call()
    except Exception:
        sym, dec = addr[-6:], 18
    _tok_cache[key] = {'symbol': sym, 'decimals': dec}
    return _tok_cache[key]


def get_price_usd(oracle, addr: str) -> float:
    try:
        raw = oracle.functions.getAssetPrice(Web3.to_checksum_address(addr)).call()
        return raw / 10**ORACLE_DEC
    except Exception:
        return 0.0


def fetch_logs(w3: Web3, pool: str, from_block: int, to_block: int, chunk: int,
               topic: str = LIQ_TOPIC) -> list:
    logs  = []
    cur   = from_block
    total = to_block - from_block
    done  = 0
    while cur <= to_block:
        end = min(cur + chunk - 1, to_block)
        try:
            batch = w3.eth.get_logs({
                'address':   Web3.to_checksum_address(pool),
                'topics':    [topic],
                'fromBlock': cur,
                'toBlock':   end,
            })
            logs.extend(batch)
            done += end - cur + 1
            pct   = done / total * 100 if total else 100
            print(f"\r    Progresso: {pct:5.1f}%  ({len(logs)} eventos)   ", end='', flush=True)
        except Exception as e:
            err = str(e)
            if '429' in err or 'rate' in err.lower():
                time.sleep(3)
                continue  # retry
            elif any(k in err.lower() for k in ('range', 'limit', 'too many', 'block range')):
                chunk = max(500, chunk // 2)
                print(f"\n    [chunk reduzido para {chunk}]", flush=True)
                continue
            else:
                print(f"\n    [WARN] {cur}-{end}: {err[:100]}")
                done += end - cur + 1
        cur = end + 1
        time.sleep(0.12)
    print()  # newline after progress
    return logs


def parse_log(log) -> dict:
    topics     = log['topics']
    collateral = '0x' + topics[1].hex()[-40:]
    debt_asset = '0x' + topics[2].hex()[-40:]
    borrower   = '0x' + topics[3].hex()[-40:]
    raw_data   = bytes(log['data']) if not isinstance(log['data'], bytes) else log['data']
    debt_cover, _liq_col, _liquidator, _recv = abi_decode(
        ['uint256', 'uint256', 'address', 'bool'], raw_data
    )
    return {
        'collateral': collateral,
        'debt_asset': debt_asset,
        'borrower':   borrower,
        'debt_cover': debt_cover,
        'block':      log['blockNumber'],
        'tx':         log['transactionHash'].hex(),
    }


def scan_chain(name: str, cfg: dict) -> list:
    print(f"\n{'─'*62}")
    print(f"  {name}")
    print(f"{'─'*62}")

    w3 = connect(cfg['rpcs'])
    if not w3:
        print(f"  [ERRO] Sem RPC disponível")
        return []

    latest     = w3.eth.block_number
    blocks_3d  = int(DAYS_BACK * 24 * 3600 / cfg['block_time'])
    from_block = max(0, latest - blocks_3d)
    print(f"  Bloco actual : {latest:>12,}")
    print(f"  From block   : {from_block:>12,}  (≈ {blocks_3d:,} blocos / {DAYS_BACK}d)")

    oracle = w3.eth.contract(
        address=Web3.to_checksum_address(cfg['oracle']), abi=ORACLE_ABI
    )

    print(f"  A consultar getLogs (chunk={cfg['chunk']:,})...")
    raw_logs = fetch_logs(w3, cfg['pool'], from_block, latest, cfg['chunk'])
    print(f"  Total LiquidationCall: {len(raw_logs)}")

    results = []
    price_cache: dict = {}

    for log in raw_logs:
        try:
            ev = parse_log(log)
        except Exception as e:
            continue

        da = ev['debt_asset'].lower()
        if da not in price_cache:
            price_cache[da] = get_price_usd(oracle, ev['debt_asset'])
        price = price_cache[da]
        if price <= 0:
            continue

        info     = token_info(w3, ev['debt_asset'])
        debt_amt = ev['debt_cover'] / 10**info['decimals']
        debt_usd = debt_amt * price

        if not (DEBT_MIN_USD <= debt_usd <= DEBT_MAX_USD):
            continue

        # timestamp — só para eventos que passaram o filtro
        try:
            blk_data = w3.eth.get_block(ev['block'])
            ts = datetime.fromtimestamp(blk_data['timestamp'], tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
        except Exception:
            ts = f"blk#{ev['block']}"

        col_info = token_info(w3, ev['collateral'])

        results.append({
            'chain':    name,
            'ts':       ts,
            'borrower': ev['borrower'],
            'debt_sym': info['symbol'],
            'debt_usd': debt_usd,
            'col_sym':  col_info['symbol'],
            'tx':       ev['tx'],
        })

    print(f"  Filtradas ($500–$50k): {len(results)}")
    return results


# ─── COMPOUND V3 ──────────────────────────────────────────────────────────────

def parse_absorb_debt(log) -> dict:
    """AbsorbDebt: topics[1]=absorber, topics[2]=borrower; data=basePaidOut,usdValue"""
    topics   = log['topics']
    borrower = '0x' + topics[2].hex()[-40:]
    raw      = bytes(log['data']) if not isinstance(log['data'], bytes) else log['data']
    base_paid, _usd = abi_decode(['uint256', 'uint256'], raw)
    return {
        'borrower':  borrower,
        'base_paid': base_paid,
        'block':     log['blockNumber'],
        'tx':        log['transactionHash'].hex(),
    }


def parse_absorb_collat(log) -> dict:
    """AbsorbCollateral: topics[1]=absorber, topics[2]=borrower, topics[3]=asset"""
    topics   = log['topics']
    borrower = '0x' + topics[2].hex()[-40:]
    asset    = '0x' + topics[3].hex()[-40:]
    return {
        'borrower': borrower,
        'asset':    asset,
        'tx':       log['transactionHash'].hex(),
    }


def scan_compound_chain(chain_name: str, cfg: dict) -> list:
    print(f"\n{'─'*62}")
    print(f"  Compound V3 — {chain_name}")
    print(f"{'─'*62}")

    w3 = connect(cfg['rpcs'])
    if not w3:
        print(f"  [ERRO] Sem RPC disponível")
        return []

    latest     = w3.eth.block_number
    blocks_3d  = int(DAYS_BACK * 24 * 3600 / cfg['block_time'])
    from_block = max(0, latest - blocks_3d)
    print(f"  Bloco actual : {latest:>12,}")
    print(f"  From block   : {from_block:>12,}  (≈ {blocks_3d:,} blocos / {DAYS_BACK}d)")

    all_results = []

    for comet in cfg['comets']:
        key      = comet['key']
        addr     = comet['address']
        base_tok = comet['base_token']
        base_dec = comet['base_dec']
        print(f"\n  Comet {key} ({addr[:10]}…)")

        # AbsorbDebt logs
        print(f"    AbsorbDebt getLogs (chunk={cfg['chunk']:,})...")
        debt_logs = fetch_logs(w3, addr, from_block, latest, cfg['chunk'], ABSORB_DEBT_TOPIC)
        print(f"    AbsorbDebt total: {len(debt_logs)}")

        # AbsorbCollateral logs (para obter o asset de garantia)
        print(f"    AbsorbCollateral getLogs...")
        collat_logs = fetch_logs(w3, addr, from_block, latest, cfg['chunk'], ABSORB_COLLAT_TOPIC)
        print(f"    AbsorbCollateral total: {len(collat_logs)}")

        # Índice: (tx_hash, borrower) → collateral_asset
        collat_idx: dict = {}
        for cl in collat_logs:
            try:
                ev = parse_absorb_collat(cl)
                k  = (ev['tx'], ev['borrower'].lower())
                collat_idx.setdefault(k, ev['asset'])
            except Exception:
                pass

        results = []
        for dl in debt_logs:
            try:
                ev = parse_absorb_debt(dl)
            except Exception:
                continue

            debt_usd = ev['base_paid'] / 10**base_dec  # USDC/USDT ≈ $1/token

            if not (DEBT_MIN_USD <= debt_usd <= DEBT_MAX_USD):
                continue

            # Timestamp
            try:
                blk_data = w3.eth.get_block(ev['block'])
                ts = datetime.fromtimestamp(blk_data['timestamp'], tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
            except Exception:
                ts = f"blk#{ev['block']}"

            # Colateral
            col_key  = (ev['tx'], ev['borrower'].lower())
            col_addr = collat_idx.get(col_key)
            if col_addr:
                col_sym = token_info(w3, col_addr)['symbol']
            else:
                col_sym = '—'

            results.append({
                'chain':    chain_name,
                'protocol': f'Cmpd-{base_tok}',
                'ts':       ts,
                'borrower': ev['borrower'],
                'debt_sym': base_tok,
                'debt_usd': debt_usd,
                'col_sym':  col_sym,
                'tx':       ev['tx'],
            })

        print(f"    Filtradas ($500–$50k): {len(results)}")
        all_results.extend(results)

    return all_results


# ─── Impressão de resultados ──────────────────────────────────────────────────

def print_results(all_results: list, section_label: str) -> None:
    print(f"\n\n{'='*62}")
    print(f"  {section_label} — {len(all_results)} liquidações no filtro")
    print(f"{'='*62}")

    if not all_results:
        print(f"  Nenhuma liquidação $500–$50k nos últimos {DAYS_BACK} dias.")
        return

    all_results.sort(key=lambda x: (x.get('protocol', x['chain']), x['ts'], -x['debt_usd']))

    hdr = f"{'Chain/Protocolo':<15} {'Timestamp':<17} {'Borrower':<14} {'Debt':<8} {'Debt USD':>10} {'Colateral':<12} TX"
    print(f"\n{hdr}")
    print('─' * 95)

    for r in all_results:
        label  = r.get('protocol', r['chain'])
        bshort = r['borrower'][:6] + '…' + r['borrower'][-4:]
        tx_s   = r['tx'][:10] + '…'
        print(
            f"{label:<15} {r['ts']:<17} {bshort:<14} "
            f"{r['debt_sym']:<8} {r['debt_usd']:>10,.2f} {r['col_sym']:<12} {tx_s}"
        )

    by_proto = {}
    for r in all_results:
        k = r.get('protocol', r['chain'])
        by_proto.setdefault(k, []).append(r['debt_usd'])

    print(f"\n  Resumo:")
    for proto, vals in by_proto.items():
        print(f"    {proto:<15} {len(vals):3d} liq | total ${sum(vals):>12,.2f} | média ${sum(vals)/len(vals):>8,.2f}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*62}")
    print(f"  check_market_liquidations.py")
    print(f"  Últimos {DAYS_BACK} dias | Filtro: ${DEBT_MIN_USD:,.0f} – ${DEBT_MAX_USD:,.0f}")
    print(f"{'='*62}")

    # ── AAVE V3 ──────────────────────────────────────────────────────────────
    print(f"\n{'#'*62}")
    print(f"  AAVE V3 — Base, Arbitrum, Polygon")
    print(f"{'#'*62}")

    aave_results: list = []
    for name, cfg in CHAINS.items():
        try:
            r = scan_chain(name, cfg)
            # add protocol label for display
            for row in r:
                row['protocol'] = f"Aave-{row['chain'][:4]}"
            aave_results.extend(r)
        except KeyboardInterrupt:
            print(f"\n  Interrompido em {name}.")
            break
        except Exception as e:
            print(f"  [ERRO] {name}: {e}")

    print_results(aave_results, "AAVE V3 RESULTADOS")

    # ── COMPOUND V3 ──────────────────────────────────────────────────────────
    print(f"\n\n{'#'*62}")
    print(f"  COMPOUND V3 — Optimism + Arbitrum")
    print(f"{'#'*62}")

    cmpd_results: list = []
    for chain_name, cfg in COMPOUND_CHAINS.items():
        try:
            r = scan_compound_chain(chain_name, cfg)
            cmpd_results.extend(r)
        except KeyboardInterrupt:
            print(f"\n  Interrompido em Compound {chain_name}.")
            break
        except Exception as e:
            print(f"  [ERRO] Compound {chain_name}: {e}")

    print_results(cmpd_results, "COMPOUND V3 RESULTADOS")

    # ── TOTAIS ───────────────────────────────────────────────────────────────
    total = aave_results + cmpd_results
    print(f"\n\n{'='*62}")
    print(f"  TOTAL GERAL: {len(total)} liquidações em {DAYS_BACK} dias ($500–$50k)")
    print(f"{'='*62}")

if __name__ == '__main__':
    main()
