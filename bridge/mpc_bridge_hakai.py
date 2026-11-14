import socket
import time
import os
import subprocess
import threading
from flask import Flask, request, jsonify

app = Flask(__name__)

CARDPUTER_IP = "192.168.4.1"
CARDPUTER_UDP_PORT = 8888
CARDPUTER_LOG_PORT = 8889

udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
log_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

SCRIPT_TARGET_DIR = os.path.expanduser('/data/MPCUTER/scripts/')
CARDPUTER_SD_BACKUP_DIR = os.path.expanduser('/data/MPCUTER/cardputer_sd_backup/')
MPC_PROJECTS_DIR = (
    '/media/okcomputer/MPC_Projects/'
    if os.path.isdir('/media/okcomputer/MPC_Projects')
    else ('/media/okcomputer/' if os.path.isdir('/media/okcomputer') else '/media/az01-internal/MPC_Projects/')
)
GDRIVE_REMOTE = 'gdrive:MPCUTER_Cloud_Vault'

os.makedirs(SCRIPT_TARGET_DIR, exist_ok=True)
os.makedirs(CARDPUTER_SD_BACKUP_DIR, exist_ok=True)

def send_log_to_cardputer(message):
    try:
        clean_msg = str(message).strip()
        if clean_msg:
            log_sock.sendto(clean_msg.encode('utf-8')[:200], (CARDPUTER_IP, CARDPUTER_LOG_PORT))
    except Exception:
        pass

@app.route('/status', methods=['GET'])
def status_check():
    return jsonify({"status": "online", "service": "MPCUTER Hakai Bridge v1.0.0"}), 200

@app.route('/deploy', methods=['POST'])
def deploy_script():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No JSON payload"}), 400
            
        raw_name = str(data.get('filename') or '').strip()
        filename = os.path.basename(raw_name) or 'auto.py'
        if filename in ('.', '..'):
            filename = 'auto.py'
        content = data.get('content', '')
        auto_run = bool(data.get('auto_run', False))
        
        os.makedirs(SCRIPT_TARGET_DIR, exist_ok=True)
        filepath = os.path.join(SCRIPT_TARGET_DIR, filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
            
        if auto_run:
            subprocess.run(['pkill', '-f', filepath], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.Popen(['python3', filepath], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            send_log_to_cardputer(f"Executed: {filename}")
            
        return jsonify({"status": "success", "filename": filename}), 200
    except Exception as e:
        send_log_to_cardputer(f"Deploy Err: {str(e)[:40]}")
        return jsonify({"error": str(e)}), 500

def run_rclone_sync(sync_target):
    send_log_to_cardputer("Cloud Sync Started...")
    targets = []
    if sync_target in ["all", "sd"]:
        targets.append((CARDPUTER_SD_BACKUP_DIR, f"{GDRIVE_REMOTE}/Cardputer_SD"))
    if sync_target in ["all", "mpc"]:
        targets.append((MPC_PROJECTS_DIR, f"{GDRIVE_REMOTE}/MPC_Drive"))
        targets.append((SCRIPT_TARGET_DIR, f"{GDRIVE_REMOTE}/Hakai_Scripts"))

    for src, dst in targets:
        if not os.path.exists(src):
            continue
        send_log_to_cardputer(f"Syncing: {os.path.basename(src.rstrip('/'))}")
        cmd = ['rclone', 'copy', src, dst, '--update', '--transfers', '4', '--stats-one-line', '-v']
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            if proc.stdout is not None:
                for line in proc.stdout:
                    if "Transferred:" in line or "100%" in line:
                        send_log_to_cardputer(line.strip()[:38])
        finally:
            if proc.stdout is not None:
                proc.stdout.close()
        proc.wait()

    send_log_to_cardputer("GDrive Backup Complete! [H:1]")

@app.route('/cloud-backup', methods=['POST'])
def cloud_backup():
    try:
        data = request.get_json() or {}
        target = data.get('target', 'all')
        sd_files = data.get('sd_files', {})
        for fname, content in sd_files.items():
            safe_name = os.path.basename(fname)
            with open(os.path.join(CARDPUTER_SD_BACKUP_DIR, safe_name), 'w', encoding='utf-8') as f:
                f.write(content)

        threading.Thread(target=run_rclone_sync, args=(target,), daemon=True).start()
        return jsonify({"status": "started", "target": target}), 200
    except Exception as e:
        send_log_to_cardputer(f"Backup Err: {str(e)[:30]}")
        return jsonify({"error": str(e)}), 500

def telemetry_loop():
    while True:
        try:
            csv_payload = "L:78,R:74,BPM:120.0,H:1"
            udp_sock.sendto(csv_payload.encode('utf-8'), (CARDPUTER_IP, CARDPUTER_UDP_PORT))
            time.sleep(0.05)
        except Exception:
            time.sleep(1.0)

if __name__ == '__main__':
    t = threading.Thread(target=telemetry_loop, daemon=True)
    t.start()
    app.run(host='0.0.0.0', port=5000)
