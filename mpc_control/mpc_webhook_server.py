#!/usr/bin/env python3
"""Local Webhook Listener + Watched Inbox Recorder + macOS IAC Driver Bus 1 MIDI Router.

Pure Python 3 stdlib server bridging HTTP webhooks to:
  1. Monitored inbox directory (`mpc_control/webhook_inbox/`) watched by MPC Live Monitor
  2. Native macOS CoreMIDI destination (`IAC Driver Bus 1` / `IAC Sürücüsü Yol 1`) via `bin/mpc_iac_midi`
  3. Live AntiGravity / MPCUTER telemetry (`activity_feed.jsonl` + stdout logs)
"""

from __future__ import annotations

import argparse
import base64
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from typing import Any, Deque, Dict, List, Optional, Tuple

from mpc_client import emit_activity


HERE = Path(__file__).resolve().parent
DEFAULT_BIND = os.environ.get("MPC_WEBHOOK_BIND", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("MPC_WEBHOOK_PORT", "6780"))
DEFAULT_INBOX_DIR = Path(os.environ.get("MPC_WEBHOOK_INBOX", str(HERE / "webhook_inbox")))
DEFAULT_MIDI_DEST = os.environ.get("MPC_WEBHOOK_MIDI_DEST", "IAC")
DEFAULT_IAC_BIN = HERE / "bin" / "mpc_iac_midi"
MAX_BODY_BYTES = 4 * 1024 * 1024

_NOTE_SEMITONES = {
    "C": 0, "B#": 0, "C#": 1, "DB": 1, "D": 2, "D#": 3, "EB": 3,
    "E": 4, "FB": 4, "F": 5, "E#": 5, "F#": 6, "GB": 6, "G": 7,
    "G#": 8, "AB": 8, "A": 9, "A#": 10, "BB": 10, "B": 11, "CB": 11,
}
_NOTE_RE = re.compile(r"^\s*([A-Ga-g][#bB]?)(-?\d+)\s*$")


def _parse_midi_note(val: Any) -> int:
    if isinstance(val, (int, float)):
        return max(0, min(127, int(val)))
    s = str(val).strip()
    if re.fullmatch(r"-?\d+", s):
        return max(0, min(127, int(s)))
    m = _NOTE_RE.match(s)
    if not m:
        raise ValueError(f"Geçersiz MIDI nota değeri: {val!r}")
    pitch = _NOTE_SEMITONES[m.group(1).upper()]
    octave = int(m.group(2))
    midi_num = (octave + 1) * 12 + pitch
    return max(0, min(127, midi_num))


def _clamp7(val: Any, default: int = 0) -> int:
    if val is None:
        return default
    return max(0, min(127, int(val)))


def _resolve_channel0(spec: Dict[str, Any]) -> int:
    if "channel_0" in spec and spec["channel_0"] is not None:
        return max(0, min(15, int(spec["channel_0"])))
    ch_raw = spec.get("channel", spec.get("ch", 1))
    ch = int(ch_raw if ch_raw is not None else 1)
    # Webhook payloads commonly use 1..16 channels; accept 0 transparently as ch0.
    return 0 if ch <= 0 else max(0, min(15, ch - 1))


def _parse_hex_bytes(hex_str: str) -> List[int]:
    cleaned = re.sub(r"0[xX]", "", hex_str)
    tokens = [t for t in re.split(r"[\s,:;-]+", cleaned.strip()) if t]
    out: List[int] = []
    if len(tokens) == 1 and len(tokens[0]) > 2 and len(tokens[0]) % 2 == 0:
        pair_str = tokens[0]
        tokens = [pair_str[i : i + 2] for i in range(0, len(pair_str), 2)]
    for tok in tokens:
        out.append(max(0, min(255, int(tok, 16))))
    return out


def _single_midi_spec_to_event(spec: Any) -> Optional[Dict[str, Any]]:
    if spec is None:
        return None
    if isinstance(spec, str):
        raw = _parse_hex_bytes(spec)
        if not raw:
            return None
        return {"kind": "raw_hex", "bytes": raw, "hex": " ".join(f"{b:02X}" for b in raw), "channel_0": raw[0] & 0x0F if raw[0] < 0xF0 else 0, "note": None, "duration_ms": None}
    if isinstance(spec, list) and all(isinstance(x, int) for x in spec):
        raw = [max(0, min(255, int(x))) for x in spec]
        if not raw:
            return None
        return {"kind": "raw_bytes", "bytes": raw, "hex": " ".join(f"{b:02X}" for b in raw), "channel_0": raw[0] & 0x0F if raw[0] < 0xF0 else 0, "note": None, "duration_ms": None}
    if not isinstance(spec, dict):
        return None

    raw_bytes: Optional[List[int]] = None
    ch0 = _resolve_channel0(spec)
    note_val: Optional[int] = None
    dur_ms: Optional[float] = None
    kind = str(spec.get("type") or spec.get("event") or spec.get("midi_type") or "").strip().lower().replace("-", "_")

    if kind == "sysex" or (not kind and spec.get("sysex") is not None):
        kind = "sysex"
        src = None
        for key in ("data", "payload", "sysex", "bytes", "midi_bytes", "raw", "hex", "midi_hex", "raw_hex"):
            if spec.get(key) is not None:
                src = spec[key]
                break
        if isinstance(src, str):
            parsed = _parse_hex_bytes(src)
        elif isinstance(src, list):
            parsed = [max(0, min(255, int(x))) for x in src]
        else:
            parsed = []
        if parsed:
            if parsed[0] == 0xF0:
                parsed = parsed[1:]
            if parsed and parsed[-1] == 0xF7:
                parsed = parsed[:-1]
            raw_bytes = [0xF0] + [b & 0x7F for b in parsed] + [0xF7]
    elif spec.get("bytes") is not None or spec.get("midi_bytes") is not None or spec.get("raw") is not None:
        src = spec.get("bytes") if spec.get("bytes") is not None else (spec.get("midi_bytes") if spec.get("midi_bytes") is not None else spec.get("raw"))
        if isinstance(src, str):
            raw_bytes = _parse_hex_bytes(src)
        elif isinstance(src, list):
            raw_bytes = [max(0, min(255, int(x))) for x in src]
        kind = kind or "raw_bytes"
    elif spec.get("hex") or spec.get("midi_hex") or spec.get("raw_hex"):
        h = str(spec.get("hex") or spec.get("midi_hex") or spec.get("raw_hex"))
        raw_bytes = _parse_hex_bytes(h)
        kind = kind or "raw_hex"
    else:
        if not kind:
            if "note" in spec:
                vel_probe = spec.get("velocity", spec.get("vel", 100))
                kind = "note_off" if (vel_probe is not None and int(vel_probe) == 0) else "note_on"
            elif "cc" in spec or "controller" in spec:
                kind = "cc"
            elif "program" in spec or "pc" in spec:
                kind = "program_change"
            elif "pitch_bend" in spec or "pitch" in spec:
                kind = "pitch_bend"

        if kind in ("note_on", "noteon", "note"):
            note_val = _parse_midi_note(spec.get("note", 60))
            vel = _clamp7(spec.get("velocity", spec.get("vel", 100)), default=100)
            raw_bytes = [0x90 | ch0, note_val, vel]
            if spec.get("duration_ms") is not None and vel > 0:
                dur_ms = max(10.0, float(spec["duration_ms"]))
            kind = "note_on"
        elif kind in ("note_off", "noteoff"):
            note_val = _parse_midi_note(spec.get("note", 60))
            vel = _clamp7(spec.get("velocity", spec.get("vel", 0)), default=0)
            raw_bytes = [0x80 | ch0, note_val, vel]
            kind = "note_off"
        elif kind in ("cc", "control_change", "ctrl"):
            cc_num = _clamp7(spec.get("cc", spec.get("controller", 1)), default=1)
            val = _clamp7(spec.get("value", spec.get("val", 0)), default=0)
            raw_bytes = [0xB0 | ch0, cc_num, val]
            kind = "cc"
        elif kind in ("pc", "program_change", "program"):
            prog = _clamp7(spec.get("program", spec.get("pc", spec.get("value", 0))), default=0)
            raw_bytes = [0xC0 | ch0, prog]
            kind = "program_change"
        elif kind in ("pitch_bend", "pitchwheel", "pitch"):
            raw_src: Any = 8192
            for key in ("value", "pitch_bend", "pitch"):
                if key in spec and spec[key] is not None:
                    raw_src = spec[key]
                    break
            if isinstance(raw_src, float) and -1.0 <= raw_src <= 1.0:
                val14 = int(round((raw_src + 1.0) * 8191.5))
            else:
                raw_v = int(raw_src)
                val14 = (raw_v + 8192) if raw_v < 0 else raw_v
            val14 = max(0, min(16383, val14))
            raw_bytes = [0xE0 | ch0, val14 & 0x7F, (val14 >> 7) & 0x7F]
            kind = "pitch_bend"
        elif kind in ("aftertouch", "channel_pressure"):
            val = _clamp7(spec.get("value", spec.get("pressure", 64)), default=64)
            raw_bytes = [0xD0 | ch0, val]
            kind = "channel_pressure"
        elif kind == "poly_aftertouch":
            note_val = _parse_midi_note(spec.get("note", 60))
            val = _clamp7(spec.get("value", spec.get("pressure", 64)), default=64)
            raw_bytes = [0xA0 | ch0, note_val, val]
        elif kind == "clock":
            raw_bytes = [0xF8]
        elif kind == "start":
            raw_bytes = [0xFA]
        elif kind == "continue":
            raw_bytes = [0xFB]
        elif kind == "stop":
            raw_bytes = [0xFC]

    if not raw_bytes:
        return None
    return {
        "kind": kind or "raw",
        "bytes": raw_bytes,
        "hex": " ".join(f"{b:02X}" for b in raw_bytes),
        "channel_0": ch0,
        "note": note_val,
        "duration_ms": dur_ms,
    }


def normalize_midi_messages(payload: Any) -> List[Dict[str, Any]]:
    """Extract normalized CoreMIDI byte sequences from heterogeneous webhook JSON payloads."""
    if payload is None:
        return []
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        if "midi" in payload:
            m = payload["midi"]
            if isinstance(m, list):
                items = m
            elif isinstance(m, dict) and isinstance(m.get("events"), list):
                items = m["events"]
            else:
                items = [m]
        elif isinstance(payload.get("events"), list):
            items = payload["events"]
        elif isinstance(payload.get("messages"), list):
            items = payload["messages"]
        else:
            items = [payload]
    else:
        return []

    out: List[Dict[str, Any]] = []
    for item in items:
        ev = _single_midi_spec_to_event(item)
        if ev is not None:
            out.append(ev)
    return out


class IacMidiRouter:
    """Thread-safe persistent CoreMIDI pipe router targeting IAC Driver Bus 1."""

    def __init__(self, dest_pattern: str = DEFAULT_MIDI_DEST, helper_bin: Path = DEFAULT_IAC_BIN) -> None:
        self.dest_pattern = dest_pattern
        self.helper_bin = Path(helper_bin)
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen[str]] = None
        self.destination_name: Optional[str] = None
        self._timers: List[threading.Timer] = []

    def ensure_ready(self) -> Dict[str, Any]:
        if not self.helper_bin.is_file():
            raise FileNotFoundError(
                f"mpc_iac_midi bulunamadı: {self.helper_bin}. Derlemek için scripts/auto_webhook_iac.sh çalıştırın."
            )
        proc = subprocess.run(
            [str(self.helper_bin), "--ensure-iac", "--dest", self.dest_pattern],
            capture_output=True,
            text=True,
            timeout=6,
            check=True,
        )
        info = json.loads(proc.stdout.strip())
        if info.get("iac_destination"):
            self.destination_name = str(info["iac_destination"])
        return info

    def _start_pipe_locked(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        self.ensure_ready()
        proc = subprocess.Popen(
            [str(self.helper_bin), "--pipe", "--dest", self.dest_pattern],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        banner_line = proc.stdout.readline().strip()
        if not banner_line:
            err = proc.stderr.read().strip() if proc.stderr else "empty stdout"
            proc.kill()
            raise RuntimeError(f"mpc_iac_midi pipe başlatılamadı: {err}")
        banner = json.loads(banner_line)
        self.destination_name = str(banner.get("destination") or self.destination_name or self.dest_pattern)
        self._proc = proc

    def send_bytes(self, raw_bytes: List[int]) -> Dict[str, Any]:
        hex_cmd = " ".join(f"{b & 0xFF:02X}" for b in raw_bytes)
        with self._lock:
            for attempt in range(2):
                self._start_pipe_locked()
                proc = self._proc
                assert proc is not None and proc.stdin is not None and proc.stdout is not None
                try:
                    proc.stdin.write(hex_cmd + "\n")
                    proc.stdin.flush()
                    resp_line = proc.stdout.readline().strip()
                    if not resp_line:
                        raise BrokenPipeError("mpc_iac_midi pipe closed")
                    res = json.loads(resp_line)
                    res["destination"] = self.destination_name
                    return res
                except Exception:
                    self._close_proc_locked(graceful=False)
                    if attempt == 1:
                        raise
        raise RuntimeError("MIDI gönderimi başarısız")

    def schedule_note_off(self, channel_0: int, note: int, delay_ms: float) -> None:
        ch0 = max(0, min(15, int(channel_0)))
        n = max(0, min(127, int(note)))

        def _fire() -> None:
            try:
                self.send_bytes([0x80 | ch0, n, 0])
            except Exception:
                pass

        t = threading.Timer(delay_ms / 1000.0, _fire)
        t.daemon = True
        t.start()
        with self._lock:
            self._timers = [tm for tm in self._timers if tm.is_alive()]
            self._timers.append(t)

    def list_endpoints(self) -> Dict[str, Any]:
        proc = subprocess.run(
            [str(self.helper_bin), "--list"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return json.loads(proc.stdout.strip())

    def _close_proc_locked(self, graceful: bool = True) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if graceful and proc.stdin:
                proc.stdin.write("QUIT\n")
                proc.stdin.flush()
            proc.wait(timeout=1.5 if graceful else 0.2)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=0.5)
            except Exception:
                pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        self._proc = None

    def close(self) -> None:
        with self._lock:
            for tm in self._timers:
                tm.cancel()
            self._timers.clear()
            self._close_proc_locked(graceful=True)


class WebhookService:
    """Core handler: persist webhook data to watched inbox + forward MIDI to IAC + emit telemetry."""

    def __init__(
        self,
        inbox_dir: Path = DEFAULT_INBOX_DIR,
        router: Optional[IacMidiRouter] = None,
    ) -> None:
        self.inbox_dir = Path(inbox_dir).expanduser().resolve()
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.router = router if router is not None else IacMidiRouter()
        self._lock = threading.Lock()
        self.received_count = 0
        self.midi_sent_count = 0
        self.recent_events: Deque[Dict[str, Any]] = deque(maxlen=60)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "ok": True,
                "inbox_dir": str(self.inbox_dir),
                "iac_destination": self.router.destination_name or self.router.dest_pattern,
                "received_count": self.received_count,
                "midi_sent_count": self.midi_sent_count,
                "recent_events": list(self.recent_events),
            }

    def handle_webhook(
        self,
        req_path: str,
        headers: Dict[str, str],
        raw_body: bytes,
        remote_addr: str = "127.0.0.1",
    ) -> Dict[str, Any]:
        now_utc = datetime.now(timezone.utc)
        parsed_body: Any
        if not raw_body:
            parsed_body = {}
        else:
            try:
                parsed_body = json.loads(raw_body.decode("utf-8"))
            except Exception:
                try:
                    parsed_body = {"raw_text": raw_body.decode("utf-8")}
                except UnicodeDecodeError:
                    parsed_body = {"raw_base64": base64.b64encode(raw_body).decode("ascii")}

        midi_events = normalize_midi_messages(parsed_body)
        midi_results: List[Dict[str, Any]] = []
        for ev in midi_events:
            send_res = self.router.send_bytes(ev["bytes"])
            if ev.get("duration_ms") and ev.get("note") is not None:
                self.router.schedule_note_off(ev["channel_0"], ev["note"], float(ev["duration_ms"]))
            midi_results.append({"kind": ev["kind"], "hex": ev["hex"], "duration_ms": ev.get("duration_ms"), **send_res})

        flow_result: Optional[Dict[str, Any]] = None
        if isinstance(parsed_body, dict):
            action_name = str(parsed_body.get("action") or "").strip().lower()
            if req_path.rstrip("/") == "/flow" and not action_name:
                action_name = "flow_select" if ("slot" in parsed_body and not parsed_body.get("prompt")) else "flow_generate"
            if action_name in ("flow_generate", "flow_select"):
                try:
                    from . import mpc_flow
                except ImportError:
                    import mpc_flow  # type: ignore

                if action_name == "flow_select":
                    flow_result = mpc_flow.select_bank(
                        slot=int(parsed_body.get("slot", 1)),
                        deploy=bool(parsed_body.get("deploy", True)),
                        host=parsed_body.get("host"),
                    )
                else:
                    prompt = str(parsed_body.get("prompt") or "").strip()
                    if prompt:
                        gen_kwargs = {
                            "prompt": prompt,
                            "slot": int(parsed_body.get("slot", 1)),
                            "bpm": float(parsed_body["bpm"]) if parsed_body.get("bpm") not in (None, "") else None,
                            "key": str(parsed_body["key"]).strip() if parsed_body.get("key") else None,
                            "bars": int(parsed_body["bars"]) if parsed_body.get("bars") not in (None, "") else None,
                            "engine": str(parsed_body["engine"]).strip() if parsed_body.get("engine") else None,
                            "deploy": bool(parsed_body.get("deploy", True)),
                            "host": parsed_body.get("host"),
                        }
                        if bool(parsed_body.get("sync", True)):
                            flow_result = mpc_flow.generate_sample_bank(**gen_kwargs)
                        else:
                            threading.Thread(
                                target=mpc_flow.generate_sample_bank,
                                kwargs=gen_kwargs,
                                daemon=True,
                            ).start()
                            flow_result = {"ok": True, "queued": True, "slot": gen_kwargs["slot"], "prompt": prompt}

        with self._lock:
            self.received_count += 1
            seq = self.received_count
            self.midi_sent_count += len(midi_results)

        slug_src = req_path.strip("/").replace("/", "_") or "webhook"
        slug = re.sub(r"[^A-Za-z0-9_-]+", "_", slug_src).strip("_") or "webhook"
        stamp = now_utc.strftime("%Y%m%dT%H%M%S_%fZ")
        filename = f"{stamp}_{seq:04d}_{slug[:28]}.json"
        saved_path = self.inbox_dir / filename

        record = {
            "seq": seq,
            "received_at": now_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "path": req_path,
            "remote_addr": remote_addr,
            "headers": {k.lower(): v for k, v in headers.items() if k.lower() in ("content-type", "user-agent", "x-webhook-event", "x-source")},
            "payload": parsed_body,
            "midi_routed": midi_results,
            **({"flow_result": flow_result} if flow_result is not None else {}),
        }
        saved_path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        msg_preview = ""
        if isinstance(parsed_body, dict):
            msg_preview = str(parsed_body.get("message") or parsed_body.get("text") or parsed_body.get("event") or parsed_body.get("source") or "")
        detail_parts = [filename]
        if msg_preview:
            detail_parts.append(f'msg="{msg_preview[:80]}"')
        if midi_results:
            hexes = ", ".join(r["hex"] for r in midi_results[:4])
            if len(midi_results) > 4:
                hexes += f" (+{len(midi_results) - 4})"
            detail_parts.append(f"IAC[{hexes}]")
        summary = " | ".join(detail_parts)

        target_label = f"iac:{self.router.destination_name or 'Bus1'}" if midi_results else f"inbox:{self.inbox_dir.name}"
        emit_activity("webhook", "midi_route" if midi_results else "recv", summary, target=target_label)

        event_entry = {
            "seq": seq,
            "ts": record["received_at"],
            "path": req_path,
            "saved_file": str(saved_path),
            "midi_count": len(midi_results),
            "summary": summary,
        }
        with self._lock:
            self.recent_events.append(event_entry)

        print(f"[{record['received_at']}] WEBHOOK #{seq} {req_path} -> {summary}", flush=True)
        return {
            "ok": True,
            "seq": seq,
            "saved_file": str(saved_path),
            "inbox_dir": str(self.inbox_dir),
            "iac_destination": self.router.destination_name or self.router.dest_pattern,
            "midi_routed": len(midi_results),
            "midi_results": midi_results,
            "flow": flow_result,
            "message": msg_preview or None,
        }


def make_http_server(service: WebhookService, bind: str = DEFAULT_BIND, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    class _Handler(BaseHTTPRequestHandler):
        def _send_json(self, code: int, obj: Any) -> None:
            raw = (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format: str, *args: Any) -> None:
            # Structured logs are emitted by WebhookService directly.
            return

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Webhook-Event, X-Source")
            self.end_headers()

        def do_GET(self) -> None:
            clean = self.path.split("?", 1)[0]
            if clean in ("/", "/health", "/status", "/api/status"):
                st = service.status()
                st["bind"] = self.server.server_address[0]
                st["port"] = self.server.server_address[1]
                self._send_json(200, st)
                return
            if clean == "/events":
                self._send_json(200, {"ok": True, "events": list(service.status()["recent_events"])})
                return
            if clean == "/midi/destinations":
                try:
                    self._send_json(200, service.router.list_endpoints())
                except Exception as exc:
                    self._send_json(500, {"ok": False, "error": str(exc)})
                return
            self._send_json(404, {"ok": False, "error": f"Unknown endpoint: {clean}"})

        def do_POST(self) -> None:
            clean = self.path.split("?", 1)[0]
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self._send_json(400, {"ok": False, "error": "Invalid Content-Length"})
                return
            if length < 0 or length > MAX_BODY_BYTES:
                self._send_json(413, {"ok": False, "error": f"Payload exceeds {MAX_BODY_BYTES} bytes"})
                return
            raw_body = self.rfile.read(length) if length > 0 else b""
            try:
                res = service.handle_webhook(
                    req_path=clean,
                    headers=dict(self.headers.items()),
                    raw_body=raw_body,
                    remote_addr=str(self.client_address[0]),
                )
                self._send_json(200, res)
            except Exception as exc:
                emit_activity("webhook", "error", f"{clean}: {exc}", level="error")
                self._send_json(500, {"ok": False, "error": str(exc)})

    class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
        allow_reuse_address = True

    return _ReusableThreadingHTTPServer((bind, port), _Handler)


def get_webhook_health(url: str = f"http://{DEFAULT_BIND}:{DEFAULT_PORT}/health", timeout_s: float = 3.0) -> Dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        return {"ok": False, "error": str(exc), "url": url}


def send_test_webhook(
    url: str,
    payload: Dict[str, Any],
    timeout_s: float = 5.0,
) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "MPCUTER-WebhookClient/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run_webhook_cli(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="MPCUTER Webhook -> Watched Folder -> IAC Driver Bus 1 Köprüsü")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_srv = sub.add_parser("serve", help="Arka planda/ön planda yerel webhook sunucusunu başlat")
    p_srv.add_argument("--bind", default=DEFAULT_BIND)
    p_srv.add_argument("--port", type=int, default=DEFAULT_PORT)
    p_srv.add_argument("--inbox-dir", type=Path, default=DEFAULT_INBOX_DIR)
    p_srv.add_argument("--dest", default=DEFAULT_MIDI_DEST, help="Hedef CoreMIDI portu (varsayılan: IAC)")

    p_st = sub.add_parser("status", help="Çalışan webhook sunucusunun durumunu kontrol et")
    p_st.add_argument("--url", default=f"http://{DEFAULT_BIND}:{DEFAULT_PORT}/health")

    p_snd = sub.add_parser("send", help="Yerel webhook sunucusuna mesaj / MIDI gönder")
    p_snd.add_argument("--url", default=f"http://{DEFAULT_BIND}:{DEFAULT_PORT}/webhook")
    p_snd.add_argument("--message", default="AntiGravity Webhook Test")
    p_snd.add_argument("--note", default=None, help="Örn: 60 veya C4")
    p_snd.add_argument("--velocity", type=int, default=100)
    p_snd.add_argument("--cc", type=int, default=None)
    p_snd.add_argument("--value", type=int, default=None)
    p_snd.add_argument("--channel", type=int, default=1)
    p_snd.add_argument("--duration-ms", type=float, default=150.0)
    p_snd.add_argument("--hex", dest="raw_hex", default=None, help="Ham MIDI hex baytları, ör. '90 3C 64'")

    sub.add_parser("midi-list", help="IAC Driver Bus 1 ve tüm CoreMIDI portlarını listele")

    raw_argv = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(raw_argv)

    if args.cmd == "serve":
        router = IacMidiRouter(dest_pattern=args.dest)
        iac_info = router.ensure_ready()
        svc = WebhookService(inbox_dir=args.inbox_dir, router=router)
        srv = make_http_server(svc, bind=args.bind, port=args.port)
        emit_activity(
            "webhook",
            "start",
            f"http://{args.bind}:{args.port}/webhook -> inbox:{svc.inbox_dir.name} -> {iac_info.get('iac_destination', args.dest)}",
            target="iac:Bus1",
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "listening": f"http://{args.bind}:{args.port}/webhook",
                    "health": f"http://{args.bind}:{args.port}/health",
                    "inbox_dir": str(svc.inbox_dir),
                    "iac_destination": iac_info.get("iac_destination"),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            srv.server_close()
            router.close()
        return 0

    if args.cmd == "status":
        res = get_webhook_health(args.url)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("ok") else 1

    if args.cmd == "send":
        payload: Dict[str, Any] = {"message": args.message, "source": "cli"}
        midi_list: List[Dict[str, Any]] = []
        if args.raw_hex:
            midi_list.append({"hex": args.raw_hex})
        if args.note is not None:
            midi_list.append(
                {
                    "type": "note_on",
                    "note": args.note,
                    "velocity": args.velocity,
                    "channel": args.channel,
                    "duration_ms": args.duration_ms,
                }
            )
        if args.cc is not None:
            midi_list.append(
                {
                    "type": "cc",
                    "cc": args.cc,
                    "value": args.value if args.value is not None else 64,
                    "channel": args.channel,
                }
            )
        if midi_list:
            payload["midi"] = midi_list
        res = send_test_webhook(args.url, payload)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "midi-list":
        router = IacMidiRouter()
        router.ensure_ready()
        print(json.dumps(router.list_endpoints(), indent=2, ensure_ascii=False))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(run_webhook_cli())
