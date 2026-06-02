# crypto_bsc — Contexto do Projecto

## Visão Geral
`crypto_bsc` é um conjunto de bots de trading/arbitragem automatizada na
**Binance Smart Chain (BSC mainnet, chain_id 56)**. O sistema monitoriza preços
em múltiplos DEXs (PancakeSwap, BiSwap, ApeSwap) e numa CEX (Binance via ccxt),
detecta oportunidades de arbitragem e executa swaps on-chain, com notificações
via Telegram e persistência em base de dados.

> **Estado actual (2026-06-01):** orquestrador funcional, 4 bots implementados
> (arbitrage, grid, dca, sniper), camada `core` e `utils` completas, persistência
> SQLite activa e serviço systemd instalado a correr 24/7. Por defeito corre em
> `DRY_RUN=true` — as transacções são *construídas mas NÃO enviadas* (confirmado
> nos logs: `[DRY_RUN] transacção NÃO enviada`). Nenhuma operação real on-chain
> ocorre até `DRY_RUN` ser explicitamente desactivado no `.env`.

## Estrutura de Directórios
```
/opt/crypto_bsc/
├── venv/                  # Virtualenv Python 3.11 (todas as dependências vivem aqui)
├── main.py                # Orquestrador: corre vários bots em threads (ponto de entrada)
├── bots/
│   ├── arbitrage_bot.py   # Arbitragem entre DEXs uniswap_v2 (passo: run_once)
│   ├── grid_bot.py        # Grid trading — compra em quedas/vende em subidas (passo: tick)
│   ├── dca_bot.py         # DCA — compra montante fixo em intervalos (passo: buy_once)
│   └── sniper_bot.py      # Sniper — entra em alvos, sai por TP/SL (passo: tick)
├── core/
│   ├── wallet.py          # Ligação web3, saldos, envio de tx (respeita DRY_RUN)
│   ├── dex.py             # Interface DEX uniswap-v2 (PancakeSwap V2, BiSwap, ApeSwap)
│   ├── price_feed.py      # Agregação de preços DEX on-chain + CEX (Binance/ccxt)
│   └── risk_manager.py    # Aplica limites de settings.yaml > trading
├── utils/
│   ├── config.py          # get_env() / get_settings() — carrega .env + settings.yaml
│   ├── database.py         # Persistência SQLAlchemy 2.0 (tabelas: trades, price_snapshots)
│   ├── logger.py          # setup_logging() — colorlog + ficheiro rotativo
│   └── notifier.py        # TelegramNotifier — notificações via Bot API
├── data/
│   ├── crypto_bsc.db      # SQLite (tabelas: trades, price_snapshots)
│   └── logs/              # Ficheiros de log rotativos (crypto_bsc.log)
├── config/
│   └── settings.yaml      # Configuração não-secreta (rede, tokens, DEXs, estratégia)
├── scripts/
│   ├── crypto_bsc.service # Unit systemd (User=trader, venv 3.11, hardening)
│   ├── install_service.sh # Instala + activa + arranca o serviço (idempotente, sudo)
│   ├── uninstall_service.sh # Pára, desactiva e remove o serviço (sudo)
│   └── run.sh             # Corre o orquestrador em foreground (testes, sem systemd)
├── .env                   # Segredos (chaves privadas, API keys) — NUNCA versionar
├── requirements.txt       # Dependências Python fixadas
└── CLAUDE.md              # Este ficheiro
```

## Configuração
- **Segredos** → `.env` (chaves privadas, API keys, tokens Telegram). Nunca commitar.
- **Config não-secreta** → `config/settings.yaml`. Usa placeholders `${VAR}` que
  são resolvidos a partir do `.env` no arranque.
- Toda a config secreta é carregada via `python-dotenv`; a config geral via `pyyaml`.

## Componentes Principais
- **Rede / RPC:** web3.py contra os endpoints RPC/WSS da BSC (com backup).
- **DEXs (Uniswap-V2 style):** PancakeSwap V2, BiSwap, ApeSwap (routers + factories
  definidos em `settings.yaml`). PancakeSwap V3 incluído (quoter + fee tiers).
- **CEX:** Binance via `ccxt`.
- **Tokens monitorizados:** WBNB, USDT, USDC, BUSD, CAKE.
- **Notificações:** `python-telegram-bot`.
- **Persistência:** SQLAlchemy sobre SQLite (`data/crypto_bsc.db`).
- **Agendamento:** `schedule` para loops de scan/refresh.
- **Logging:** `colorlog` em consola + ficheiros rotativos em `data/logs/`.

## Orquestrador (`main.py`)
Ponto de entrada do sistema. Corre vários bots em conjunto, cada um na sua thread,
executando um "passo" (uma iteração da estratégia) em loop com o intervalo da config.
Isola falhas: o erro de um bot (na inicialização ou por iteração) não derruba os
outros nem o processo. Trata de arranque e encerramento limpo (SIGTERM/SIGINT).

- **Registo de bots** (`BOT_REGISTRY`): nome → (factory, método-passo, intervalo).
  `arbitrage`→`run_once`, `grid`→`tick`, `dca`→`buy_once`, `sniper`→`tick`.
- **Seleção de bots** (por ordem de prioridade): (1) argumentos CLI
  (`main.py arbitrage dca`); (2) env `ENABLED_BOTS=arbitrage,grid`;
  (3) `settings.yaml > orchestrator.enabled_bots` (default actual: arbitrage, grid, dca).
- **Flags:** `--list` lista bots disponíveis/seleccionados e sai;
  `--once` executa um único passo de cada bot e termina (validação).

## Serviço systemd
Unit instalado: **`crypto_bsc.service`** (underscore — consistente com o nome Python;
NÃO usar hífen `crypto-bsc`, esse nome não existe).

- Definição em `scripts/crypto_bsc.service`; instalado em `/etc/systemd/system/`.
- `User=trader`, `WorkingDirectory=/opt/crypto_bsc`,
  `ExecStart=/opt/crypto_bsc/venv/bin/python /opt/crypto_bsc/main.py`.
- `Restart=on-failure` (RestartSec=10), `enabled` (arranca no boot).
- Hardening: `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=full`,
  `ProtectHome=read-only`, `ReadWritePaths=/opt/crypto_bsc/data`.
- Logs vão para o journal (`SyslogIdentifier=crypto_bsc`) **e** para `data/logs/`.

Comandos:
```bash
sudo bash scripts/install_service.sh    # instalar + activar + arrancar
sudo bash scripts/uninstall_service.sh  # parar + desactivar + remover
systemctl status crypto_bsc             # estado
journalctl -u crypto_bsc -f             # logs em tempo real
```

## Stack Técnica
- **Python 3.11.15** num virtualenv em `/opt/crypto_bsc/venv`.
  > **Importante:** o `python3` do sistema é o **3.14**, que NÃO tem wheels para
  > `pandas==2.2.1`/`numpy==1.26.4` (a compilação falha). Por isso as dependências
  > foram instaladas num venv 3.11. Usar **sempre** `./venv/bin/python` e
  > `./venv/bin/pip`, nunca o `python3`/`pip` do sistema.
- web3 6.15.1 · ccxt 4.2.99 · pandas 2.2.1 · numpy 1.26.4
- aiohttp / websockets para I/O assíncrono
- SQLAlchemy 2.0 · pyyaml · python-dotenv · schedule · colorlog
- python-telegram-bot 20.8

## Convenções
- **Segurança em primeiro lugar:** o default é `DRY_RUN=true`. Nunca enviar
  transacções reais sem confirmação explícita. Nunca expor ou logar a `PRIVATE_KEY`.
- Endereços de contratos sempre em formato checksum.
- Limites de risco (perda diária, tamanho máximo de trade, slippage) definidos em
  `settings.yaml > trading.risk` — respeitar sempre.
- Logs vão para `data/logs/`; não imprimir segredos.

## Como Executar
```bash
cd /opt/crypto_bsc
# .env já preenchido; DRY_RUN=true por defeito.

# Via systemd (modo produção, 24/7) — já instalado:
systemctl status crypto_bsc

# Em foreground (testes, sem systemd):
./venv/bin/python main.py            # bots de orchestrator.enabled_bots
./venv/bin/python main.py --list     # lista bots e sai
./venv/bin/python main.py --once     # um passo por bot e sai (validação)
./venv/bin/python main.py arbitrage dca   # corre apenas estes bots
# (ou via wrapper: scripts/run.sh [args])
```

## Notas
- O VPS tem Python 3.14 (incompatível com as versões fixadas) — usar SEMPRE o venv.
- Validação rápida de dependências:
  `./venv/bin/python -c "import web3, ccxt, pandas; print('OK')"`
