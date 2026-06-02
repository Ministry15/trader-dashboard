#!/usr/bin/env bash
# Pára, desactiva e remove o serviço systemd do crypto_bsc.
set -euo pipefail

SERVICE_NAME="crypto_bsc"
DEST="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Este script precisa de privilégios. A reexecutar com sudo..."
  exec sudo -E bash "$0" "$@"
fi

echo "A parar ${SERVICE_NAME} (se a correr)..."
systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
echo "A desactivar ${SERVICE_NAME}..."
systemctl disable "${SERVICE_NAME}" 2>/dev/null || true

if [[ -f "${DEST}" ]]; then
  echo "A remover ${DEST}..."
  rm -f "${DEST}"
fi

systemctl daemon-reload
systemctl reset-failed "${SERVICE_NAME}" 2>/dev/null || true
echo "Serviço removido."
