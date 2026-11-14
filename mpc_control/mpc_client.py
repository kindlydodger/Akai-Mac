#!/usr/bin/env python3
"""Shared client, map manager, and MIDI decoder for Akai MPC Live control."""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import shlex
import socket
import subprocess
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


DEFAULT_MAP_PATH = Path(__file__).resolve().parent / "mpc_live_map.json"
ACTIVITY_LOG_PATH = Path(
    os.environ.get("MPC_ACTIVITY_LOG") or str(Path(__file__).resolve().parent / "activity_feed.jsonl")
)
_ACTIVITY_LOCK = threading.Lock()
_ACTIVITY_MAX_BYTES = 512 * 1024
_HOST_CACHE_LOCK = threading.Lock()
_HOST_IP_CACHE: Dict[str, Tuple[str, float]] = {}


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip())
        return True
    except ValueError:
        return False


def invalidate_mpc_host_cache(host: Optional[str] = None) -> None:
    """Clear cached IPv4 resolution for one host or all hosts."""
    with _HOST_CACHE_LOCK:
        if host is None:
            _HOST_IP_CACHE.clear()
        else:
            _HOST_IP_CACHE.pop(host.strip(), None)


def resolve_mpc_host(host: str, force_refresh: bool = False, ttl_s: float = 15.0) -> str:
    """Resolve hostnames (such as mpc-live.local) via IPv4 to avoid macOS AAAA mDNS stalls."""
    cleaned = (host or "").strip() or "mpc-live.local"
    if _is_ip_literal(cleaned):
        return cleaned
    now = time.monotonic()
    with _HOST_CACHE_LOCK:
        cached = _HOST_IP_CACHE.get(cleaned)
        if not force_refresh and cached is not None and (now - cached[1]) < ttl_s:
            return cached[0]
    try:
        # AF_INET avoids ~1.7s mDNS IPv6 AAAA wait when connecting to .local targets.
        ip = socket.getaddrinfo(cleaned, None, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    except OSError:
        return cleaned
    with _HOST_CACHE_LOCK:
        _HOST_IP_CACHE[cleaned] = (ip, time.monotonic())
    return ip


def emit_activity(
    source: str,
    action: str,
    detail: str = "",
    target: str = "mpc-live",
    level: str = "info",
    extra: Optional[Dict[str, Any]] = None,
    log_path: Optional[Path] = None,
) -> None:
    """Append a structured JSONL activity event for live terminal monitoring."""
    path = Path(log_path) if log_path is not None else Path(os.environ.get("MPC_ACTIVITY_LOG") or ACTIVITY_LOG_PATH)
    now = datetime.now()
    entry: Dict[str, Any] = {
        "ts": round(time.time(), 3),
        "iso": now.strftime("%H:%M:%S"),
        "source": str(source or "mpc_client"),
        "target": str(target or "mpc-live"),
        "level": str(level or "info"),
        "action": str(action),
        "detail": str(detail),
    }
    if extra:
        entry["extra"] = extra
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _ACTIVITY_LOCK:
            if path.exists() and path.stat().st_size > _ACTIVITY_MAX_BYTES:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
                path.write_text("".join(lines[-800:]), encoding="utf-8")
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError:
        # Telemetry must never interrupt hardware control.
        return


def deploy_file_to_mpc(
    local_path: Path,
    host: str,
    remote_dir: str = "/media/okcomputer/MPCUTER_Audio",
    ssh_user: str = "root",
    source: str = "deploy",
    action: str = "deploy_file",
) -> Dict[str, Any]:
    """Upload a local file directly to MPC storage over SSH/SCP and log telemetry."""
    p = Path(local_path)
    remote_target = f"{remote_dir.rstrip('/')}/{p.name}"
    subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", f"{ssh_user}@{host}", f"mkdir -p {shlex.quote(remote_dir)}"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["scp", "-o", "ConnectTimeout=5", str(p), f"{ssh_user}@{host}:{remote_target}"],
        check=True,
        capture_output=True,
        text=True,
    )
    size = p.stat().st_size
    emit_activity(source, action, f"{p.name} ({size}B) -> {remote_target}", target=f"mpc:{host}")
    return {"deployed": True, "host": host, "remote_path": remote_target, "bytes": size}


def _normalize_key(name: str) -> str:
    return name.strip().upper().replace(" ", "_").replace("-", "_")


def _encode_rel_7bit(step: int) -> int:
    """Encode a signed non-zero delta (-63..+63) as 7-bit two's complement."""
    if step > 0:
        return min(step, 63)
    return 0x80 - min(-step, 63)


def decode_midi_bytes(raw: bytes) -> Dict[str, Any]:
    """Decode a single raw MIDI packet from hw:1,0,1 into structured fields."""
    hex_str = " ".join(f"{b:02X}" for b in raw)
    if not raw:
        return {"type": "empty", "hex": hex_str}

    status = raw[0]
    if status == 0xF0:
        return {"type": "sysex", "hex": hex_str, "length": len(raw)}

    msg_type = status & 0xF0
    ch = status & 0x0F
    d1 = raw[1] if len(raw) > 1 else None
    d2 = raw[2] if len(raw) > 2 else None

    if msg_type == 0x90 and d1 is not None and d2 is not None:
        return {
            "type": "note_off" if d2 == 0 else "note_on",
            "channel": ch,
            "note": d1,
            "velocity": d2,
            "hex": hex_str,
        }
    if msg_type == 0x80 and d1 is not None:
        return {"type": "note_off", "channel": ch, "note": d1, "velocity": d2 or 0, "hex": hex_str}
    if msg_type == 0xA0 and d1 is not None and d2 is not None:
        return {"type": "poly_aftertouch", "channel": ch, "note": d1, "pressure": d2, "hex": hex_str}
    if msg_type == 0xB0 and d1 is not None and d2 is not None:
        rel = d2 if d2 < 64 else d2 - 128
        return {"type": "cc", "channel": ch, "cc": d1, "value": d2, "relative": rel, "hex": hex_str}
    return {"type": "raw", "status": status, "hex": hex_str}


def is_noise_event(ev: Dict[str, Any]) -> bool:
    """Return True for periodic MPC keepalive SysEx and Tap Tempo LED blink traffic."""
    hx = ev.get("hex", "")
    # Akai MPC Live periodic keepalive telemetry SysEx + Tap Tempo blink B0 35
    if hx.startswith("F0 47 7F 3B 53 00 05 6E 6F 63"):
        return True
    if ev.get("dir") == "TX" and hx.startswith("B0 35 "):
        return True
    return False


class MpcMap:
    """Loads, queries, and updates mpc_live_map.json."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else DEFAULT_MAP_PATH
        self.reload()

    def reload(self) -> None:
        with self.path.open("r", encoding="utf-8") as fh:
            self.data: Dict[str, Any] = json.load(fh)
        self._build_indices()

    def _build_indices(self) -> None:
        self.buttons: Dict[str, Dict[str, Any]] = {
            _normalize_key(k): v for k, v in self.data.get("buttons", {}).items()
        }
        self.aliases: Dict[str, str] = {
            _normalize_key(k): _normalize_key(v) for k, v in self.data.get("aliases", {}).items()
        }
        # Allow literal "-" and "+" as button names without breaking normalization.
        for raw_k, target in self.data.get("aliases", {}).items():
            if raw_k in ("-", "+"):
                self.aliases[raw_k] = _normalize_key(target)

        self.combos: Dict[str, List[str]] = {
            _normalize_key(k): [_normalize_key(x) for x in v]
            for k, v in self.data.get("combos", {}).items()
        }
        self.learned: Dict[str, Dict[str, Any]] = {
            _normalize_key(k): v for k, v in self.data.get("learned", {}).items()
        }
        self.pads_by_num: Dict[int, Dict[str, Any]] = {
            int(item["pad"]): item for item in self.data.get("pads", {}).get("items", [])
        }
        self.reserved_notes = set(int(x) for x in self.data.get("meta", {}).get("reserved_unsafe_notes", [88]))

    def save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, sort_keys=False)
            fh.write("\n")
        tmp.replace(self.path)
        self._build_indices()

    def resolve_button(self, name: str) -> Dict[str, Any]:
        raw = name.strip()
        if raw in self.aliases:
            canonical = self.aliases[raw]
        else:
            norm = _normalize_key(raw)
            canonical = self.aliases.get(norm, norm)

        if canonical in self.learned and "note" in self.learned[canonical]:
            entry = dict(self.learned[canonical])
            entry.setdefault("channel", 0)
            entry["name"] = canonical
            return entry

        if canonical in self.buttons:
            entry = dict(self.buttons[canonical])
            entry.setdefault("channel", 0)
            entry["name"] = canonical
            return entry

        raise ValueError(
            f"Unknown button '{name}'. Use list_controls or mpc_learn to inspect/teach controls."
        )

    def resolve_combo(self, name: str) -> Optional[Tuple[str, str]]:
        norm = _normalize_key(name)
        seq = self.combos.get(norm)
        if seq and len(seq) == 2:
            return seq[0], seq[1]
        return None

    def resolve_pad(self, pad_id: Any) -> Dict[str, Any]:
        if isinstance(pad_id, str):
            s = _normalize_key(pad_id).lstrip("PAD_")
            if s.isdigit():
                num = int(s)
            else:
                raise ValueError(f"Invalid pad identifier '{pad_id}'; expected 1..16 or PAD_1..PAD_16")
        else:
            num = int(pad_id)
        if num not in self.pads_by_num:
            raise ValueError(f"Pad {num} out of range (valid: 1..16)")
        return self.pads_by_num[num]

    def resolve_ui(self, target: str) -> Tuple[int, int]:
        """Resolve 'section.element' (e.g. 'step_seq.tc_btn') from map ui coordinates."""
        parts = [p.strip().lower() for p in target.split(".") if p.strip()]
        if len(parts) != 2:
            raise ValueError(f"Expected 'screen.element' UI target, got '{target}'")
        ui = self.data.get("ui", {})
        coords = ui.get(parts[0], {}).get(parts[1])
        if not isinstance(coords, list) or len(coords) != 2:
            raise ValueError(f"Unknown UI coordinate target '{target}'")
        return int(coords[0]), int(coords[1])

    def summary(self) -> Dict[str, Any]:
        return {
            "device": self.data.get("meta", {}).get("device"),
            "hw_port": self.data.get("meta", {}).get("hw_port"),
            "buttons": {k: v for k, v in sorted(self.buttons.items())},
            "aliases": self.data.get("aliases", {}),
            "combos": self.combos,
            "pads": self.data.get("pads", {}),
            "dial": self.data.get("dial", {}),
            "qlinks": self.data.get("qlinks", {}),
            "learned": self.learned,
        }


class MpcBridgeClient:
    """Low-latency TCP client for libmpc_hw_bridge.so (:6731) + HTTP screen (:6720)."""

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        remote_port: Optional[int] = None,
        map_path: Optional[Path] = None,
        timeout: float = 5.0,
        source: Optional[str] = None,
    ) -> None:
        self.map = MpcMap(map_path)
        meta = self.map.data.get("meta", {})
        self.host = host or os.environ.get("MPC_HOST") or meta.get("default_host", "mpc-live.local")
        self.port = int(port or os.environ.get("MPC_BRIDGE_PORT") or meta.get("bridge_port", 6731))
        self.remote_port = int(remote_port or os.environ.get("MPC_REMOTE_PORT") or meta.get("remote_port", 6720))
        self.timeout = timeout
        self.source = source or os.environ.get("MPC_ACTIVITY_SOURCE") or "mpc_client"
        self._http_lock = threading.Lock()
        self._http_conn: Optional[http.client.HTTPConnection] = None

    def resolve_host(self, force_refresh: bool = False) -> str:
        """Resolve configured MPC host to its active IPv4 address (cached briefly for speed)."""
        return resolve_mpc_host(self.host, force_refresh=force_refresh)

    def _log(self, action: str, detail: str = "", level: str = "info", extra: Optional[Dict[str, Any]] = None) -> None:
        emit_activity(self.source, action, detail, target=f"mpc:{self.host}", level=level, extra=extra)

    def _remote_request(self, method: str, path: str, body: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None) -> bytes:
        with self._http_lock:
            is_literal = _is_ip_literal(self.host)
            for attempt in range(2):
                target = self.resolve_host(force_refresh=(attempt == 1 and not is_literal))
                try:
                    if self._http_conn is None or getattr(self._http_conn, "host", None) != target:
                        if self._http_conn is not None:
                            try:
                                self._http_conn.close()
                            except Exception:
                                pass
                        self._http_conn = http.client.HTTPConnection(target, self.remote_port, timeout=self.timeout)
                    self._http_conn.request(method, path, body=body, headers=headers or {})
                    resp = self._http_conn.getresponse()
                    data = resp.read()
                    if resp.status >= 400:
                        raise RuntimeError(f"Remote HTTP {resp.status} for {path}: {data.decode('utf-8', errors='replace')}")
                    return data
                except (http.client.HTTPException, OSError):
                    if self._http_conn is not None:
                        try:
                            self._http_conn.close()
                        except Exception:
                            pass
                        self._http_conn = None
                    if attempt == 1:
                        raise
            return b""

    def _connect(self) -> Tuple[socket.socket, Any]:
        is_literal = _is_ip_literal(self.host)
        attempts = 1 if is_literal else 2
        for attempt in range(attempts):
            target = self.resolve_host(force_refresh=(attempt == 1))
            try:
                sock = socket.create_connection((target, self.port), timeout=self.timeout)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                fh = sock.makefile("rwb", buffering=0)
                return sock, fh
            except OSError:
                if attempt + 1 >= attempts:
                    raise
        raise RuntimeError("Failed to connect to mpc_hw_bridge")

    def _cmd(self, fh: Any, line: str) -> str:
        fh.write((line + "\n").encode("utf-8"))
        resp = fh.readline().decode("utf-8", errors="replace").strip()
        if not resp:
            raise ConnectionError("Empty response from mpc_hw_bridge")
        return resp

    def _guard_packet(self, data: bytes, allow_unsafe: bool = False) -> None:
        if not data or len(data) > 256:
            raise ValueError(f"Packet length must be 1..256 bytes, got {len(data)}")
        if not allow_unsafe and len(data) >= 2 and (data[0] & 0xF0) in (0x80, 0x90):
            if (data[0] & 0x0F) == 0 and data[1] in self.map.reserved_notes:
                raise ValueError(f"Refusing reserved unsafe note 0x{data[1]:02X} on ch 0 (exits MPC)")

    def _inject(self, fh: Any, data: bytes, allow_unsafe: bool = False) -> int:
        self._guard_packet(data, allow_unsafe=allow_unsafe)
        hx = " ".join(f"{b:02X}" for b in data)
        resp = self._cmd(fh, f"INJECT {hx}")
        if not resp.startswith("OK"):
            raise RuntimeError(f"Bridge rejected INJECT {hx}: {resp}")
        return int(resp.split()[1])

    def status(self, check_screen: bool = True) -> Dict[str, Any]:
        sock, fh = self._connect()
        try:
            raw = self._cmd(fh, "PING")
        finally:
            fh.close()
            sock.close()
        parts = raw.split()
        resolved_ip = self.resolve_host()
        info: Dict[str, Any] = {
            "raw": raw,
            "host": self.host,
            "resolved_ip": resolved_ip,
            "bridge_port": self.port,
            "ok": raw.startswith("OK"),
        }
        for token in parts[3:]:
            if "=" in token:
                k, v = token.split("=", 1)
                info[k] = int(v) if v.isdigit() else v
        info["ready"] = bool(info.get("ok") and info.get("priv_in") not in (None, "(nil)", "0x0"))
        if check_screen:
            try:
                url = f"http://{resolved_ip}:{self.remote_port}/screen?half=1"
                with urllib.request.urlopen(url, timeout=2.0) as resp:
                    sig = resp.read(8)
                    info["screen_http"] = resp.status == 200 and sig.startswith(b"\x89PNG")
            except Exception as exc:
                info["screen_http"] = False
                info["screen_error"] = str(exc)
        return info

    def press_button(
        self,
        name: str,
        action: str = "tap",
        hold_ms: int = 60,
        release_mode: str = "90_00",
    ) -> Dict[str, Any]:
        action = action.lower()
        if action not in ("tap", "down", "hold", "up", "release"):
            raise ValueError("action must be tap, down/hold, or up/release")

        # Support combo names directly via button tool (e.g. REDO, BANK_E)
        combo_pair = self.map.resolve_combo(name)
        if combo_pair and action == "tap":
            return self.combo(combo_pair[0], combo_pair[1], hold_ms=hold_ms, release_mode=release_mode)

        btn = self.map.resolve_button(name)
        ch = int(btn.get("channel", 0)) & 0x0F
        note = int(btn["note"]) & 0x7F
        down_pkt = bytes([0x90 | ch, note, 0x7F])
        up_pkt = bytes([0x90 | ch, note, 0x00]) if release_mode == "90_00" else bytes([0x80 | ch, note, 0x40])

        sock, fh = self._connect()
        try:
            if action in ("down", "hold"):
                self._inject(fh, down_pkt)
            elif action in ("up", "release"):
                self._inject(fh, up_pkt)
            else:
                self._inject(fh, down_pkt)
                time.sleep(max(10, hold_ms) / 1000.0)
                self._inject(fh, up_pkt)
        finally:
            fh.close()
            sock.close()

        self._log("button", f"{btn['name']} ({action}, 0x{note:02X})")
        return {"button": btn["name"], "note": note, "hex": f"0x{note:02X}", "action": action, "hold_ms": hold_ms}

    def combo(
        self,
        modifier: str,
        target: str,
        hold_ms: int = 60,
        release_mode: str = "90_00",
    ) -> Dict[str, Any]:
        mod = self.map.resolve_button(modifier)
        tgt = self.map.resolve_button(target)
        m_ch, m_note = int(mod.get("channel", 0)) & 0x0F, int(mod["note"]) & 0x7F
        t_ch, t_note = int(tgt.get("channel", 0)) & 0x0F, int(tgt["note"]) & 0x7F

        def up_bytes(ch: int, note: int) -> bytes:
            return bytes([0x90 | ch, note, 0x00]) if release_mode == "90_00" else bytes([0x80 | ch, note, 0x40])

        sock, fh = self._connect()
        try:
            self._inject(fh, bytes([0x90 | m_ch, m_note, 0x7F]))
            time.sleep(0.03)
            self._inject(fh, bytes([0x90 | t_ch, t_note, 0x7F]))
            time.sleep(max(20, hold_ms) / 1000.0)
            self._inject(fh, up_bytes(t_ch, t_note))
            time.sleep(0.03)
            self._inject(fh, up_bytes(m_ch, m_note))
        finally:
            fh.close()
            sock.close()

        self._log("combo", f"{mod['name']} + {tgt['name']}")
        return {"modifier": mod["name"], "target": tgt["name"], "hold_ms": hold_ms}

    def switch_bank(self, bank: str) -> Dict[str, Any]:
        b = bank.strip().upper()
        if b in ("A", "B", "C", "D"):
            return self.press_button(f"BANK_{b}")
        if b in ("E", "F", "G", "H"):
            base = {"E": "BANK_A", "F": "BANK_B", "G": "BANK_C", "H": "BANK_D"}[b]
            return self.combo("SHIFT", base)
        raise ValueError(f"Invalid bank '{bank}'; expected A..H")

    def trigger_pads(
        self,
        pads: Iterable[Any],
        velocity: int = 100,
        duration_ms: int = 100,
        aftertouch: Optional[int] = None,
        bank: Optional[str] = None,
        gap_ms: int = 0,
    ) -> Dict[str, Any]:
        pad_entries = [self.map.resolve_pad(p) for p in pads]
        if not pad_entries:
            raise ValueError("At least one pad must be specified")
        vel = max(1, min(127, int(velocity)))
        ch = int(self.map.data.get("pads", {}).get("channel", 9)) & 0x0F

        if bank:
            self.switch_bank(bank)
            time.sleep(0.04)

        sock, fh = self._connect()
        try:
            if gap_ms <= 0:
                for p in pad_entries:
                    self._inject(fh, bytes([0x90 | ch, int(p["note"]) & 0x7F, vel]))
                if aftertouch is not None:
                    pressure = max(0, min(127, int(aftertouch)))
                    time.sleep(0.02)
                    for p in pad_entries:
                        self._inject(fh, bytes([0xA0 | ch, int(p["note"]) & 0x7F, pressure]))
                time.sleep(max(10, duration_ms) / 1000.0)
                for p in pad_entries:
                    self._inject(fh, bytes([0x80 | ch, int(p["note"]) & 0x7F, 0x00]))
            else:
                for idx, p in enumerate(pad_entries):
                    note = int(p["note"]) & 0x7F
                    self._inject(fh, bytes([0x90 | ch, note, vel]))
                    if aftertouch is not None:
                        time.sleep(0.015)
                        self._inject(fh, bytes([0xA0 | ch, note, max(0, min(127, int(aftertouch)))]))
                    time.sleep(max(10, duration_ms) / 1000.0)
                    self._inject(fh, bytes([0x80 | ch, note, 0x00]))
                    if idx + 1 < len(pad_entries):
                        time.sleep(gap_ms / 1000.0)
        finally:
            fh.close()
            sock.close()

        self._log(
            "pads",
            f"{[p['pad'] for p in pad_entries]} vel={vel}" + (f" bank={bank.upper()}" if bank else ""),
        )
        return {
            "pads": [p["pad"] for p in pad_entries],
            "notes": [p["note"] for p in pad_entries],
            "velocity": vel,
            "duration_ms": duration_ms,
            "aftertouch": aftertouch,
            "bank": bank.upper() if bank else None,
        }

    def turn_dial(self, steps: int = 1, press: bool = False, step_delay_ms: int = 20) -> Dict[str, Any]:
        dial = self.map.data.get("dial", {})
        ch = int(dial.get("channel", 0)) & 0x0F
        cc = int(dial.get("cc", 100)) & 0x7F

        rem = int(steps)
        sock, fh = self._connect()
        try:
            # Emit +/-1 ticks sequentially for deterministic UI encoder stepping.
            tick_dir = 1 if rem > 0 else -1
            encoded = _encode_rel_7bit(tick_dir)
            for i in range(abs(rem)):
                self._inject(fh, bytes([0xB0 | ch, cc, encoded]))
                if i + 1 < abs(rem) and step_delay_ms > 0:
                    time.sleep(step_delay_ms / 1000.0)
        finally:
            fh.close()
            sock.close()

        if press:
            if steps != 0:
                time.sleep(0.03)
            self.press_button(dial.get("press_button", "DATA_DIAL_PRESS"))

        self._log("dial", f"steps={steps:+d} press={press}")
        return {"steps": steps, "cc": cc, "pressed": press}

    def qlink(
        self,
        knob: int,
        delta: Optional[int] = None,
        value: Optional[int] = None,
        touch: Optional[bool] = None,
    ) -> Dict[str, Any]:
        knobs = self.map.data.get("qlinks", {}).get("knobs", {})
        kcfg = knobs.get(str(knob))
        if not kcfg:
            raise ValueError(f"Q-Link knob must be 1..4, got {knob}")

        cc = kcfg.get("cc")
        learned_key = f"QLINK_{knob}"
        if cc is None and learned_key in self.map.learned:
            cc = self.map.learned[learned_key].get("cc")
        touch_note = kcfg.get("touch_note")
        ch = int(kcfg.get("channel", 0)) & 0x0F

        if cc is None and not (touch is not None and touch_note is not None):
            raise ValueError(
                f"Q-Link {knob} CC/touch note is not yet mapped for MPC Live hardware. "
                f"Run mpc_learn(name='QLINK_{knob}') while turning physical Q-Link {knob}, "
                "or touch the UI parameter via :6720/mcp / mpc_dial."
            )

        sock, fh = self._connect()
        try:
            if touch is True and touch_note is not None:
                self._inject(fh, bytes([0x90 | ch, int(touch_note) & 0x7F, 0x7F]))
                time.sleep(0.02)
            if cc is not None and delta is not None and delta != 0:
                step_dir = 1 if delta > 0 else -1
                rel = _encode_rel_7bit(step_dir)
                for _ in range(abs(int(delta))):
                    self._inject(fh, bytes([0xB0 | ch, int(cc) & 0x7F, rel]))
                    time.sleep(0.015)
            elif cc is not None and value is not None:
                v = max(0, min(127, int(value)))
                self._inject(fh, bytes([0xB0 | ch, int(cc) & 0x7F, v]))
            if touch is False and touch_note is not None:
                time.sleep(0.02)
                self._inject(fh, bytes([0x90 | ch, int(touch_note) & 0x7F, 0x00]))
        finally:
            fh.close()
            sock.close()

        self._log("qlink", f"knob={knob} delta={delta} val={value}")
        return {"knob": knob, "cc": cc, "delta": delta, "value": value, "touch": touch}

    def play_sequence(self, steps: List[Dict[str, Any]], default_step_ms: int = 125) -> Dict[str, Any]:
        executed = 0
        for step in steps:
            wait_after = int(step.get("wait_ms", default_step_ms))
            if "pad" in step or "pads" in step:
                pad_list = step["pads"] if "pads" in step else [step["pad"]]
                self.trigger_pads(
                    pad_list,
                    velocity=int(step.get("velocity", 100)),
                    duration_ms=int(step.get("duration_ms", min(80, wait_after))),
                    aftertouch=step.get("aftertouch"),
                    bank=step.get("bank"),
                )
            elif "button" in step:
                self.press_button(
                    step["button"],
                    action=step.get("action", "tap"),
                    hold_ms=int(step.get("hold_ms", 60)),
                )
            elif "combo" in step:
                c = step["combo"]
                self.combo(c[0], c[1], hold_ms=int(step.get("hold_ms", 60)))
            elif "dial" in step:
                self.turn_dial(steps=int(step["dial"]), press=bool(step.get("press", False)))
            elif "rest_ms" in step:
                time.sleep(int(step["rest_ms"]) / 1000.0)
                wait_after = 0
            executed += 1
            if wait_after > 0:
                time.sleep(wait_after / 1000.0)
        return {"steps_executed": executed}

    def raw_midi(self, hex_bytes: str, allow_unsafe: bool = False) -> Dict[str, Any]:
        tokens = hex_bytes.replace(",", " ").replace(":", " ").split()
        raw = bytes(int(tok, 16) for tok in tokens)
        sock, fh = self._connect()
        try:
            sent = self._inject(fh, raw, allow_unsafe=allow_unsafe)
        finally:
            fh.close()
            sock.close()
        decoded = decode_midi_bytes(raw)
        self._log("raw_midi", decoded.get("hex", hex_bytes))
        return {"bytes_sent": sent, "decoded": decoded}

    def events(
        self,
        since_seq: int = 0,
        sniff_leds: Optional[bool] = None,
        clear_first: bool = False,
        include_noise: bool = False,
    ) -> Dict[str, Any]:
        sock, fh = self._connect()
        try:
            if sniff_leds is not None:
                self._cmd(fh, f"SNIFF_LEDS {1 if sniff_leds else 0}")
            if clear_first:
                clr = self._cmd(fh, "CLEAR")
                since_seq = int(clr.split()[-1])
            fh.write(f"EVENTS {int(since_seq)}\n".encode("utf-8"))
            captured: List[Dict[str, Any]] = []
            latest = since_seq
            while True:
                ln = fh.readline().decode("utf-8", errors="replace").strip()
                if not ln:
                    break
                if ln.startswith("END"):
                    latest = int(ln.split()[-1])
                    break
                if ln.startswith("EV "):
                    parts = ln.split(None, 4)
                    if len(parts) == 5:
                        seq, ts_ms, direction, hx = int(parts[1]), int(parts[2]), parts[3], parts[4]
                        raw = bytes(int(x, 16) for x in hx.split())
                        ev = {"seq": seq, "ts_ms": ts_ms, "dir": direction, **decode_midi_bytes(raw)}
                        if include_noise or not is_noise_event(ev):
                            captured.append(ev)
        finally:
            fh.close()
            sock.close()
        return {"latest_seq": latest, "count": len(captured), "events": captured}

    def learn_control(self, name: str, timeout_s: float = 10.0) -> Dict[str, Any]:
        canonical = _normalize_key(name)
        sock, fh = self._connect()
        try:
            since = int(self._cmd(fh, "CLEAR").split()[-1])
            deadline = time.monotonic() + max(1.0, float(timeout_s))
            learned_ev: Optional[Dict[str, Any]] = None
            while time.monotonic() < deadline:
                fh.write(f"EVENTS {since}\n".encode("utf-8"))
                while True:
                    ln = fh.readline().decode("utf-8", errors="replace").strip()
                    if not ln or ln.startswith("END"):
                        if ln.startswith("END"):
                            since = int(ln.split()[-1])
                        break
                    if ln.startswith("EV "):
                        parts = ln.split(None, 4)
                        if len(parts) == 5 and parts[3] == "RX":
                            raw = bytes(int(x, 16) for x in parts[4].split())
                            ev = {"seq": int(parts[1]), "ts_ms": int(parts[2]), "dir": "RX", **decode_midi_bytes(raw)}
                            if not is_noise_event(ev) and ev.get("type") in ("note_on", "cc"):
                                learned_ev = ev
                                break
                if learned_ev:
                    break
                time.sleep(0.08)
        finally:
            fh.close()
            sock.close()

        if not learned_ev:
            raise TimeoutError(f"No physical hardware control event received within {timeout_s}s")

        entry: Dict[str, Any] = {"channel": learned_ev["channel"], "hex": learned_ev["hex"]}
        if learned_ev["type"] == "note_on":
            entry["note"] = learned_ev["note"]
        elif learned_ev["type"] == "cc":
            entry["cc"] = learned_ev["cc"]
            entry["encoding"] = "twos_complement_7bit"
            if canonical.startswith("QLINK_") and canonical[-1] in ("1", "2", "3", "4"):
                self.map.data.setdefault("qlinks", {}).setdefault("knobs", {}).setdefault(canonical[-1], {}).update(
                    {"cc": learned_ev["cc"], "channel": learned_ev["channel"]}
                )

        self.map.data.setdefault("learned", {})[canonical] = entry
        self.map.save()
        self._log("learn", f"{canonical} -> {learned_ev.get('type')} {learned_ev.get('hex')}")
        return {"learned": canonical, "event": learned_ev, "saved_entry": entry}

    def screenshot(self, half: bool = True, save_path: Optional[Path] = None) -> bytes:
        suffix = "?half=1" if half else ""
        png_bytes = self._remote_request("GET", f"/screen{suffix}")
        if save_path:
            Path(save_path).write_bytes(png_bytes)
            self._log("screenshot", f"{Path(save_path).name} ({len(png_bytes)}B)")
        return png_bytes

    def screen_info(self) -> Dict[str, Any]:
        raw = self._remote_request("GET", "/info")
        return json.loads(raw.decode("utf-8"))

    def tap(self, x: int, y: int, pause_ms: int = 140) -> Dict[str, Any]:
        self._remote_request("GET", f"/tap?x={int(x)}&y={int(y)}")
        if pause_ms > 0:
            time.sleep(pause_ms / 1000.0)
        self._log("tap", f"({int(x)},{int(y)})")
        return {"x": int(x), "y": int(y)}

    def ui_tap(self, target: str, pause_ms: int = 160) -> Dict[str, Any]:
        x, y = self.map.resolve_ui(target)
        self._remote_request("GET", f"/tap?x={x}&y={y}")
        if pause_ms > 0:
            time.sleep(pause_ms / 1000.0)
        self._log("ui_tap", f"{target} ({x},{y})")
        return {"target": target, "x": x, "y": y}

    def remote_mcp(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments or {}},
        }).encode("utf-8")
        raw = self._remote_request("POST", "/mcp", body=payload, headers={"Content-Type": "application/json"})
        resp = json.loads(raw.decode("utf-8"))
        if "error" in resp:
            raise RuntimeError(f"Remote MCP error: {resp['error']}")
        return resp.get("result", {})

    def drag(self, from_x: int, from_y: int, to_x: int, to_y: int, duration_ms: int = 200) -> Dict[str, Any]:
        return self.remote_mcp("drag", {
            "from_x": int(from_x), "from_y": int(from_y),
            "to_x": int(to_x), "to_y": int(to_y),
            "duration_ms": int(duration_ms),
        })

    def scroll(self, x: int, y: int, direction: str = "down", distance: int = 300) -> Dict[str, Any]:
        return self.remote_mcp("scroll", {
            "x": int(x), "y": int(y), "direction": direction, "distance": int(distance),
        })

    def clear_sequence_events(self) -> Dict[str, Any]:
        """Clear all recorded MIDI events on the active Sequence via MPC 3.x UI."""
        self.press_button("MAIN")
        time.sleep(0.22)
        self.ui_tap("main.seq_edit_pencil", pause_ms=260)
        self.ui_tap("seq_edit_modal.clear_events_icon", pause_ms=260)
        self.ui_tap("seq_edit_modal.confirm_clear_events_btn", pause_ms=300)
        self.press_button("STOP")
        time.sleep(0.12)
        self.press_button("STOP")
        self._log("ui_macro", "clear_sequence_events")
        return {"cleared": True}

    def open_browser(self, select_okcomputer: bool = True) -> Dict[str, Any]:
        """Open MPC 3.x Browser (workaround for hardware BROWSE button on 3.x UI)."""
        self.press_button("MAIN")
        time.sleep(0.22)
        self.ui_tap("main.browser_icon", pause_ms=300)
        if select_okcomputer:
            self.ui_tap("browser.places_tab", pause_ms=220)
            self.ui_tap("browser.okcomputer_place", pause_ms=250)
        self._log("ui_macro", f"open_browser okcomputer={select_okcomputer}")
        return {"browser_opened": True, "okcomputer": select_okcomputer}

    def program_step_seq(
        self,
        pads_map: Dict[Any, Dict[Any, Dict[Any, Any]]],
        start_pad: int = 1,
        bars: int = 4,
        step_pause_ms: int = 135,
        nav_pause_ms: int = 180,
    ) -> Dict[str, Any]:
        """Program Step Sequencer steps + velocities deterministically via calibrated UI coords."""
        ss = self.map.data.get("ui", {}).get("step_seq", {})
        x0 = int(ss.get("step_x_start", 63))
        dx = int(ss.get("step_x_pitch", 66))
        step_y = int(ss.get("step_y", 661))
        top_y = int(ss.get("vel_top_y", 224))
        bot_y = int(ss.get("vel_bottom_y", 617))
        bar_minus = ss.get("bar_minus", [302, 768])
        bar_plus = ss.get("bar_plus", [548, 768])
        pad_minus = ss.get("pad_minus", [942, 768])
        pad_plus = ss.get("pad_plus", [1188, 768])

        def vel_to_y(v: int) -> int:
            return max(top_y + 2, min(bot_y - 7, round(bot_y - (max(1, min(127, v)) / 127.0) * (bot_y - top_y))))

        self.press_button("STEP_SEQ")
        time.sleep(0.25)

        cur_pad = int(start_pad)
        # Ensure we begin on Bar 1 before iterating
        for _ in range(max(1, bars - 1)):
            self.tap(bar_minus[0], bar_minus[1], pause_ms=150)

        steps_written = 0
        ordered_pads = sorted(int(k) for k in pads_map.keys())
        for target_pad in ordered_pads:
            while cur_pad < target_pad:
                self.tap(pad_plus[0], pad_plus[1], pause_ms=nav_pause_ms)
                cur_pad += 1
            while cur_pad > target_pad:
                self.tap(pad_minus[0], pad_minus[1], pause_ms=nav_pause_ms)
                cur_pad -= 1

            if target_pad != ordered_pads[0]:
                for _ in range(max(1, bars - 1)):
                    self.tap(bar_minus[0], bar_minus[1], pause_ms=150)

            bar_dict = pads_map.get(target_pad) or pads_map.get(str(target_pad)) or {}
            for bar_num in range(1, bars + 1):
                step_dict = bar_dict.get(bar_num) or bar_dict.get(str(bar_num)) or {}
                for step_k, vel_v in sorted((int(sk), sv) for sk, sv in step_dict.items()):
                    vel = int(vel_v["velocity"]) if isinstance(vel_v, dict) else int(vel_v)
                    sx = x0 + dx * (step_k - 1)
                    self.tap(sx, step_y, pause_ms=step_pause_ms)
                    if vel < 127:
                        self.tap(sx, vel_to_y(vel), pause_ms=max(90, step_pause_ms - 25))
                    steps_written += 1
                if bar_num < bars:
                    self.tap(bar_plus[0], bar_plus[1], pause_ms=nav_pause_ms)

        self._log("step_seq", f"{len(ordered_pads)} pad, {steps_written} adım, {bars} bar")
        return {"pads_programmed": len(ordered_pads), "steps_written": steps_written, "bars": bars}
