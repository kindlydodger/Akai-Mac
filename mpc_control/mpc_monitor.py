#!/usr/bin/env python3
"""Real-time Mac terminal monitor for Akai MPC Live hardware, Antigravity agent actions, and extensible targets."""

from __future__ import annotations

import argparse
import concurrent.futures
import curses
import glob
import json
import locale
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from mpc_client import ACTIVITY_LOG_PATH, MpcBridgeClient, MpcMap, emit_activity

DEFAULT_TARGETS_PATH = HERE / "monitor_targets.json"
DEFAULT_EVENT_LIMIT = 2000

# Authentic Akai MPC 3.x Menu color palette (xterm-256 indices derived from live 1280x800 UI)
ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[38;5;249m"       # #AFAFB1 menu muted silver
ANSI_HDR_BAR = "\033[48;5;236;38;5;255;1m"   # #2F2F34 menu topbar
ANSI_CRIMSON_BG = "\033[48;5;161;38;5;255;1m" # #D31145 MAIN MODE / REC ARM badge
ANSI_COLORS = {
    "OK": "\033[38;5;42m",       # #2DD06C MEM/CPU emerald
    "WARN": "\033[38;5;214m",    # #E2AA02 BROWSER gold
    "ERR": "\033[38;5;197m",     # #EE1734 MAIN MODE crimson
    "INIT": "\033[38;5;44m",     # #1B7985 SAMPLE EDIT teal
    "OFF": "\033[38;5;242m",     # muted slate
    "info": "\033[38;5;44m",     # Sample Edit teal
    "midi": "\033[38;5;207m",    # #FF51FF ARRANGE magenta
    "warn": "\033[38;5;214m",    # Browser gold
    "err": "\033[38;5;197m",     # Crimson
    "ok": "\033[38;5;42m",       # Emerald
    "agent": "\033[38;5;39m",    # SONG cobalt sky blue
}


def _now_iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time()).strftime("%H:%M:%S")


def _safe_str(value: Any, max_len: int = 240) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").replace("\t", " ")
    cleaned = "".join(ch if (ch >= " " or ord(ch) > 127) else " " for ch in text)
    cleaned = " ".join(cleaned.split())
    if max_len > 3 and len(cleaned) > max_len:
        return cleaned[: max_len - 3] + "..."
    return cleaned


def _make_event(
    target_id: str,
    source: str,
    action: str,
    detail: str,
    level: str = "info",
    ts: Optional[float] = None,
) -> Dict[str, Any]:
    now = ts if ts is not None else time.time()
    return {
        "ts": now,
        "iso": _now_iso(now),
        "target_id": target_id,
        "source": source,
        "action": action,
        "detail": _safe_str(detail, 300),
        "level": level,
    }


def _build_pad_note_lookup(mpc_map: Optional[MpcMap] = None) -> Dict[int, str]:
    try:
        m = mpc_map or MpcMap()
        return {int(item["note"]): str(pad_num) for pad_num, item in m.pads_by_num.items()}
    except Exception:
        return {}


def _format_midi_event(ev: Dict[str, Any], pad_lookup: Dict[int, str]) -> str:
    etype = ev.get("type", "unknown")
    ch = ev.get("channel", 0)
    if etype in ("note_on", "note_off"):
        note = ev.get("note", -1)
        vel = ev.get("velocity", 0)
        pad_label = f"Pad {pad_lookup[note]}" if ch == 9 and note in pad_lookup else f"note={note} (0x{note:02X})"
        return f"{etype.upper()} ch{ch + 1} {pad_label} vel={vel}"
    if etype == "cc":
        return f"CC#{ev.get('cc')} val={ev.get('value')} (rel={ev.get('relative'):+d})"
    if etype == "poly_aftertouch":
        return f"AT ch{ch + 1} note={ev.get('note')} pressure={ev.get('pressure')}"
    return f"{etype.upper()} {ev.get('hex', '')}".strip()


# ---------------------------------------------------------------------------
# Log line parsers
# ---------------------------------------------------------------------------

def _parse_jsonl_activity(line: str, target_id: str) -> Optional[Dict[str, Any]]:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return _make_event(target_id, "log", "raw", line, level="warn")
    return _make_event(
        target_id=target_id,
        source=str(obj.get("source") or "mpc"),
        action=str(obj.get("action") or "event"),
        detail=str(obj.get("detail") or ""),
        level=str(obj.get("level") or "info"),
        ts=float(obj["ts"]) if isinstance(obj.get("ts"), (int, float)) else None,
    )


def _summarize_tool_calls(tool_calls: Any) -> str:
    if not isinstance(tool_calls, list) or not tool_calls:
        return ""
    items: List[str] = []
    for tc in tool_calls[:4]:
        if not isinstance(tc, dict):
            continue
        name = str(tc.get("name") or tc.get("tool") or "tool")
        args = tc.get("args") or tc.get("arguments") or {}
        if isinstance(args, dict):
            preview_parts = []
            for k in ("name", "target", "pads", "preset", "Path", "CommandLine", "toolSummary"):
                if k in args and args[k] is not None:
                    raw_val = str(args[k]).strip('"').strip("'")
                    if k == "Path" and "/" in raw_val:
                        raw_val = Path(raw_val).name
                    preview_parts.append(f"{k}={_safe_str(raw_val, 42)}")
                    break
            hint = f"({', '.join(preview_parts)})" if preview_parts else ""
        else:
            hint = ""
        items.append(f"{name}{hint}")
    extra = f" (+{len(tool_calls) - 4})" if len(tool_calls) > 4 else ""
    return ", ".join(items) + extra


def _parse_antigravity_transcript(line: str, target_id: str) -> Optional[Dict[str, Any]]:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    step_type = str(obj.get("type") or "")
    status = str(obj.get("status") or "")
    step_idx = obj.get("step_index", "?")
    content = _safe_str(obj.get("content") or "", 140)
    level = "err" if status == "ERROR" else "agent"

    if step_type == "USER_INPUT":
        return _make_event(target_id, "USER", f"step#{step_idx}", content or "(user message)", level="ok")
    if step_type == "PLANNER_RESPONSE":
        tc_summary = _summarize_tool_calls(obj.get("tool_calls"))
        if tc_summary:
            return _make_event(target_id, "AGENT", f"tools#{step_idx}", tc_summary, level=level)
        if content:
            return _make_event(target_id, "AGENT", f"reply#{step_idx}", content, level=level)
        return None
    if status == "ERROR" and content:
        return _make_event(target_id, step_type or "SYS", f"err#{step_idx}", content, level="err")
    return None


def _parse_plain_log(line: str, target_id: str) -> Optional[Dict[str, Any]]:
    text = line.strip()
    if not text:
        return None
    return _make_event(target_id, target_id, "log", text, level="info")


LOG_PARSERS: Dict[str, Callable[[str, str], Optional[Dict[str, Any]]]] = {
    "jsonl_activity": _parse_jsonl_activity,
    "antigravity_transcript": _parse_antigravity_transcript,
    "plain": _parse_plain_log,
}


# ---------------------------------------------------------------------------
# Probe handlers
# ---------------------------------------------------------------------------

def _emit_status_transition(
    target_id: str,
    state: Dict[str, Any],
    new_status: str,
    summary: str,
    events: List[Dict[str, Any]],
) -> None:
    prev_status = state.get("_last_status")
    prev_summary = state.get("_last_summary")
    state["_last_status"] = new_status
    state["_last_summary"] = summary
    if prev_status is None:
        lvl = "ok" if new_status == "OK" else ("warn" if new_status == "WARN" else "err")
        events.append(_make_event(target_id, target_id, new_status, summary, level=lvl))
    elif prev_status != new_status or (new_status == "ERR" and summary != prev_summary):
        lvl = "ok" if new_status == "OK" else ("warn" if new_status == "WARN" else "err")
        events.append(_make_event(target_id, target_id, f"{prev_status}->{new_status}", summary, level=lvl))


def _probe_mpc_bridge(target: Dict[str, Any], state: Dict[str, Any]) -> Tuple[str, str, List[Dict[str, Any]]]:
    tid = target["id"]
    cfg = target.get("config") or {}
    client: Optional[MpcBridgeClient] = state.get("_client")
    if client is None:
        client = MpcBridgeClient(
            host=cfg.get("host"),
            port=int(cfg.get("port", 6731)),
            timeout=float(cfg.get("timeout_s", 1.8)),
        )
        state["_client"] = client
        state["_pad_lookup"] = _build_pad_note_lookup(client.map)

    events_out: List[Dict[str, Any]] = []
    try:
        st = client.status(check_screen=False)
        initialized = "_since_seq" in state
        since_seq = int(state.get("_since_seq", 0))
        sniff = cfg.get("sniff_leds")
        evs = client.events(
            since_seq=since_seq,
            sniff_leds=bool(sniff) if not initialized and sniff is not None else None,
            include_noise=bool(cfg.get("include_noise", False)),
        )
        latest = int(evs.get("latest_seq", since_seq))
        state["_since_seq"] = latest

        pad_lookup = state.get("_pad_lookup") or {}
        if initialized or cfg.get("replay_initial"):
            for raw_ev in evs.get("events", []):
                direction = raw_ev.get("dir", "?")
                desc = _format_midi_event(raw_ev, pad_lookup)
                events_out.append(_make_event(tid, f"MIDI {direction}", f"#{raw_ev.get('seq', '?')}", desc, level="midi"))

        ready = st.get("ready", False)
        status_code = "OK" if ready else "WARN"
        summary = f"ready={ready} seq={latest} rx={st.get('rx_pkts', '?')} tx={st.get('tx_inj', '?')} priv={st.get('priv_in', '?')}"
        _emit_status_transition(tid, state, status_code, summary, events_out)
        return status_code, summary, events_out
    except Exception as exc:
        summary = _safe_str(f"Bridge bağlantı hatası: {exc}", 140)
        _emit_status_transition(tid, state, "ERR", summary, events_out)
        return "ERR", summary, events_out


def _summarize_http_payload(body: bytes, expect_key: Optional[str]) -> Tuple[str, str]:
    text = body.decode("utf-8", errors="replace").strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return "OK", _safe_str(text or "HTTP 200 OK", 120)

    if not isinstance(obj, dict):
        return "OK", _safe_str(json.dumps(obj, ensure_ascii=False), 120)

    status_code = "OK"
    if expect_key and not obj.get(expect_key):
        status_code = "WARN"
    if isinstance(obj.get("bridge"), dict):
        b = obj["bridge"]
        scr = obj.get("screen") if isinstance(obj.get("screen"), dict) else {}
        if not b.get("ready"):
            status_code = "WARN"
        res_s = f"{scr.get('width', '?')}x{scr.get('height', '?')}" if scr else str(obj.get("screen_http", "?"))
        return status_code, f"bridge={b.get('ready')} screen={res_s} rx={b.get('rx_pkts')} tx={b.get('tx_inj')}"
    if "bridge_ready" in obj and not obj.get("bridge_ready"):
        status_code = "WARN"
    if "bridge_ready" in obj:
        return status_code, (
            f"bridge={obj.get('bridge_ready')} screen={obj.get('screen_http')} "
            f"res={obj.get('width', '?')}x{obj.get('height', '?')}"
        )
    if "width" in obj and "height" in obj:
        return status_code, f"screen={obj.get('width')}x{obj.get('height')} ok={obj.get('ok', True)}"

    compact = " ".join(f"{k}={obj[k]}" for k in list(obj.keys())[:4])
    return status_code, _safe_str(compact or "JSON OK", 120)


def _probe_http(target: Dict[str, Any], state: Dict[str, Any]) -> Tuple[str, str, List[Dict[str, Any]]]:
    tid = target["id"]
    cfg = target.get("config") or {}
    url = str(cfg.get("url") or "")
    timeout_s = float(cfg.get("timeout_s", 2.5))
    expect_key = cfg.get("expect_json_key")
    events_out: List[Dict[str, Any]] = []

    if not url:
        return "ERR", "url eksik", events_out
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mpc-monitor/1.0"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            code = getattr(resp, "status", 200)
            body = resp.read(32768)
        status_code, detail = _summarize_http_payload(body, expect_key)
        summary = f"HTTP {code} · {detail}"
        _emit_status_transition(tid, state, status_code, summary, events_out)
        return status_code, summary, events_out
    except urllib.error.HTTPError as exc:
        summary = f"HTTP {exc.code} · {exc.reason}"
        _emit_status_transition(tid, state, "ERR", summary, events_out)
        return "ERR", summary, events_out
    except Exception as exc:
        summary = _safe_str(f"Erişilemiyor: {exc}", 140)
        _emit_status_transition(tid, state, "ERR", summary, events_out)
        return "ERR", summary, events_out


def _probe_tcp(target: Dict[str, Any], state: Dict[str, Any]) -> Tuple[str, str, List[Dict[str, Any]]]:
    tid = target["id"]
    cfg = target.get("config") or {}
    host = str(cfg.get("host") or "")
    port = int(cfg.get("port") or 0)
    timeout_s = float(cfg.get("timeout_s", 2.0))
    events_out: List[Dict[str, Any]] = []

    if not host or port <= 0:
        return "ERR", "host/port eksik", events_out
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            pass
        summary = f"{host}:{port} AÇIK"
        _emit_status_transition(tid, state, "OK", summary, events_out)
        return "OK", summary, events_out
    except Exception as exc:
        summary = _safe_str(f"{host}:{port} kapalı ({exc})", 140)
        _emit_status_transition(tid, state, "ERR", summary, events_out)
        return "ERR", summary, events_out


def _probe_ssh(target: Dict[str, Any], state: Dict[str, Any]) -> Tuple[str, str, List[Dict[str, Any]]]:
    tid = target["id"]
    cfg = target.get("config") or {}
    host = str(cfg.get("host") or "")
    command = str(cfg.get("command") or "uptime")
    timeout_s = float(cfg.get("timeout_s", 4.0))
    events_out: List[Dict[str, Any]] = []

    if not host:
        return "ERR", "ssh host eksik", events_out
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={max(1, int(timeout_s))}", host, command],
            capture_output=True,
            text=True,
            timeout=timeout_s + 2.0,
        )
        if proc.returncode == 0:
            summary = _safe_str(proc.stdout.strip() or "SSH OK", 140)
            prev_out = state.get("_last_summary")
            _emit_status_transition(tid, state, "OK", summary, events_out)
            if prev_out is not None and summary != prev_out and not events_out:
                events_out.append(_make_event(tid, tid, "ssh_update", summary, level="info"))
            return "OK", summary, events_out
        err = _safe_str(proc.stderr.strip() or f"exit {proc.returncode}", 140)
        _emit_status_transition(tid, state, "ERR", err, events_out)
        return "ERR", err, events_out
    except Exception as exc:
        summary = _safe_str(f"SSH hatası: {exc}", 140)
        _emit_status_transition(tid, state, "ERR", summary, events_out)
        return "ERR", summary, events_out


def _resolve_target_path(cfg: Dict[str, Any]) -> Optional[Path]:
    if cfg.get("path"):
        return Path(os.path.expanduser(str(cfg["path"])))
    pattern = cfg.get("path_glob")
    if not pattern:
        return None
    matches = [Path(p) for p in glob.glob(os.path.expanduser(str(pattern))) if Path(p).is_file()]
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime_ns)


def _probe_log_tail(target: Dict[str, Any], state: Dict[str, Any]) -> Tuple[str, str, List[Dict[str, Any]]]:
    tid = target["id"]
    cfg = target.get("config") or {}
    parser_name = str(cfg.get("parser") or "plain")
    parser_fn = LOG_PARSERS.get(parser_name, _parse_plain_log)
    max_initial = int(cfg.get("max_initial_lines", 25))
    events_out: List[Dict[str, Any]] = []

    resolved = _resolve_target_path(cfg)
    if resolved is None or not resolved.exists():
        summary = f"Bekleniyor ({cfg.get('path') or cfg.get('path_glob')})"
        state["_last_status"] = "WARN"
        return "WARN", _safe_str(summary, 140), events_out

    try:
        st = resolved.stat()
    except OSError as exc:
        return "ERR", _safe_str(str(exc), 120), events_out

    prev_file = state.get("_file")
    prev_inode = state.get("_inode")
    prev_offset = int(state.get("_offset", 0))
    first_bind = prev_file != str(resolved) or prev_inode != st.st_ino or st.st_size < prev_offset

    parsed_lines: List[Dict[str, Any]] = []
    if first_bind:
        # Tail near the end on first open so huge existing transcripts never flood startup.
        tail_window = max(65536, max_initial * 1024)
        start_pos = max(0, st.st_size - tail_window)
        with resolved.open("rb") as fh:
            if start_pos > 0:
                fh.seek(start_pos)
                fh.readline()  # discard partial first line
            chunk_start = fh.tell()
            raw = fh.read()
        last_nl = raw.rfind(b"\n")
        complete = raw[: last_nl + 1] if last_nl != -1 else b""
        new_offset = chunk_start + len(complete)
        lines = complete.decode("utf-8", errors="replace").splitlines()
        for ln in lines[-max_initial * 3 :]:
            if not ln.strip():
                continue
            ev = parser_fn(ln, tid)
            if ev is not None:
                parsed_lines.append(ev)
        parsed_lines = parsed_lines[-max_initial:]
        if prev_file is not None and prev_file != str(resolved):
            events_out.append(_make_event(tid, tid, "switched", f"Yeni dosya: {resolved.name}", level="ok"))
        events_out.extend(parsed_lines)
        state["_file"] = str(resolved)
        state["_inode"] = st.st_ino
        state["_offset"] = new_offset
    else:
        if st.st_size > prev_offset:
            with resolved.open("rb") as fh:
                fh.seek(prev_offset)
                raw = fh.read(512 * 1024)
            last_nl = raw.rfind(b"\n")
            if last_nl != -1:
                complete = raw[: last_nl + 1]
                state["_offset"] = prev_offset + len(complete)
                for ln in complete.decode("utf-8", errors="replace").splitlines():
                    if not ln.strip():
                        continue
                    ev = parser_fn(ln, tid)
                    if ev is not None:
                        events_out.append(ev)

    size_kb = st.st_size / 1024.0
    extra_note = f"+{len(events_out)}" if events_out else "güncel"
    summary = f"{resolved.name} ({size_kb:.1f}KB, {extra_note})"
    state["_last_status"] = "OK"
    return "OK", _safe_str(summary, 140), events_out


def _scan_dir_entries(cfg: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Tuple[int, int]]]:
    entries: Dict[str, Tuple[int, int]] = {}
    if cfg.get("path"):
        root = Path(os.path.expanduser(str(cfg["path"])))
        if not root.exists():
            return f"Klasör yok: {root}", entries
        for item in root.iterdir():
            try:
                if item.is_file():
                    st = item.stat()
                    entries[str(item)] = (st.st_mtime_ns, st.st_size)
            except OSError:
                continue
        return None, entries

    pattern = cfg.get("path_glob")
    if pattern:
        for raw_p in glob.glob(os.path.expanduser(str(pattern))):
            p = Path(raw_p)
            try:
                if p.is_file():
                    st = p.stat()
                    entries[str(p)] = (st.st_mtime_ns, st.st_size)
            except OSError:
                continue
        return None, entries
    return "path veya path_glob eksik", entries


def _probe_dir_watch(target: Dict[str, Any], state: Dict[str, Any]) -> Tuple[str, str, List[Dict[str, Any]]]:
    tid = target["id"]
    cfg = target.get("config") or {}
    recent_n = int(cfg.get("recent_n", 5))
    events_out: List[Dict[str, Any]] = []

    err, current = _scan_dir_entries(cfg)
    if err:
        return "WARN", _safe_str(err, 120), events_out

    prev: Optional[Dict[str, Tuple[int, int]]] = state.get("_snapshot")
    if prev is None:
        newest = sorted(current.items(), key=lambda kv: kv[1][0], reverse=True)[:recent_n]
        for path_str, (_, sz) in reversed(newest):
            events_out.append(_make_event(tid, tid, "watch", f"{Path(path_str).name} ({sz}B)", level="info"))
    else:
        for p, (mtime, sz) in current.items():
            name = Path(p).name
            if p not in prev:
                events_out.append(_make_event(tid, tid, "created", f"+ {name} ({sz}B)", level="ok"))
            elif prev[p] != (mtime, sz):
                delta = sz - prev[p][1]
                events_out.append(_make_event(tid, tid, "modified", f"~ {name} ({delta:+d}B -> {sz}B)", level="info"))
        for p in prev:
            if p not in current:
                events_out.append(_make_event(tid, tid, "deleted", f"- {Path(p).name}", level="warn"))

    state["_snapshot"] = current
    if not current:
        return "WARN", "0 dosya", events_out
    latest_path = max(current.items(), key=lambda kv: kv[1][0])[0]
    summary = f"{len(current)} dosya · son: {Path(latest_path).name}"
    return "OK", _safe_str(summary, 140), events_out


def _probe_cmd(target: Dict[str, Any], state: Dict[str, Any]) -> Tuple[str, str, List[Dict[str, Any]]]:
    tid = target["id"]
    cfg = target.get("config") or {}
    command = str(cfg.get("command") or "")
    timeout_s = float(cfg.get("timeout_s", 3.0))
    events_out: List[Dict[str, Any]] = []

    if not command:
        return "ERR", "command eksik", events_out
    try:
        proc = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout_s)
        out_line = _safe_str(proc.stdout.strip() or proc.stderr.strip() or f"exit={proc.returncode}", 140)
        status_code = "OK" if proc.returncode == 0 else "ERR"
        prev_out = state.get("_last_summary")
        _emit_status_transition(tid, state, status_code, out_line, events_out)
        if prev_out is not None and out_line != prev_out and not events_out:
            events_out.append(_make_event(tid, tid, "cmd", out_line, level="info"))
        return status_code, out_line, events_out
    except Exception as exc:
        msg = _safe_str(f"Komut hatası: {exc}", 140)
        _emit_status_transition(tid, state, "ERR", msg, events_out)
        return "ERR", msg, events_out


PROBE_HANDLERS: Dict[str, Callable[[Dict[str, Any], Dict[str, Any]], Tuple[str, str, List[Dict[str, Any]]]]] = {
    "mpc_bridge": _probe_mpc_bridge,
    "http": _probe_http,
    "tcp": _probe_tcp,
    "ssh": _probe_ssh,
    "log_tail": _probe_log_tail,
    "dir_watch": _probe_dir_watch,
    "cmd": _probe_cmd,
}


# ---------------------------------------------------------------------------
# Target config persistence
# ---------------------------------------------------------------------------

def load_targets(path: Path = DEFAULT_TARGETS_PATH) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    targets = data.get("targets", []) if isinstance(data, dict) else []
    return [t for t in targets if isinstance(t, dict) and t.get("id") and t.get("kind") in PROBE_HANDLERS]


def save_targets(targets: List[Dict[str, Any]], path: Path = DEFAULT_TARGETS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    payload = {"version": 1, "targets": targets}
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def add_target(target: Dict[str, Any], path: Path = DEFAULT_TARGETS_PATH) -> List[Dict[str, Any]]:
    tid = str(target["id"]).strip()
    kind = str(target["kind"]).strip()
    if not tid:
        raise ValueError("Target id boş olamaz")
    if kind not in PROBE_HANDLERS:
        raise ValueError(f"Desteklenmeyen tür '{kind}'. Geçerliler: {sorted(PROBE_HANDLERS)}")
    entry = {
        "id": tid,
        "name": str(target.get("name") or tid),
        "kind": kind,
        "enabled": bool(target.get("enabled", True)),
        "interval_s": max(0.3, float(target.get("interval_s", 2.0))),
        "config": dict(target.get("config") or {}),
    }
    targets = [t for t in load_targets(path) if t.get("id") != tid]
    targets.append(entry)
    save_targets(targets, path)
    return targets


def remove_target(target_id: str, path: Path = DEFAULT_TARGETS_PATH) -> Tuple[bool, List[Dict[str, Any]]]:
    existing = load_targets(path)
    kept = [t for t in existing if t.get("id") != target_id]
    if len(kept) == len(existing):
        return False, existing
    save_targets(kept, path)
    return True, kept


def set_target_enabled(target_id: str, enabled: Optional[bool] = None, path: Path = DEFAULT_TARGETS_PATH) -> bool:
    targets = load_targets(path)
    found = False
    for t in targets:
        if t.get("id") == target_id:
            t["enabled"] = (not bool(t.get("enabled", True))) if enabled is None else bool(enabled)
            found = True
            break
    if found:
        save_targets(targets, path)
    return found


def build_target_entry(
    target_id: str,
    kind: str,
    spec: str,
    name: Optional[str] = None,
    interval_s: float = 2.0,
    parser: str = "plain",
) -> Dict[str, Any]:
    """Create a uniform target entry from a compact kind + spec input (CLI or TUI wizard)."""
    cfg: Dict[str, Any] = {}
    if kind == "http":
        cfg = {"url": spec, "timeout_s": 2.5}
    elif kind == "tcp":
        if ":" not in spec:
            raise ValueError(f"Geçersiz tcp biçimi '{spec}' (beklenen host:port)")
        host, port_s = spec.rsplit(":", 1)
        if not host.strip() or not port_s.strip().isdigit():
            raise ValueError(f"Geçersiz tcp host:port '{spec}'")
        cfg = {"host": host.strip(), "port": int(port_s), "timeout_s": 2.0}
    elif kind == "ssh":
        if "::" in spec:
            host, cmd = spec.split("::", 1)
        else:
            host, cmd = spec, "uptime"
        cfg = {"host": host.strip(), "command": cmd.strip(), "timeout_s": 4.0}
    elif kind == "log_tail":
        key = "path_glob" if any(ch in spec for ch in "*?[") else "path"
        cfg = {key: spec.strip(), "parser": parser, "max_initial_lines": 20}
    elif kind == "dir_watch":
        key = "path_glob" if any(ch in spec for ch in "*?[") else "path"
        cfg = {key: spec.strip(), "recent_n": 5}
    elif kind == "cmd":
        cfg = {"command": spec.strip(), "timeout_s": 3.0}
    elif kind == "mpc_bridge":
        if ":" in spec:
            host, port_s = spec.rsplit(":", 1)
            cfg = {"host": host.strip(), "port": int(port_s)}
        else:
            cfg = {"host": spec.strip(), "port": 6731}
    else:
        raise ValueError(f"Geçersiz tür: {kind}")

    return {
        "id": target_id.strip(),
        "name": (name or target_id).strip(),
        "kind": kind,
        "enabled": True,
        "interval_s": max(0.3, float(interval_s)),
        "config": cfg,
    }


# ---------------------------------------------------------------------------
# Concurrent monitor engine
# ---------------------------------------------------------------------------

class MonitorEngine:
    """Runs target probes concurrently on per-target schedules and collects live events."""

    def __init__(self, targets_path: Path = DEFAULT_TARGETS_PATH, event_limit: int = DEFAULT_EVENT_LIMIT) -> None:
        self.targets_path = Path(targets_path)
        self._lock = threading.RLock()
        self.targets: List[Dict[str, Any]] = []
        self.states: Dict[str, Dict[str, Any]] = {}
        self.events: deque[Dict[str, Any]] = deque(maxlen=event_limit)
        self.paused = False
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._in_flight: set[str] = set()
        self._thread: Optional[threading.Thread] = None
        self.reload_targets()

    def reload_targets(self) -> List[Dict[str, Any]]:
        with self._lock:
            self.targets = load_targets(self.targets_path)
            active_ids = {t["id"] for t in self.targets}
            for t in self.targets:
                tid = t["id"]
                st = self.states.setdefault(
                    tid,
                    {
                        "status": "INIT" if t.get("enabled", True) else "OFF",
                        "latency_ms": None,
                        "summary": "Bekliyor..." if t.get("enabled", True) else "Devre dışı",
                        "last_checked": 0.0,
                        "next_due": 0.0,
                        "cursor": {},
                    },
                )
                if not t.get("enabled", True):
                    st["status"] = "OFF"
                    st["summary"] = "Devre dışı"
                elif st["status"] == "OFF":
                    st["status"] = "INIT"
                    st["next_due"] = 0.0
            for dead_id in [k for k in self.states if k not in active_ids]:
                del self.states[dead_id]
            self._wake.set()
            return list(self.targets)

    def force_refresh(self) -> None:
        with self._lock:
            for st in self.states.values():
                st["next_due"] = 0.0
        self._wake.set()

    def clear_events(self) -> None:
        with self._lock:
            self.events.clear()

    def append_external_event(self, ev: Dict[str, Any]) -> None:
        with self._lock:
            self.events.append(ev)

    def _run_single_probe(self, target: Dict[str, Any]) -> None:
        tid = target["id"]
        handler = PROBE_HANDLERS.get(target.get("kind", ""))
        if handler is None:
            return
        with self._lock:
            state_entry = self.states.get(tid)
            if state_entry is None:
                return
            cursor = state_entry["cursor"]

        t0 = time.monotonic()
        try:
            status_code, summary, new_events = handler(target, cursor)
        except Exception as exc:
            status_code, summary, new_events = "ERR", _safe_str(f"Probe exception: {exc}", 140), []
        elapsed_ms = (time.monotonic() - t0) * 1000.0

        now = time.time()
        interval = max(0.3, float(target.get("interval_s", 2.0)))
        with self._lock:
            st = self.states.get(tid)
            if st is not None:
                st["status"] = status_code if target.get("enabled", True) else "OFF"
                st["latency_ms"] = round(elapsed_ms, 1)
                st["summary"] = summary
                st["last_checked"] = now
                st["next_due"] = now + interval
            if not self.paused:
                for ev in new_events:
                    self.events.append(ev)
            self._in_flight.discard(tid)

    def probe_once_all(self, timeout_s: float = 6.0) -> None:
        targets = self.reload_targets()
        enabled = [t for t in targets if t.get("enabled", True)]
        if not enabled:
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(enabled)))) as pool:
            futs = [pool.submit(self._run_single_probe, t) for t in enabled]
            concurrent.futures.wait(futs, timeout=timeout_s)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="mpc-monitor-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)

    def _loop(self) -> None:
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)
        try:
            while not self._stop.is_set():
                now = time.time()
                due: List[Dict[str, Any]] = []
                with self._lock:
                    for t in self.targets:
                        tid = t["id"]
                        if not t.get("enabled", True):
                            continue
                        st = self.states.get(tid)
                        if st and tid not in self._in_flight and now >= st.get("next_due", 0.0):
                            self._in_flight.add(tid)
                            due.append(dict(t))
                for t in due:
                    pool.submit(self._run_single_probe, t)
                self._wake.wait(timeout=0.25)
                self._wake.clear()
        finally:
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                pool.shutdown(wait=False)

    def snapshot(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        with self._lock:
            rows: List[Dict[str, Any]] = []
            for t in self.targets:
                st = self.states.get(t["id"], {})
                rows.append({
                    "id": t["id"],
                    "name": t.get("name", t["id"]),
                    "kind": t["kind"],
                    "enabled": bool(t.get("enabled", True)),
                    "interval_s": t.get("interval_s", 2.0),
                    "status": st.get("status", "INIT"),
                    "latency_ms": st.get("latency_ms"),
                    "summary": st.get("summary", ""),
                    "last_checked": st.get("last_checked", 0.0),
                })
            return rows, list(self.events)


# ---------------------------------------------------------------------------
# Headless snapshot & ANSI stream renderers
# ---------------------------------------------------------------------------

def _print_ansi_snapshot(rows: List[Dict[str, Any]], events: List[Dict[str, Any]]) -> None:
    print(f"{ANSI_CRIMSON_BG} MPC LIVE {ANSI_HDR_BAR} CANLI MONİTÖR ({_now_iso()}) {ANSI_RESET}")
    print(f"{ANSI_DIM}{'ID':<19} {'TÜR':<11} {'DURUM':<6} {'MS':>6}  {'ÖZET'}{ANSI_RESET}")
    print(f"{ANSI_DIM}{'-' * 88}{ANSI_RESET}")
    for r in rows:
        st = r["status"]
        col = ANSI_COLORS.get(st, "")
        ms = f"{r['latency_ms']:.0f}" if isinstance(r["latency_ms"], (int, float)) else "-"
        mark = "●" if r["enabled"] else "○"
        print(f"{col}{mark} {r['id']:<17}{ANSI_RESET} {ANSI_DIM}{r['kind']:<11}{ANSI_RESET} {col}{st:<6}{ANSI_RESET} {ms:>6}  {r['summary']}")
    print(f"\n{ANSI_HDR_BAR} ANLIK AKIŞ (son {min(30, len(events))} olay) {ANSI_RESET}")
    for ev in events[-30:]:
        col = ANSI_COLORS.get(ev.get("level", "info"), "")
        print(
            f"{ANSI_DIM}{ev['iso']}{ANSI_RESET} "
            f"{col}[{ev['source']:<12}]{ANSI_RESET} "
            f"{ANSI_BOLD}{ev['action']:<12}{ANSI_RESET} {ev['detail']}"
        )


def run_stream_mode(engine: MonitorEngine, interval: float = 0.5) -> int:
    engine.start()
    seen = 0
    print(f"{ANSI_CRIMSON_BG} MPC LIVE {ANSI_HDR_BAR} Canlı akış başlatıldı ({engine.targets_path}) · Ctrl+C ile çık {ANSI_RESET}", flush=True)
    try:
        while True:
            _, evs = engine.snapshot()
            if seen > len(evs):
                seen = len(evs)
            for ev in evs[seen:]:
                col = ANSI_COLORS.get(ev.get("level", "info"), "")
                print(
                    f"{ANSI_DIM}{ev['iso']}{ANSI_RESET} "
                    f"{ANSI_DIM}[{ev['target_id']:<14}]{ANSI_RESET} "
                    f"{col}[{ev['source']:<10}]{ANSI_RESET} "
                    f"{ANSI_BOLD}{ev['action']:<12}{ANSI_RESET} {ev['detail']}",
                    flush=True,
                )
            seen = len(evs)
            time.sleep(max(0.2, interval))
    except KeyboardInterrupt:
        print("\nAkış durduruldu.", flush=True)
    finally:
        engine.stop()
    return 0


# ---------------------------------------------------------------------------
# Interactive curses TUI (Akai MPC 3.x Menu Palette)
# ---------------------------------------------------------------------------

def _init_curses_colors() -> Dict[str, int]:
    if not curses.has_colors():
        return {}
    curses.start_color()
    curses.use_default_colors()

    if curses.COLORS >= 256 and curses.COLOR_PAIRS >= 24:
        # Exact xterm-256 approximations of sampled MPC 3.x Menu RGB hexes
        bg_canvas = 16     # #000000 OLED menu canvas
        bg_sidebar = 233   # #171717 sidebar charcoal
        bg_tile = 235      # #242428 menu tile surface
        bg_topbar = 236    # #2F2F34 topbar gunmetal
        fg_white = 255     # #FFFFFF
        fg_text = 254      # #EAECEF
        fg_muted = 249     # #AFAFB1
        fg_crimson = 197   # #EE1734 MAIN MODE crimson
        bg_crimson = 161   # #D31145 REC ARM crimson
        fg_emerald = 42    # #2DD06C MEM/CPU emerald
        fg_gold = 214      # #E2AA02 BROWSER gold
        fg_teal = 44       # #1B7985 SAMPLE EDIT teal
        fg_magenta = 207   # #FF51FF ARRANGE magenta
        fg_blue = 39       # #0E72D6 SONG cobalt
    else:
        bg_canvas = curses.COLOR_BLACK
        bg_sidebar = curses.COLOR_BLACK
        bg_tile = curses.COLOR_BLACK
        bg_topbar = curses.COLOR_BLACK
        fg_white = curses.COLOR_WHITE
        fg_text = curses.COLOR_WHITE
        fg_muted = curses.COLOR_WHITE
        fg_crimson = curses.COLOR_RED
        bg_crimson = curses.COLOR_RED
        fg_emerald = curses.COLOR_GREEN
        fg_gold = curses.COLOR_YELLOW
        fg_teal = curses.COLOR_CYAN
        fg_magenta = curses.COLOR_MAGENTA
        fg_blue = curses.COLOR_BLUE

    defs = {
        1: (fg_emerald, bg_canvas),
        2: (fg_gold, bg_canvas),
        3: (fg_crimson, bg_canvas),
        4: (fg_teal, bg_canvas),
        5: (fg_magenta, bg_canvas),
        6: (fg_blue, bg_canvas),
        7: (fg_white, bg_crimson),
        8: (fg_white, bg_topbar),
        9: (fg_muted, bg_sidebar),
        10: (fg_white, bg_tile),
        11: (fg_emerald, bg_topbar),
        12: (fg_gold, bg_topbar),
        13: (fg_crimson, bg_tile),
        14: (fg_muted, bg_canvas),
        15: (fg_text, bg_canvas),
        16: (fg_gold, bg_tile),
        17: (fg_teal, bg_tile),
        18: (fg_emerald, bg_tile),
    }
    for pid, (fg, bg) in defs.items():
        curses.init_pair(pid, fg, bg)

    return {
        "OK": curses.color_pair(1),
        "ok": curses.color_pair(1),
        "WARN": curses.color_pair(2),
        "warn": curses.color_pair(2),
        "ERR": curses.color_pair(3),
        "err": curses.color_pair(3),
        "INIT": curses.color_pair(4),
        "info": curses.color_pair(4),
        "OFF": curses.color_pair(14),
        "midi": curses.color_pair(5),
        "agent": curses.color_pair(6),
        "badge_red": curses.color_pair(7) | curses.A_BOLD,
        "topbar": curses.color_pair(8) | curses.A_BOLD,
        "topbar_ok": curses.color_pair(11) | curses.A_BOLD,
        "topbar_gold": curses.color_pair(12) | curses.A_BOLD,
        "sidebar_hdr": curses.color_pair(9) | curses.A_BOLD,
        "row_sel": curses.color_pair(10) | curses.A_BOLD,
        "sel_arrow": curses.color_pair(13) | curses.A_BOLD,
        "muted": curses.color_pair(14),
        "canvas": curses.color_pair(15),
        "footer_tile": curses.color_pair(10),
        "key_gold": curses.color_pair(16) | curses.A_BOLD,
        "key_teal": curses.color_pair(17) | curses.A_BOLD,
        "sel_ok": curses.color_pair(18) | curses.A_BOLD,
        "sel_warn": curses.color_pair(16) | curses.A_BOLD,
        "sel_err": curses.color_pair(13) | curses.A_BOLD,
    }


def _prompt_line(stdscr: Any, prompt: str, colors: Dict[str, int], initial: str = "") -> Optional[str]:
    h, w = stdscr.getmaxyx()
    curses.echo()
    curses.curs_set(1)
    stdscr.timeout(-1)
    p_attr = colors.get("key_gold", curses.A_BOLD)
    i_attr = colors.get("row_sel", 0)
    try:
        stdscr.move(h - 1, 0)
        stdscr.clrtoeol()
        stdscr.bkgdset(" ", colors.get("footer_tile", 0))
        stdscr.addnstr(h - 1, 0, " " * max(1, w - 1), w - 1, colors.get("footer_tile", 0))
        text = prompt[: max(1, w - 10)]
        stdscr.addnstr(h - 1, 0, text, w - 1, p_attr)
        if initial:
            stdscr.addnstr(h - 1, len(text), initial, max(1, w - len(text) - 2), i_attr)
        stdscr.refresh()
        buf: List[str] = list(initial)
        while True:
            ch = stdscr.get_wch()
            if ch in ("\n", "\r", curses.KEY_ENTER):
                return "".join(buf).strip()
            if ch == "\x1b":
                return None
            if ch in ("\b", "\x7f", curses.KEY_BACKSPACE):
                if buf:
                    buf.pop()
            elif isinstance(ch, str) and ch >= " ":
                buf.append(ch)
            stdscr.addnstr(h - 1, 0, " " * max(1, w - 1), w - 1, colors.get("footer_tile", 0))
            stdscr.addnstr(h - 1, 0, text, w - 1, p_attr)
            stdscr.addnstr(h - 1, len(text), "".join(buf), max(1, w - len(text) - 1), i_attr)
            stdscr.refresh()
    finally:
        curses.noecho()
        curses.curs_set(0)
        stdscr.bkgdset(" ", colors.get("canvas", 0))
        stdscr.timeout(250)


def _interactive_add_wizard(stdscr: Any, engine: MonitorEngine, colors: Dict[str, int]) -> str:
    kinds_help = "Tür seç [http | tcp | ssh | log_tail | dir_watch | cmd | mpc_bridge]: "
    kind = _prompt_line(stdscr, kinds_help, colors)
    if not kind:
        return "Konum ekleme iptal edildi."
    kind = kind.strip()
    if kind not in PROBE_HANDLERS:
        return f"Geçersiz tür: {kind}"

    spec_prompts = {
        "http": "URL (ör. http://127.0.0.1:8000/health): ",
        "tcp": "host:port (ör. 10.101.132.233:6731): ",
        "ssh": "user@host::komut (ör. root@10.101.132.233::uptime): ",
        "log_tail": "Dosya veya glob yolu: ",
        "dir_watch": "Klasör veya glob yolu: ",
        "cmd": "Çalıştırılacak terminal komutu: ",
        "mpc_bridge": "MPC IP[:port] (ör. 10.101.132.233:6731): ",
    }
    tid = _prompt_line(stdscr, "Kısa ID (ör. ses-klasoru, studio-api): ", colors)
    if not tid:
        return "İptal edildi."
    spec = _prompt_line(stdscr, spec_prompts[kind], colors)
    if not spec:
        return "İptal edildi."
    interval_raw = _prompt_line(stdscr, "Yenileme aralığı saniye [2.0]: ", colors, "2.0")
    try:
        interval_s = float(interval_raw or "2.0")
    except ValueError:
        return f"Geçersiz aralık sayısı: {interval_raw}"

    entry = build_target_entry(tid, kind, spec, interval_s=interval_s)
    add_target(entry, engine.targets_path)
    engine.reload_targets()
    engine.force_refresh()
    return f"Konum eklendi: {entry['id']} ({kind})"


def _draw_tui(
    stdscr: Any,
    engine: MonitorEngine,
    colors: Dict[str, int],
    selected: int,
    filter_target: Optional[str],
    status_flash: str,
) -> int:
    rows, events = engine.snapshot()
    h, w = stdscr.getmaxyx()
    stdscr.bkgd(" ", colors.get("canvas", 0))
    stdscr.erase()
    if h < 6 or w < 24:
        if h >= 1 and w >= 2:
            stdscr.addnstr(0, 0, "Pencere çok küçük", w - 1)
        stdscr.refresh()
        return selected

    selected = max(0, min(selected, max(0, len(rows) - 1)))
    ok_cnt = sum(1 for r in rows if r["status"] == "OK")
    pause_tag = "DURAKLATILDI" if engine.paused else "CANLI"
    filt_tag = f"  FİLTRE:{filter_target}" if filter_target else ""

    # Top MPC 3.x Menu header bar: [MPC LIVE] Crimson badge + Gunmetal bar + Emerald OK meter
    badge = " MPC LIVE "
    stdscr.addnstr(0, 0, badge, w - 1, colors.get("badge_red", curses.A_REVERSE))
    title_rest = f" CANLI MONİTÖR  |  {pause_tag}{filt_tag}".ljust(max(1, w - len(badge) - 1))
    if len(badge) < w - 1:
        stdscr.addnstr(0, len(badge), title_rest, w - len(badge) - 1, colors.get("topbar", curses.A_REVERSE))
    meter = f" HEDEF {ok_cnt}/{len(rows)} | {_now_iso()} "
    if w - len(meter) - 1 > len(badge) + 4:
        stdscr.addnstr(0, w - len(meter) - 1, meter, len(meter), colors.get("topbar_ok", curses.A_REVERSE))

    # Pane 1: Targets table (Sidebar dark bar header)
    max_targets_pane = max(5, min(len(rows) + 3, max(6, h // 2 - 2)))
    cols_hdr = f"  {'KONUM ID':<18} {'TÜR':<10} {'DURUM':<5} {'MS':>5}  {'CANLI BİLGİ'}".ljust(max(1, w - 1))
    if 1 < h:
        stdscr.addnstr(1, 0, cols_hdr, w - 1, colors.get("sidebar_hdr", curses.A_BOLD))

    visible_rows = max(1, max_targets_pane - 2)
    scroll_top = max(0, selected - visible_rows + 1)
    for idx in range(visible_rows):
        r_idx = scroll_top + idx
        line_y = 2 + idx
        if r_idx >= len(rows) or line_y >= h - 3:
            break
        r = rows[r_idx]
        is_sel = r_idx == selected
        mark = "▸" if is_sel else ("●" if r["enabled"] else "○")
        ms = f"{r['latency_ms']:.0f}" if isinstance(r["latency_ms"], (int, float)) else "-"
        st_code = r["status"]

        row_base_attr = colors.get("row_sel", curses.A_REVERSE) if is_sel else colors.get("canvas", 0)
        mark_attr = colors.get("sel_arrow", row_base_attr) if is_sel else colors.get(st_code, row_base_attr)
        st_key = {"OK": "sel_ok", "WARN": "sel_warn", "ERR": "sel_err"}.get(st_code, "row_sel") if is_sel else st_code
        st_attr = colors.get(st_key, row_base_attr)

        if is_sel:
            stdscr.addnstr(line_y, 0, " " * max(1, w - 1), w - 1, row_base_attr)

        left_part = f" {r['id']:<17} {r['kind']:<10} "
        st_part = f"{st_code:<5}"
        right_part = f" {ms:>5}  {r['summary']}"
        stdscr.addnstr(line_y, 0, f"{mark}", w - 1, mark_attr)
        col_x = 1
        if col_x < w - 1:
            stdscr.addnstr(line_y, col_x, left_part, w - col_x - 1, row_base_attr)
            col_x += len(left_part)
        if col_x < w - 1:
            stdscr.addnstr(line_y, col_x, st_part, w - col_x - 1, st_attr | curses.A_BOLD)
            col_x += len(st_part)
        if col_x < w - 1:
            stdscr.addnstr(line_y, col_x, right_part, w - col_x - 1, row_base_attr)

    feed_hdr_y = 2 + visible_rows
    if feed_hdr_y < h - 2:
        feed_title = f" ANLIK BİLGİ AKIŞI ({len(events)} olay) ".ljust(max(1, w - 1), "─")
        stdscr.addnstr(feed_hdr_y, 0, feed_title, w - 1, colors.get("topbar_gold", curses.A_BOLD))

    # Pane 2: Live stream feed on OLED obsidian
    filtered_events = [
        ev for ev in events
        if not filter_target
        or ev.get("target_id") == filter_target
        or (filter_target == "MIDI" and "MIDI" in str(ev.get("source", "")))
    ]
    feed_start_y = feed_hdr_y + 1
    feed_lines = max(0, (h - 2) - feed_start_y)
    tail = filtered_events[-feed_lines:] if feed_lines > 0 else []
    for i, ev in enumerate(tail):
        y = feed_start_y + i
        if y >= h - 2:
            break
        ts_str = f"{ev['iso']} "
        src_str = f"[{ev['source']:<11}] "
        act_str = f"{ev['action']:<11} "
        lvl_attr = colors.get(ev.get("level", "info"), 0)

        x = 0
        stdscr.addnstr(y, x, ts_str, w - 1, colors.get("muted", 0))
        x += len(ts_str)
        if x < w - 1:
            stdscr.addnstr(y, x, src_str, w - x - 1, lvl_attr | curses.A_BOLD)
            x += len(src_str)
        if x < w - 1:
            stdscr.addnstr(y, x, act_str, w - x - 1, lvl_attr)
            x += len(act_str)
        if x < w - 1:
            stdscr.addnstr(y, x, ev["detail"], w - x - 1, colors.get("canvas", 0))

    if h >= 3:
        stdscr.addnstr(h - 2, 0, " " * max(1, w - 1), w - 1, colors.get("footer_tile", curses.A_REVERSE))
        shortcuts = [
            ("[↑/↓]", "Seç"),
            ("[Enter]", "Filtre"),
            ("[a]", "Ekle"),
            ("[d]", "Sil"),
            ("[t]", "Aç/Kapa"),
            ("[p]", "Duraklat"),
            ("[c]", "Temizle"),
            ("[r]", "Yenile"),
            ("[q]", "Çık"),
        ]
        fx = 1
        for key_lbl, desc_lbl in shortcuts:
            if fx + len(key_lbl) + len(desc_lbl) + 3 >= w:
                break
            stdscr.addnstr(h - 2, fx, key_lbl, w - fx - 1, colors.get("key_gold", curses.A_BOLD))
            fx += len(key_lbl) + 1
            stdscr.addnstr(h - 2, fx, desc_lbl, w - fx - 1, colors.get("footer_tile", 0))
            fx += len(desc_lbl) + 2

        flash_txt = _safe_str(status_flash or f"Konum dosyası: {engine.targets_path}", w - 1)
        stdscr.addnstr(h - 1, 0, flash_txt, w - 1, colors.get("muted", 0))
    stdscr.refresh()
    return selected


def run_curses_tui(engine: MonitorEngine) -> int:
    locale.setlocale(locale.LC_ALL, "")

    def _tui_main(stdscr: Any) -> int:
        curses.curs_set(0)
        stdscr.keypad(True)
        stdscr.timeout(250)
        colors = _init_curses_colors()
        engine.start()

        selected = 0
        filter_target: Optional[str] = None
        flash = ""

        try:
            while True:
                selected = _draw_tui(stdscr, engine, colors, selected, filter_target, flash)
                key = stdscr.getch()
                if key == -1:
                    continue
                rows, _ = engine.snapshot()
                if key in (ord("q"), ord("Q")):
                    break
                elif key in (curses.KEY_UP, ord("k")):
                    selected = max(0, selected - 1)
                elif key in (curses.KEY_DOWN, ord("j")):
                    selected = min(max(0, len(rows) - 1), selected + 1)
                elif key in (10, 13, curses.KEY_ENTER):
                    if rows:
                        tid = rows[selected]["id"]
                        filter_target = None if filter_target == tid else tid
                        flash = f"Filtre: {filter_target or 'TÜMÜ'}"
                elif key in (ord("f"), ord("F")):
                    filter_target = "MIDI" if filter_target != "MIDI" else None
                    flash = f"Filtre: {filter_target or 'TÜMÜ'}"
                elif key in (ord("p"), ord("P"), ord(" ")):
                    engine.paused = not engine.paused
                    flash = "Akış duraklatıldı." if engine.paused else "Akış devam ediyor."
                elif key in (ord("c"), ord("C")):
                    engine.clear_events()
                    flash = "Akış ekranı temizlendi."
                elif key in (ord("r"), ord("R")):
                    engine.reload_targets()
                    engine.force_refresh()
                    flash = "Tüm hedefler yenilendi."
                elif key in (ord("t"), ord("T")) and rows:
                    tid = rows[selected]["id"]
                    set_target_enabled(tid, None, engine.targets_path)
                    engine.reload_targets()
                    flash = f"Aç/kapa güncellendi: {tid}"
                elif key in (ord("d"), ord("D")) and rows:
                    tid = rows[selected]["id"]
                    ans = _prompt_line(stdscr, f"'{tid}' silinsin mi? [e/h]: ", colors)
                    if ans and ans.lower() in ("e", "evet", "y", "yes"):
                        remove_target(tid, engine.targets_path)
                        engine.reload_targets()
                        selected = max(0, selected - 1)
                        flash = f"Silindi: {tid}"
                    else:
                        flash = "Silme iptal edildi."
                elif key in (ord("a"), ord("A")):
                    try:
                        flash = _interactive_add_wizard(stdscr, engine, colors)
                    except Exception as exc:
                        flash = f"Hata: {exc}"
        finally:
            engine.stop()
        return 0

    return curses.wrapper(_tui_main)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def run_monitor_cli(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Canlı Mac Terminal Monitörü (MPC + Antigravity + Özel Konumlar)")
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS_PATH, help="Konum config JSON yolu")
    parser.add_argument("--once", action="store_true", help="Tüm konumları 1 kez yokla, özet + son olayları bas")
    parser.add_argument("--json", action="store_true", help="--once veya --list çıktısını JSON olarak bas")
    parser.add_argument("--stream", action="store_true", help="TUI yerine düz terminal akış modu (Ctrl+C ile çık)")
    parser.add_argument("--interval", type=float, default=0.6, help="Akış yenileme aralığı (sn)")
    parser.add_argument("--list", action="store_true", help="Tanımlı tüm izleme konumlarını listele")
    parser.add_argument("--add", action="store_true", help="Yeni izleme konumu ekle")
    parser.add_argument("--id", dest="target_id", default=None, help="Eklenecek konum ID")
    parser.add_argument("--name", default=None, help="Görünen ad")
    parser.add_argument("--kind", choices=sorted(PROBE_HANDLERS.keys()), default=None, help="Konum türü")
    parser.add_argument("--spec", default=None, help="URL, host:port, user@host::cmd, dosya/klasör yolu veya komut")
    parser.add_argument("--parser", choices=sorted(LOG_PARSERS.keys()), default="plain", help="log_tail satır ayrıştırıcı")
    parser.add_argument("--target-interval", type=float, default=2.0, help="Yeni konum yoklama aralığı (sn)")
    parser.add_argument("--remove", metavar="ID", default=None, help="Konumu ID ile kaldır")
    parser.add_argument("--enable", metavar="ID", default=None, help="Konumu etkinleştir")
    parser.add_argument("--disable", metavar="ID", default=None, help="Konumu devre dışı bırak")
    parser.add_argument("--emit", nargs=3, metavar=("SOURCE", "ACTION", "DETAIL"), default=None, help="Test/bilgi olayı ekle")
    args = parser.parse_args(argv)

    tpath = args.targets
    if args.emit:
        emit_activity(args.emit[0], args.emit[1], args.emit[2])
        print(f"Olay eklendi -> {ACTIVITY_LOG_PATH}")
        return 0

    if args.add:
        if not (args.target_id and args.kind and args.spec):
            parser.error("--add için --id, --kind ve --spec zorunludur")
        entry = build_target_entry(
            args.target_id,
            args.kind,
            args.spec,
            name=args.name,
            interval_s=args.target_interval,
            parser=args.parser,
        )
        targets = add_target(entry, tpath)
        print(json.dumps({"added": entry, "total": len(targets)}, indent=2, ensure_ascii=False))
        return 0

    if args.remove:
        ok, targets = remove_target(args.remove, tpath)
        print(json.dumps({"removed": ok, "id": args.remove, "remaining": len(targets)}, ensure_ascii=False))
        return 0 if ok else 1

    if args.enable:
        ok = set_target_enabled(args.enable, True, tpath)
        print(json.dumps({"enabled": ok, "id": args.enable}, ensure_ascii=False))
        return 0 if ok else 1

    if args.disable:
        ok = set_target_enabled(args.disable, False, tpath)
        print(json.dumps({"disabled": ok, "id": args.disable}, ensure_ascii=False))
        return 0 if ok else 1

    if args.list:
        targets = load_targets(tpath)
        if args.json:
            print(json.dumps(targets, indent=2, ensure_ascii=False))
        else:
            for t in targets:
                mark = "●" if t.get("enabled", True) else "○"
                print(f"{mark} {t['id']:<18} {t['kind']:<11} ({t.get('interval_s', 2.0)}s) -> {t.get('config')}")
        return 0

    engine = MonitorEngine(targets_path=tpath)
    if args.once:
        engine.probe_once_all()
        rows, events = engine.snapshot()
        if args.json:
            print(json.dumps({"targets": rows, "events": events[-50:]}, indent=2, ensure_ascii=False))
        else:
            _print_ansi_snapshot(rows, events)
        return 0

    if args.stream or not sys.stdout.isatty():
        return run_stream_mode(engine, interval=args.interval)

    return run_curses_tui(engine)


if __name__ == "__main__":
    raise SystemExit(run_monitor_cli())
