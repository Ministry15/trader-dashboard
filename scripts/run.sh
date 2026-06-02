#!/usr/bin/env bash
# Corre o orquestrador em foreground com o venv (sem systemd) — útil para testes.
# Exemplos:
#   scripts/run.sh                 # bots de settings.orchestrator.enabled_bots
#   scripts/run.sh --once          # uma iteração de cada bot e sai
#   scripts/run.sh arbitrage dca   # corre apenas estes bots
set -euo pipefail

cd /opt/crypto_bsc
exec /opt/crypto_bsc/venv/bin/python /opt/crypto_bsc/main.py "$@"
