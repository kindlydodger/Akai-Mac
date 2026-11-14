#!/usr/bin/env bash
# AntiGravity Auto-Script: Yerel Webhook + İzlenen Klasör + IAC Driver Bus 1 Kurulum & Başlatıcı
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MPC_CONTROL_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PORT="${MPC_WEBHOOK_PORT:-6780}"
BIND="${MPC_WEBHOOK_BIND:-127.0.0.1}"
INBOX_DIR="${MPC_WEBHOOK_INBOX:-${MPC_CONTROL_DIR}/webhook_inbox}"
LOG_FILE="${MPC_CONTROL_DIR}/webhook_server.log"
PID_FILE="${MPC_CONTROL_DIR}/.webhook_server.pid"

mkdir -p "${MPC_CONTROL_DIR}/bin" "${INBOX_DIR}"

# 1. Native CoreMIDI IAC köprüsünü gerekirse derle
if [[ ! -x "${MPC_CONTROL_DIR}/bin/mpc_iac_midi" ]] || [[ "${MPC_CONTROL_DIR}/native/mpc_iac_midi.c" -nt "${MPC_CONTROL_DIR}/bin/mpc_iac_midi" ]]; then
  /usr/bin/clang -O2 -Wall -Wextra "${MPC_CONTROL_DIR}/native/mpc_iac_midi.c" \
    -framework CoreMIDI -framework CoreFoundation \
    -o "${MPC_CONTROL_DIR}/bin/mpc_iac_midi"
fi

# 2. macOS IAC Driver Bus 1 ('IAC Sürücüsü Yol 1') çevrimiçi hale getir
"${MPC_CONTROL_DIR}/bin/mpc_iac_midi" --ensure-iac

# 3. Mod seçimi: --verify | --stop | --background | (varsayılan önplan)
MODE="${1:-serve}"
case "${MODE}" in
  --stop)
    if [[ -f "${PID_FILE}" ]]; then
      kill "$(cat "${PID_FILE}")" 2>/dev/null || true
      rm -f "${PID_FILE}"
    fi
    pkill -f "[m]pc_webhook_server.py serve" 2>/dev/null || true
    echo "Webhook sunucusu durduruldu."
    ;;
  --background)
    if [[ -f "${PID_FILE}" ]] && kill -0 "$(cat "${PID_FILE}")" 2>/dev/null; then
      echo "Webhook sunucusu zaten çalışıyor (PID $(cat "${PID_FILE}"))."
    else
      nohup /usr/bin/python3 "${MPC_CONTROL_DIR}/mpc_webhook_server.py" serve \
        --bind "${BIND}" --port "${PORT}" --inbox-dir "${INBOX_DIR}" \
        >> "${LOG_FILE}" 2>&1 < /dev/null &
      echo $! > "${PID_FILE}"
      sleep 0.5
    fi
    /usr/bin/python3 "${MPC_CONTROL_DIR}/mpc_webhook_server.py" status --url "http://${BIND}:${PORT}/health"
    ;;
  --verify)
    /usr/bin/python3 "${MPC_CONTROL_DIR}/mpc_webhook_server.py" send \
      --url "http://${BIND}:${PORT}/webhook" \
      --message "AntiGravity Auto-Script Doğrulama Mesajı" \
      --note C4 --velocity 100 --duration-ms 120
    echo "--- Son AntiGravity Aktivite Akışı ---"
    tail -n 5 "${MPC_CONTROL_DIR}/activity_feed.jsonl" || true
    echo "--- İzlenen Klasör (${INBOX_DIR}) ---"
    ls -lt "${INBOX_DIR}" | head -n 6
    ;;
  *)
    exec /usr/bin/python3 "${MPC_CONTROL_DIR}/mpc_webhook_server.py" serve \
      --bind "${BIND}" --port "${PORT}" --inbox-dir "${INBOX_DIR}"
    ;;
esac
