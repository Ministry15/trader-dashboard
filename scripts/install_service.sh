#!/usr/bin/env bash
# Instala e arranca o serviço systemd do crypto_bsc.
# Requer privilégios (sudo). Idempotente.
set -euo pipefail

SERVICE_NAME="crypto_bsc"
SRC="/opt/crypto_bsc/scripts/${SERVICE_NAME}.service"
DEST="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Este script precisa de privilégios. A reexecutar com sudo..."
  exec sudo -E bash "$0" "$@"
fi

[[ -f "${SRC}" ]] || { echo "ERRO: unit não encontrado em ${SRC}" >&2; exit 1; }
[[ -x /opt/crypto_bsc/venv/bin/python ]] || { echo "ERRO: venv em falta (/opt/crypto_bsc/venv)" >&2; exit 1; }

echo "A instalar ${DEST}..."
install -m 0644 "${SRC}" "${DEST}"

echo "A recarregar o systemd..."
systemctl daemon-reload

echo "A activar e arrancar ${SERVICE_NAME}..."
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

echo
echo "Feito. Estado:"
systemctl --no-pager --full status "${SERVICE_NAME}" || true
echo
echo "Logs em tempo real:  journalctl -u ${SERVICE_NAME} -f"
