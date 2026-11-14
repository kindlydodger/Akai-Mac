#!/usr/bin/env python3
"""Cardputer / SD trigger script: send a Google Flow prompt via local Webhook (:6780) & report UDP telemetry."""

import json
import socket
import sys
import urllib.request

WEBHOOK_URL = "http://127.0.0.1:6780/webhook"
CARDPUTER_IP = "192.168.4.1"
LOG_PORT = 8889


def udp_log(msg: str) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(msg.encode("utf-8"), (CARDPUTER_IP, LOG_PORT))
    finally:
        sock.close()


def trigger_flow(prompt: str, slot: int = 1, bpm: float = 92.0, key: str = "C minor") -> dict:
    payload = {
        "action": "flow_generate",
        "source": "cardputer-sd",
        "prompt": prompt,
        "slot": slot,
        "bpm": bpm,
        "key": key,
        "bars": 4,
        "sync": True,
    }
    req = urllib.request.Request(
        WEBHOOK_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


if __name__ == "__main__":
    prompt_text = " ".join(sys.argv[1:]).strip() or "dusty lofi rhodes loop 92 bpm"
    udp_log(f"FlowGen: {prompt_text[:40]}")
    res = trigger_flow(prompt_text)
    flow = res.get("flow") or {}
    meta = flow.get("meta") or {}
    udp_log(f"FlowGen Ready B{flow.get('slot', 1):02d} {meta.get('bpm', '')}BPM {meta.get('key', '')}")
    print(json.dumps(res, indent=2, ensure_ascii=False))
