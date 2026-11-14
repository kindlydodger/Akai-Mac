#!/usr/bin/env python3
"""Real-time Mac terminal monitor for Akai MPC Live hardware, Antigravity agent actions, and extensible targets."""

from __future__ import annotations

import argparse
import concurrent.futures
import curses
import glob
import json
import locale
import math
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from mpc_client import ACTIVITY_LOG_PATH, MpcBridgeClient, MpcMap, emit_activity, resolve_mpc_host

DEFAULT_TARGETS_PATH = HERE / "monitor_targets.json"
DEFAULT_EVENT_LIMIT = 2000
DASH_HISTORY_LEN = 60

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
        summary = _safe_str(f"Bridge connection error: {exc}", 140)
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


def _resolve_http_url(url: str, force_refresh: bool = False) -> Tuple[str, Dict[str, str]]:
    """Rewrite non-literal URL hostnames using cached IPv4 resolution, keeping Host header."""
    parsed = urllib.parse.urlsplit(url)
    orig_host = parsed.hostname
    headers = {"User-Agent": "mpc-monitor/1.0"}
    if not orig_host:
        return url, headers
    resolved_ip = resolve_mpc_host(orig_host, force_refresh=force_refresh)
    if resolved_ip == orig_host:
        return url, headers
    port_part = f":{parsed.port}" if parsed.port else ""
    rewritten = urllib.parse.urlunsplit((parsed.scheme, f"{resolved_ip}{port_part}", parsed.path, parsed.query, parsed.fragment))
    headers["Host"] = parsed.netloc
    return rewritten, headers


def _probe_http(target: Dict[str, Any], state: Dict[str, Any]) -> Tuple[str, str, List[Dict[str, Any]]]:
    tid = target["id"]
    cfg = target.get("config") or {}
    url = str(cfg.get("url") or "")
    timeout_s = float(cfg.get("timeout_s", 2.5))
    expect_key = cfg.get("expect_json_key")
    events_out: List[Dict[str, Any]] = []

    if not url:
        return "ERR", "missing url", events_out
    try:
        code, body = 200, b""
        for attempt in range(2):
            target_url, headers = _resolve_http_url(url, force_refresh=(attempt == 1))
            try:
                req = urllib.request.Request(target_url, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                    code = getattr(resp, "status", 200)
                    body = resp.read(32768)
                break
            except (urllib.error.URLError, OSError):
                if attempt == 1 or target_url == url:
                    raise
        status_code, detail = _summarize_http_payload(body, expect_key)
        summary = f"HTTP {code} · {detail}"
        _emit_status_transition(tid, state, status_code, summary, events_out)
        return status_code, summary, events_out
    except urllib.error.HTTPError as exc:
        summary = f"HTTP {exc.code} · {exc.reason}"
        _emit_status_transition(tid, state, "ERR", summary, events_out)
        return "ERR", summary, events_out
    except Exception as exc:
        summary = _safe_str(f"Unreachable: {exc}", 140)
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
        return "ERR", "missing host/port", events_out
    try:
        resolved = resolve_mpc_host(host)
        with socket.create_connection((resolved, port), timeout=timeout_s):
            pass
        summary = f"{host}:{port} OPEN"
        _emit_status_transition(tid, state, "OK", summary, events_out)
        return "OK", summary, events_out
    except Exception as exc:
        summary = _safe_str(f"{host}:{port} closed ({exc})", 140)
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
        return "ERR", "missing ssh host", events_out
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
        summary = _safe_str(f"SSH error: {exc}", 140)
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
        summary = f"Waiting ({cfg.get('path') or cfg.get('path_glob')})"
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
            events_out.append(_make_event(tid, tid, "switched", f"New file: {resolved.name}", level="ok"))
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
    extra_note = f"+{len(events_out)}" if events_out else "up to date"
    summary = f"{resolved.name} ({size_kb:.1f}KB, {extra_note})"
    state["_last_status"] = "OK"
    return "OK", _safe_str(summary, 140), events_out


def _scan_dir_entries(cfg: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Tuple[int, int]]]:
    entries: Dict[str, Tuple[int, int]] = {}
    if cfg.get("path"):
        root = Path(os.path.expanduser(str(cfg["path"])))
        if not root.exists():
            return f"Folder not found: {root}", entries
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
    return "missing path or path_glob", entries


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
        return "WARN", "0 files", events_out
    latest_path = max(current.items(), key=lambda kv: kv[1][0])[0]
    summary = f"{len(current)} files · latest: {Path(latest_path).name}"
    return "OK", _safe_str(summary, 140), events_out


def _probe_cmd(target: Dict[str, Any], state: Dict[str, Any]) -> Tuple[str, str, List[Dict[str, Any]]]:
    tid = target["id"]
    cfg = target.get("config") or {}
    command = str(cfg.get("command") or "")
    timeout_s = float(cfg.get("timeout_s", 3.0))
    events_out: List[Dict[str, Any]] = []

    if not command:
        return "ERR", "missing command", events_out
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
        msg = _safe_str(f"Command error: {exc}", 140)
        _emit_status_transition(tid, state, "ERR", msg, events_out)
        return "ERR", msg, events_out


def _human_bytes(num_bytes: float, si: bool = False) -> str:
    """Format byte counts into compact human-readable size strings."""
    val = max(0.0, float(num_bytes))
    if val < 1.0:
        return "0 B"
    base = 1000.0 if si else 1024.0
    units = ("B", "KB", "MB", "GB", "TB", "PB") if si else ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    idx = min(len(units) - 1, int(math.floor(math.log(val, base))))
    scaled = val / (base ** idx)
    return f"{scaled:.0f} B" if idx == 0 else f"{scaled:.1f} {units[idx]}"


def _parse_cpu_stat(line: str) -> Optional[Tuple[int, int]]:
    parts = line.strip().split()
    if len(parts) < 5 or not parts[0].startswith("cpu"):
        return None
    try:
        vals = [int(x) for x in parts[1:]]
    except ValueError:
        return None
    total = sum(vals)
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return total, idle


def _compute_cpu_delta(curr: Optional[Tuple[int, int]], prev: Optional[Tuple[int, int]]) -> float:
    if prev and curr and curr[0] > prev[0]:
        dt = curr[0] - prev[0]
        di = max(0, curr[1] - prev[1])
        return round(max(0.0, min(100.0, 100.0 * (1.0 - (di / dt)))), 1)
    if curr and curr[0] > 0:
        return round(max(0.0, min(100.0, 100.0 * (1.0 - (curr[1] / curr[0])))), 1)
    return 0.0


def _parse_net_dev(lines: Sequence[str]) -> Dict[str, Tuple[int, int]]:
    out: Dict[str, Tuple[int, int]] = {}
    for ln in lines:
        if ":" not in ln:
            continue
        iface_part, rest = ln.split(":", 1)
        iface = iface_part.strip()
        if not iface or iface == "lo":
            continue
        cols = rest.split()
        if len(cols) >= 9:
            try:
                rx_bytes = int(cols[0])
                tx_bytes = int(cols[8])
            except ValueError:
                continue
            out[iface] = (rx_bytes, tx_bytes)
    return out


def _parse_human_kb(token: str) -> float:
    t = token.strip().lower()
    if not t:
        return 0.0
    mult = 1.0
    if t.endswith("g"):
        mult = 1024.0 * 1024.0
        t = t[:-1]
    elif t.endswith("m"):
        mult = 1024.0
        t = t[:-1]
    elif t.endswith("k"):
        t = t[:-1]
    try:
        return float(t) * mult
    except ValueError:
        return 0.0


def _parse_processes(lines: Sequence[str], mem_total_kb: int) -> List[Dict[str, Any]]:
    procs: List[Dict[str, Any]] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        rss_kb = 0
        if "|RSS=" in line:
            line, rss_part = line.rsplit("|RSS=", 1)
            line = line.strip()
            try:
                rss_kb = max(0, int(rss_part.strip()))
            except ValueError:
                rss_kb = 0
        parts = line.split(None, 7)
        if len(parts) < 8 or not parts[0].isdigit():
            continue
        cmd = parts[7].strip()
        if any(skip in cmd for skip in ("top -b -n 1", "awk NR<=4", "head -n 36", "sh -s")):
            continue
        # Normalize "{Thread Name} /usr/bin/Proc args..." cleanly for compact table display
        if cmd.startswith("{") and "}" in cmd:
            brace_end = cmd.find("}")
            thread_name = cmd[1:brace_end].strip()
            cmd_rest = cmd[brace_end + 1 :].strip().split()
            exe_name = Path(cmd_rest[0]).name if cmd_rest else ""
            cmd_display = thread_name or exe_name or cmd
        else:
            first_tok = cmd.split()[0]
            cmd_display = cmd if first_tok.startswith("[") else (Path(first_tok).name if "/" in first_tok else cmd)
        try:
            cpu_val = float(parts[6].rstrip("%"))
        except ValueError:
            cpu_val = 0.0
        try:
            vsz_pct = float(parts[5].rstrip("%"))
        except ValueError:
            vsz_pct = 0.0
        if mem_total_kb > 0 and ("|RSS=" in raw or rss_kb > 0):
            mem_val = round(min(100.0, max(0.0, 100.0 * rss_kb / mem_total_kb)), 1)
        else:
            mem_val = round(vsz_pct, 1)
        procs.append({
            "pid": int(parts[0]),
            "stat": parts[3],
            "cpu_pct": round(cpu_val, 1),
            "mem_pct": mem_val,
            "rss_kb": rss_kb if rss_kb > 0 else int(_parse_human_kb(parts[4])),
            "command": _safe_str(cmd_display, 48),
        })
    return _sort_processes(procs, sort_key="cpu", reverse=False)


def _sort_processes(procs: Sequence[Dict[str, Any]], sort_key: str = "cpu", reverse: bool = False) -> List[Dict[str, Any]]:
    key_map = {
        "pid": lambda p: (int(p.get("pid", 0)), str(p.get("command", ""))),
        "cpu": lambda p: (float(p.get("cpu_pct", 0.0)), float(p.get("mem_pct", 0.0)), int(p.get("pid", 0))),
        "mem": lambda p: (float(p.get("mem_pct", 0.0)), float(p.get("cpu_pct", 0.0)), int(p.get("pid", 0))),
    }
    key_fn = key_map.get(sort_key, key_map["cpu"])
    return sorted(procs, key=key_fn, reverse=not reverse)


def _format_uptime(seconds: float) -> str:
    total_s = max(0, int(seconds))
    days, rem = divmod(total_s, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days > 0:
        return f"{days}d {hours:02d}h {mins:02d}m"
    return f"{hours}h {mins:02d}m"


def _parse_mpc_sysinfo(
    raw_text: str,
    prev_cpu: Optional[Tuple[int, int]] = None,
    prev_state: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], Optional[Tuple[int, int]]]:
    sections: Dict[str, List[str]] = {}
    cur_sec: Optional[str] = None
    for line in raw_text.splitlines():
        s = line.strip()
        if s.startswith("==") and s.endswith("==") and len(s) > 4:
            cur_sec = s[2:-2]
            sections.setdefault(cur_sec, [])
        elif cur_sec is not None and s:
            sections[cur_sec].append(s)

    all_ips: List[Dict[str, str]] = []
    for ln in sections.get("IP", []):
        parts = ln.split()
        if len(parts) >= 2:
            all_ips.append({"iface": parts[0], "cidr": parts[1], "ip": parts[1].split("/", 1)[0]})
    primary_ip = all_ips[0]["ip"] if all_ips else "?"
    primary_iface = all_ips[0]["iface"] if all_ips else "?"

    def _parse_cpu_block(sec_lines: Sequence[str]) -> Dict[str, Tuple[int, int]]:
        out: Dict[str, Tuple[int, int]] = {}
        for ln in sec_lines:
            label = ln.split()[0] if ln.split() else ""
            parsed = _parse_cpu_stat(ln)
            if label and parsed is not None:
                out[label] = parsed
        return out

    cpus1 = _parse_cpu_block(sections.get("CPU1", []))
    cpus2 = _parse_cpu_block(sections.get("CPU2", []))
    prev_cpus = dict((prev_state or {}).get("cpus") or {})
    if prev_cpu and "cpu" not in prev_cpus:
        prev_cpus["cpu"] = prev_cpu

    base_cpus, curr_cpus = (cpus1, cpus2) if (cpus1 and cpus2) else (prev_cpus, cpus1)
    latest_cpus = cpus2 or cpus1 or prev_cpus
    cpu_pct = _compute_cpu_delta(curr_cpus.get("cpu"), base_cpus.get("cpu"))

    core_keys = sorted(
        (k for k in latest_cpus if k.startswith("cpu") and k[3:].isdigit()),
        key=lambda k: int(k[3:]),
    )
    per_core = [_compute_cpu_delta(curr_cpus.get(k), base_cpus.get(k)) for k in core_keys]

    mem_kb: Dict[str, int] = {}
    for ln in sections.get("MEM", []):
        parts = ln.split()
        if len(parts) >= 2 and parts[1].isdigit():
            mem_kb[parts[0].rstrip(":")] = int(parts[1])
    mem_total_kb = mem_kb.get("MemTotal", 0)
    mem_avail_kb = mem_kb.get("MemAvailable", 0)
    mem_used_kb = max(0, mem_total_kb - mem_avail_kb)
    ram_pct = round(100.0 * mem_used_kb / mem_total_kb, 1) if mem_total_kb > 0 else 0.0

    swap_total_kb = mem_kb.get("SwapTotal", 0)
    swap_free_kb = mem_kb.get("SwapFree", 0)
    swap_used_kb = max(0, swap_total_kb - swap_free_kb)
    swap_pct = round(100.0 * swap_used_kb / swap_total_kb, 1) if swap_total_kb > 0 else 0.0

    load_parts = sections["LOAD"][0].split() if sections.get("LOAD") else []
    load_str = f"{load_parts[0]} {load_parts[1]}" if len(load_parts) >= 2 else "?"
    uptime_s = 0.0
    if len(sections.get("LOAD", [])) >= 2:
        try:
            uptime_s = float(sections["LOAD"][1].split()[0])
        except (ValueError, IndexError):
            uptime_s = 0.0
    up2_s = uptime_s
    if sections.get("UP2"):
        try:
            up2_s = float(sections["UP2"][0].split()[0])
        except (ValueError, IndexError):
            up2_s = uptime_s

    temp_c: Optional[float] = None
    if sections.get("TEMP"):
        try:
            raw_t = float(sections["TEMP"][0])
            temp_c = round(raw_t / 1000.0 if raw_t > 200 else raw_t, 1)
        except ValueError:
            temp_c = None

    # Network stats from /proc/net/dev (delta across NET1->NET2 or previous probe)
    net1 = _parse_net_dev(sections.get("NET1", []))
    net2 = _parse_net_dev(sections.get("NET2", []))
    prev_net_map = dict((prev_state or {}).get("net") or {})
    prev_up_s = (prev_state or {}).get("uptime_s")
    if net1 and net2:
        net_base, net_curr = net1, net2
        net_dt = max(0.05, up2_s - uptime_s)
        final_up_s = up2_s
    else:
        net_base, net_curr = prev_net_map, (net2 or net1)
        net_dt = (uptime_s - float(prev_up_s)) if (prev_up_s is not None and uptime_s > float(prev_up_s)) else 0.0
        final_up_s = uptime_s
    latest_net = net2 or net1 or prev_net_map
    net_iface = primary_iface if primary_iface in latest_net else (next(iter(latest_net), primary_iface))
    rx_bytes, tx_bytes = latest_net.get(net_iface, (0, 0))
    rx_sec = 0.0
    tx_sec = 0.0
    if net_iface in net_curr and net_iface in net_base and net_dt > 0.0:
        b_rx, b_tx = net_base[net_iface]
        c_rx, c_tx = net_curr[net_iface]
        if c_rx >= b_rx:
            rx_sec = round((c_rx - b_rx) / net_dt, 1)
        if c_tx >= b_tx:
            tx_sec = round((c_tx - b_tx) / net_dt, 1)

    label_map = {"/media/okcomputer": "okcomputer", "/sdcard": "sdcard", "/data": "data", "/": "root"}
    order_map = {"/media/okcomputer": 0, "/sdcard": 1, "/data": 2, "/": 3}
    disks_by_mount: Dict[str, Dict[str, Any]] = {}
    for ln in sections.get("DF", []):
        if ln.startswith("Filesystem"):
            continue
        parts = ln.split()
        if len(parts) < 6:
            continue
        mount = parts[5]
        pct_raw = parts[4].rstrip("%")
        pct = float(pct_raw) if pct_raw.replace(".", "", 1).isdigit() else 0.0
        disks_by_mount[mount] = {
            "mount": mount,
            "label": label_map.get(mount, Path(mount).name or "root"),
            "size": parts[1],
            "used": parts[2],
            "avail": parts[3],
            "pct": pct,
        }
    disks = sorted(disks_by_mount.values(), key=lambda d: (order_map.get(d["mount"], 99), d["mount"]))

    processes = _parse_processes(sections.get("TOP", []), mem_total_kb)

    metrics = {
        "ip": primary_ip,
        "iface": primary_iface,
        "all_ips": all_ips,
        "cpu_pct": cpu_pct,
        "per_core": per_core,
        "core_count": len(per_core),
        "ram_pct": ram_pct,
        "ram_used_mb": round(mem_used_kb / 1024),
        "ram_total_mb": round(mem_total_kb / 1024),
        "ram_used_bytes": mem_used_kb * 1024,
        "ram_total_bytes": mem_total_kb * 1024,
        "swap_pct": swap_pct,
        "swap_used_mb": round(swap_used_kb / 1024),
        "swap_total_mb": round(swap_total_kb / 1024),
        "swap_used_bytes": swap_used_kb * 1024,
        "swap_total_bytes": swap_total_kb * 1024,
        "load": load_str,
        "temp_c": temp_c,
        "uptime_s": round(final_up_s, 1),
        "uptime_str": _format_uptime(final_up_s),
        "net": {
            "iface": net_iface,
            "rx_sec": rx_sec,
            "tx_sec": tx_sec,
            "rx_bytes": rx_bytes,
            "tx_bytes": tx_bytes,
        },
        "disks": disks,
        "processes": processes,
        "_state": {
            "cpus": latest_cpus,
            "net": latest_net,
            "uptime_s": final_up_s,
        },
    }
    return metrics, latest_cpus.get("cpu") or prev_cpu


_SYSINFO_BASE_SCRIPT = (
    "echo '==IP=='\n"
    "ip -4 addr show 2>/dev/null | awk '/inet / && $NF != \"lo\" {print $NF,$2}'\n"
    "echo '==CPU1=='\ngrep '^cpu' /proc/stat\n"
    "echo '==NET1=='\ncat /proc/net/dev 2>/dev/null || true\n"
    "echo '==MEM=='\nawk '/^(MemTotal|MemAvailable|SwapTotal|SwapFree):/ {print $1,$2}' /proc/meminfo\n"
    "echo '==LOAD=='\ncat /proc/loadavg\ncat /proc/uptime\n"
    "echo '==TEMP=='\ncat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || true\n"
    "echo '==DF=='\ndf -P -h / /data /sdcard /media/okcomputer 2>/dev/null || df -h / /data /sdcard /media/okcomputer\n"
    "echo '==TOP=='\n"
    "top -b -n 1 2>/dev/null | head -n 36 | awk 'NR<=4{next} {pid=$1; f=\"/proc/\"pid\"/statm\"; rss=0; if ((getline l < f) > 0) {split(l,a); rss=a[2]*4; close(f)}; print $0 \" |RSS=\" rss}'\n"
)


def _record_sys_history(state: Dict[str, Any], metrics: Dict[str, Any]) -> Dict[str, Any]:
    hist = state.setdefault("history", {})
    cpu_total = hist.setdefault("cpu_total", deque(maxlen=DASH_HISTORY_LEN))
    cpu_cores = hist.setdefault("cpu_cores", [])
    mem_hist = hist.setdefault("mem_pct", deque(maxlen=DASH_HISTORY_LEN))
    swap_hist = hist.setdefault("swap_pct", deque(maxlen=DASH_HISTORY_LEN))
    rx_hist = hist.setdefault("net_rx_sec", deque(maxlen=DASH_HISTORY_LEN))
    tx_hist = hist.setdefault("net_tx_sec", deque(maxlen=DASH_HISTORY_LEN))

    cpu_total.append(float(metrics.get("cpu_pct", 0.0)))
    per_core = metrics.get("per_core") or []
    while len(cpu_cores) < len(per_core):
        cpu_cores.append(deque(maxlen=DASH_HISTORY_LEN))
    for i, val in enumerate(per_core):
        cpu_cores[i].append(float(val))
    mem_hist.append(float(metrics.get("ram_pct", 0.0)))
    swap_hist.append(float(metrics.get("swap_pct", 0.0)))
    net = metrics.get("net") or {}
    rx_hist.append(float(net.get("rx_sec", 0.0)))
    tx_hist.append(float(net.get("tx_sec", 0.0)))

    return {
        "cpu_total": list(cpu_total),
        "cpu_cores": [list(c) for c in cpu_cores],
        "mem_pct": list(mem_hist),
        "swap_pct": list(swap_hist),
        "net_rx_sec": list(rx_hist),
        "net_tx_sec": list(tx_hist),
    }


def _probe_mpc_system(target: Dict[str, Any], state: Dict[str, Any]) -> Tuple[str, str, List[Dict[str, Any]]]:
    tid = target["id"]
    cfg = target.get("config") or {}
    host = str(cfg.get("host") or "root@mpc-live.local")
    timeout_s = float(cfg.get("timeout_s", 4.0))
    events_out: List[Dict[str, Any]] = []

    need_baseline_sample = "_prev_state" not in state
    tail_script = (
        "sleep 0.12\necho '==CPU2=='\ngrep '^cpu' /proc/stat\n"
        "echo '==NET2=='\ncat /proc/net/dev 2>/dev/null || true\n"
        "echo '==UP2=='\ncat /proc/uptime\n"
        if need_baseline_sample
        else ""
    )
    remote_script = _SYSINFO_BASE_SCRIPT + tail_script
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={max(1, int(timeout_s))}", host, "sh", "-s"],
            input=remote_script,
            capture_output=True,
            text=True,
            timeout=timeout_s + 2.5,
        )
        if proc.returncode != 0:
            err = _safe_str(proc.stderr.strip() or f"exit {proc.returncode}", 140)
            _emit_status_transition(tid, state, "ERR", err, events_out)
            return "ERR", err, events_out

        metrics, new_prev_cpu = _parse_mpc_sysinfo(
            proc.stdout,
            prev_cpu=state.get("_prev_cpu"),
            prev_state=state.get("_prev_state"),
        )
        state["_prev_cpu"] = new_prev_cpu
        state["_prev_state"] = metrics.pop("_state", {})
        metrics["history"] = _record_sys_history(state, metrics)
        prev_ip = state.get("_last_ip")
        state["_last_ip"] = metrics["ip"]
        state["metrics"] = metrics

        if prev_ip is not None and metrics["ip"] != prev_ip:
            events_out.append(_make_event(tid, "mpc-sys", "ip_changed", f"{prev_ip} -> {metrics['ip']}", level="warn"))

        max_disk_pct = max((d["pct"] for d in metrics["disks"]), default=0.0)
        high_warn = metrics["cpu_pct"] >= 90.0 or metrics["ram_pct"] >= 92.0 or max_disk_pct >= 90.0
        status_code = "WARN" if high_warn else "OK"

        short_names = {"okcomputer": "ok", "sdcard": "sd", "data": "data", "root": "/"}
        hdd_compact = " ".join(f"{short_names.get(d['label'], d['label'])}:{d['pct']:.0f}%" for d in metrics["disks"])
        temp_part = f" {metrics['temp_c']}°C" if metrics["temp_c"] is not None else ""
        net = metrics["net"]
        net_part = f" | ↓{_human_bytes(net['rx_sec'])}/s ↑{_human_bytes(net['tx_sec'])}/s"
        summary = f"IP {metrics['ip']} | CPU {metrics['cpu_pct']:.1f}%{temp_part} | RAM {metrics['ram_pct']:.1f}%{net_part} | {hdd_compact}"
        summary = _safe_str(summary, 140)
        _emit_status_transition(tid, state, status_code, summary, events_out)
        return status_code, summary, events_out
    except Exception as exc:
        msg = _safe_str(f"System query error: {exc}", 140)
        _emit_status_transition(tid, state, "ERR", msg, events_out)
        return "ERR", msg, events_out


PROBE_HANDLERS: Dict[str, Callable[[Dict[str, Any], Dict[str, Any]], Tuple[str, str, List[Dict[str, Any]]]]] = {
    "mpc_bridge": _probe_mpc_bridge,
    "mpc_system": _probe_mpc_system,
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
        raise ValueError("Target id cannot be empty")
    if kind not in PROBE_HANDLERS:
        raise ValueError(f"Unsupported kind '{kind}'. Valid: {sorted(PROBE_HANDLERS)}")
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
            raise ValueError(f"Invalid tcp format '{spec}' (expected host:port)")
        host, port_s = spec.rsplit(":", 1)
        if not host.strip() or not port_s.strip().isdigit():
            raise ValueError(f"Invalid tcp host:port '{spec}'")
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
    elif kind == "mpc_system":
        cfg = {"host": spec.strip() or "root@mpc-live.local", "timeout_s": 4.0}
    else:
        raise ValueError(f"Invalid kind: {kind}")

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
                        "summary": "Pending..." if t.get("enabled", True) else "Disabled",
                        "last_checked": 0.0,
                        "next_due": 0.0,
                        "cursor": {},
                    },
                )
                if not t.get("enabled", True):
                    st["status"] = "OFF"
                    st["summary"] = "Disabled"
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
                if "metrics" in cursor:
                    st["metrics"] = dict(cursor["metrics"])
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
                    **({"metrics": st["metrics"]} if "metrics" in st else {}),
                })
            return rows, list(self.events)


# ---------------------------------------------------------------------------
# Headless snapshot & ANSI stream renderers
# ---------------------------------------------------------------------------

_BRAILLE_BITS = (
    (0x01, 0x08),
    (0x02, 0x10),
    (0x04, 0x20),
    (0x40, 0x80),
)
_SPARK_CHARS = " ▁▂▃▄▅▆▇█"


def _render_sparkline(values: Sequence[float], width: int, max_val: Optional[float] = None) -> str:
    if width <= 0:
        return ""
    pts = [max(0.0, float(v)) for v in values[-width:]]
    if not pts:
        return " " * width
    top = float(max_val) if max_val is not None else max(1.0, max(pts))
    pad_left = " " * max(0, width - len(pts))
    chars = []
    for v in pts:
        if v <= 0.0:
            chars.append("▁")
        else:
            idx = min(len(_SPARK_CHARS) - 1, max(1, int(round((v / top) * (len(_SPARK_CHARS) - 1)))))
            chars.append(_SPARK_CHARS[idx])
    return pad_left + "".join(chars)


def _render_braille_chart(
    series_list: Sequence[Sequence[float]],
    width: int,
    height: int,
    max_val: Optional[float] = 100.0,
) -> List[List[Tuple[str, int]]]:
    """Render continuous multi-series line plots into a (height x width) 2x4 Braille cell grid."""
    cols = max(1, int(width))
    rows = max(1, int(height))
    px_w = cols * 2
    px_h = rows * 4

    top = float(max_val) if max_val is not None else 1.0
    if max_val is None:
        flat_max = max((float(v) for s in series_list for v in s), default=0.0)
        top = max(1.0, flat_max * 1.08)

    # cell_bits[row][col] -> dict[series_idx, dot_bitmask]
    cell_bits: List[List[Dict[int, int]]] = [[{} for _ in range(cols)] for _ in range(rows)]

    def _plot_px(px: int, py: int, s_idx: int) -> None:
        if 0 <= px < px_w and 0 <= py < px_h:
            cy, dy = divmod(py, 4)
            cx, dx = divmod(px, 2)
            bit = _BRAILLE_BITS[dy][dx]
            d = cell_bits[cy][cx]
            d[s_idx] = d.get(s_idx, 0) | bit

    for s_idx, raw_vals in enumerate(series_list):
        pts = [max(0.0, float(v)) for v in raw_vals[-px_w:]]
        if not pts:
            continue
        x_offset = px_w - len(pts)
        mapped: List[Tuple[int, int]] = []
        for i, v in enumerate(pts):
            ratio = min(1.0, max(0.0, v / top))
            py = int(round((1.0 - ratio) * (px_h - 1)))
            mapped.append((x_offset + i, py))

        for i in range(len(mapped)):
            x0, y0 = mapped[i]
            _plot_px(x0, y0, s_idx)
            if i + 1 < len(mapped):
                x1, y1 = mapped[i + 1]
                steps = max(1, abs(x1 - x0), abs(y1 - y0))
                for step in range(1, steps + 1):
                    px = int(round(x0 + (x1 - x0) * (step / steps)))
                    py = int(round(y0 + (y1 - y0) * (step / steps)))
                    _plot_px(px, py, s_idx)

    grid: List[List[Tuple[str, int]]] = []
    for r in range(rows):
        row_cells: List[Tuple[str, int]] = []
        for c in range(cols):
            contrib = cell_bits[r][c]
            if not contrib:
                row_cells.append((" ", -1))
                continue
            combined = 0
            winner_idx = -1
            winner_score = -1
            for s_idx, mask in contrib.items():
                combined |= mask
                score = bin(mask).count("1") * 100 - s_idx
                if score > winner_score:
                    winner_score = score
                    winner_idx = s_idx
            row_cells.append((chr(0x2800 | combined), winner_idx))
        grid.append(row_cells)
    return grid


def _render_braille_donut(pct: float, width: int = 8, height: int = 4) -> List[List[Tuple[str, int]]]:
    """Render a circular progress donut gauge (1=filled, 0=track, -1=blank) in Braille."""
    cols = max(3, int(width))
    rows = max(2, int(height))
    px_w = cols * 2
    px_h = rows * 4
    cx = (px_w - 1) / 2.0
    cy = (px_h - 1) / 2.0
    r_outer = max(2.0, min(cx, cy) - 0.2)
    r_inner = r_outer * 0.54
    frac = max(0.0, min(100.0, float(pct))) / 100.0

    grid: List[List[Tuple[str, int]]] = []
    for r in range(rows):
        row_cells: List[Tuple[str, int]] = []
        for c in range(cols):
            fill_mask = 0
            track_mask = 0
            for dy in range(4):
                for dx in range(2):
                    px = c * 2 + dx
                    py = r * 4 + dy
                    dist = math.hypot(px - cx, py - cy)
                    if r_inner <= dist <= r_outer:
                        angle = (math.atan2(px - cx, -(py - cy)) % (2.0 * math.pi)) / (2.0 * math.pi)
                        bit = _BRAILLE_BITS[dy][dx]
                        if frac > 0.0 and angle <= frac:
                            fill_mask |= bit
                        else:
                            track_mask |= bit
            if fill_mask:
                row_cells.append((chr(0x2800 | fill_mask | track_mask), 1))
            elif track_mask:
                row_cells.append((chr(0x2800 | track_mask), 0))
            else:
                row_cells.append((" ", -1))
        grid.append(row_cells)
    return grid


def _pct_bar(pct: float, width: int = 8) -> str:
    clamped = max(0.0, min(100.0, float(pct)))
    filled = round((clamped / 100.0) * width)
    return "[" + ("█" * filled) + ("░" * (width - filled)) + "]"


def _pct_level(pct: float) -> str:
    if pct >= 90.0:
        return "ERR"
    if pct >= 70.0:
        return "WARN"
    return "OK"


def _find_system_metrics(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for r in rows:
        m = r.get("metrics")
        if isinstance(m, dict) and "ip" in m:
            return m
    return None


def _print_ansi_snapshot(rows: List[Dict[str, Any]], events: List[Dict[str, Any]]) -> None:
    print(f"{ANSI_CRIMSON_BG} MPC LIVE {ANSI_HDR_BAR} LIVE MONITOR ({_now_iso()}) {ANSI_RESET}")
    sys_m = _find_system_metrics(rows)
    if sys_m:
        cpu_c = ANSI_COLORS.get(_pct_level(sys_m["cpu_pct"]), "")
        ram_c = ANSI_COLORS.get(_pct_level(sys_m["ram_pct"]), "")
        temp_s = f" | Temp: {sys_m['temp_c']}°C" if sys_m.get("temp_c") is not None else ""
        cores = sys_m.get("per_core") or []
        cores_s = (" [" + " ".join(f"C{i+1}:{c:.0f}%" for i, c in enumerate(cores)) + "]") if cores else ""
        swap_s = (
            f" · Swap {sys_m.get('swap_pct', 0):.0f}% ({sys_m.get('swap_used_mb', 0)}/{sys_m.get('swap_total_mb', 0)}MB)"
            if sys_m.get("swap_total_mb", 0) > 0
            else " · Swap: 0"
        )
        print(
            f"{ANSI_HDR_BAR} MPC SYSTEM {ANSI_RESET} "
            f"IP: {ANSI_BOLD}{sys_m['ip']}{ANSI_RESET} ({sys_m['iface']})  "
            f"CPU: {cpu_c}{sys_m['cpu_pct']:.1f}% {_pct_bar(sys_m['cpu_pct'])}{ANSI_RESET}{cores_s} "
            f"(load {sys_m['load']}{temp_s})  "
            f"RAM: {ram_c}{sys_m['ram_pct']:.1f}% {_pct_bar(sys_m['ram_pct'])}{ANSI_RESET} "
            f"({sys_m['ram_used_mb']}/{sys_m['ram_total_mb']}MB{swap_s})  UP: {sys_m['uptime_str']}"
        )
        net = sys_m.get("net") or {}
        if net:
            print(
                f"  {ANSI_DIM}NET  {net.get('iface', '?'):<11}{ANSI_RESET} "
                f"↓ {ANSI_COLORS['OK']}{_human_bytes(net.get('rx_sec', 0.0))}/s{ANSI_RESET} "
                f"(Total: {_human_bytes(net.get('rx_bytes', 0))})  "
                f"↑ {ANSI_COLORS['WARN']}{_human_bytes(net.get('tx_sec', 0.0))}/s{ANSI_RESET} "
                f"(Total: {_human_bytes(net.get('tx_bytes', 0))})"
            )
        for d in sys_m.get("disks", []):
            dc = ANSI_COLORS.get(_pct_level(d["pct"]), "")
            print(
                f"  {ANSI_DIM}DISK {d['label']:<11} ({d['mount']:<17}){ANSI_RESET} "
                f"{dc}{d['pct']:>4.0f}% {_pct_bar(d['pct'], 10)}{ANSI_RESET}  "
                f"Used: {d['used']:>6} / {d['size']:<6}  Free: {d['avail']}"
            )
        top_procs = (sys_m.get("processes") or [])[:5]
        if top_procs:
            proc_str = " │ ".join(f"{p['command']} ({p['cpu_pct']:.0f}% CPU, {p['mem_pct']:.1f}% RAM)" for p in top_procs[:4])
            print(f"  {ANSI_DIM}TOP PROCESSES:{ANSI_RESET} {proc_str}")
    print(f"{ANSI_DIM}{'ID':<19} {'KIND':<11} {'STATUS':<6} {'MS':>6}  {'SUMMARY'}{ANSI_RESET}")
    print(f"{ANSI_DIM}{'-' * 88}{ANSI_RESET}")
    for r in rows:
        st = r["status"]
        col = ANSI_COLORS.get(st, "")
        ms = f"{r['latency_ms']:.0f}" if isinstance(r["latency_ms"], (int, float)) else "-"
        mark = "●" if r["enabled"] else "○"
        print(f"{col}{mark} {r['id']:<17}{ANSI_RESET} {ANSI_DIM}{r['kind']:<11}{ANSI_RESET} {col}{st:<6}{ANSI_RESET} {ms:>6}  {r['summary']}")
    print(f"\n{ANSI_HDR_BAR} LIVE FEED (last {min(30, len(events))} events) {ANSI_RESET}")
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
    print(f"{ANSI_CRIMSON_BG} MPC LIVE {ANSI_HDR_BAR} Live stream started ({engine.targets_path}) · Ctrl+C to exit {ANSI_RESET}", flush=True)
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
        print("\nStream stopped.", flush=True)
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
    kinds_help = "Select kind [http | tcp | ssh | mpc_bridge | mpc_system | log_tail | dir_watch | cmd]: "
    kind = _prompt_line(stdscr, kinds_help, colors)
    if not kind:
        return "Add target cancelled."
    kind = kind.strip()
    if kind not in PROBE_HANDLERS:
        return f"Invalid kind: {kind}"

    spec_prompts = {
        "http": "URL (e.g. http://127.0.0.1:8000/health): ",
        "tcp": "host:port (e.g. mpc-live.local:6731): ",
        "ssh": "user@host::command (e.g. root@mpc-live.local::uptime): ",
        "log_tail": "File or glob path: ",
        "dir_watch": "Folder or glob path: ",
        "cmd": "Shell command to run: ",
        "mpc_bridge": "MPC IP/host[:port] (e.g. mpc-live.local:6731): ",
        "mpc_system": "SSH target [root@mpc-live.local]: ",
    }
    tid = _prompt_line(stdscr, "Short ID (e.g. audio-inbox, studio-api): ", colors)
    if not tid:
        return "Cancelled."
    spec = _prompt_line(stdscr, spec_prompts[kind], colors)
    if not spec and kind == "mpc_system":
        spec = "root@mpc-live.local"
    if not spec:
        return "Cancelled."
    interval_raw = _prompt_line(stdscr, "Refresh interval seconds [2.0]: ", colors, "2.0")
    try:
        interval_s = float(interval_raw or "2.0")
    except ValueError:
        return f"Invalid interval number: {interval_raw}"

    entry = build_target_entry(tid, kind, spec, interval_s=interval_s)
    add_target(entry, engine.targets_path)
    engine.reload_targets()
    engine.force_refresh()
    return f"Target added: {entry['id']} ({kind})"


def _draw_segments(
    stdscr: Any,
    y: int,
    w: int,
    segments: List[Tuple[str, int]],
    x_start: int = 0,
    x_limit: Optional[int] = None,
) -> None:
    max_x = min(w - 1, x_limit) if x_limit is not None else (w - 1)
    x = x_start
    for txt, attr in segments:
        if x >= max_x or not txt:
            continue
        stdscr.addnstr(y, x, txt, max_x - x, attr)
        x += len(txt)


def _draw_box(
    stdscr: Any,
    y: int,
    x: int,
    h: int,
    w: int,
    title: str,
    border_attr: int,
    title_attr: int,
) -> Tuple[int, int, int, int]:
    """Draw a rounded Unicode frame bounded inside screen dims and return inner (iy, ix, ih, iw)."""
    scr_h, scr_w = stdscr.getmaxyx()
    if h < 3 or w < 6 or y < 0 or x < 0 or y + h > scr_h or x + w >= scr_w:
        return y, x, max(0, h), max(0, w)
    inner_w = w - 2
    stdscr.addnstr(y, x, "╭" + ("─" * inner_w) + "╮", w, border_attr)
    if title and inner_w > 4:
        lbl = f" {title[: inner_w - 3]} "
        stdscr.addnstr(y, x + 2, lbl, min(len(lbl), inner_w - 2), title_attr)
    for row_off in range(1, h - 1):
        stdscr.addnstr(y + row_off, x, "│", 1, border_attr)
        stdscr.addnstr(y + row_off, x + w - 1, "│", 1, border_attr)
    stdscr.addnstr(y + h - 1, x, "╰" + ("─" * inner_w) + "╯", w, border_attr)
    return y + 1, x + 1, h - 2, inner_w


def _draw_tui_sysinfo(stdscr: Any, colors: Dict[str, int], sys_m: Dict[str, Any], w: int, y_start: int, rows_budget: int = 3) -> int:
    if rows_budget < 2:
        return 0
    muted = colors.get("muted", 0)
    txt = colors.get("canvas", 0)
    hdr = colors.get("sidebar_hdr", curses.A_BOLD)
    cpu_pct = float(sys_m.get("cpu_pct", 0.0))
    ram_pct = float(sys_m.get("ram_pct", 0.0))
    cpu_attr = colors.get(_pct_level(cpu_pct), txt) | curses.A_BOLD
    ram_attr = colors.get(_pct_level(ram_pct), txt) | curses.A_BOLD
    temp_s = f" {sys_m['temp_c']}°C" if sys_m.get("temp_c") is not None else ""
    hist = sys_m.get("history") or {}
    cpu_spark = _render_sparkline(hist.get("cpu_total") or [cpu_pct], 10, 100.0)
    cores = sys_m.get("per_core") or []
    cores_s = (" [" + " ".join(f"C{i+1}:{c:.0f}%" for i, c in enumerate(cores[:4])) + "]") if (cores and w >= 126) else ""

    row1: List[Tuple[str, int]] = [
        (" IP ", hdr),
        (f"{sys_m.get('ip', '?')}", colors.get("INIT", txt) | curses.A_BOLD),
        (f" ({sys_m.get('iface', '?')})  ", muted),
        ("CPU ", hdr),
        (f"{cpu_pct:.1f}% {_pct_bar(cpu_pct, 6)}", cpu_attr),
        (f" {cpu_spark}", colors.get("midi", txt)),
        (f"{cores_s} ({sys_m.get('load', '?')}{temp_s})  ", muted),
        ("UP ", hdr),
        (f"{sys_m.get('uptime_str', '?')}", muted),
    ]
    _draw_segments(stdscr, y_start, w, row1)

    if rows_budget >= 3:
        net = sys_m.get("net") or {}
        rx_s = _human_bytes(float(net.get("rx_sec", 0.0)))
        tx_s = _human_bytes(float(net.get("tx_sec", 0.0)))
        rx_tot = _human_bytes(float(net.get("rx_bytes", 0)))
        tx_tot = _human_bytes(float(net.get("tx_bytes", 0)))
        net_spark = _render_sparkline(
            [r + t for r, t in zip(hist.get("net_rx_sec") or [], hist.get("net_tx_sec") or [])] or [0.0],
            8,
            None,
        )
        top_procs = sys_m.get("processes") or []
        top_s = f"{top_procs[0]['command']} {top_procs[0]['cpu_pct']:.0f}%" if top_procs else "-"
        swap_s = f" · Sw:{sys_m.get('swap_pct', 0):.0f}%" if sys_m.get("swap_total_mb", 0) > 0 else ""
        row_mid: List[Tuple[str, int]] = [
            (" RAM ", hdr),
            (f"{ram_pct:.1f}% {_pct_bar(ram_pct, 6)}", ram_attr),
            (f" {sys_m.get('ram_used_mb', 0)}/{sys_m.get('ram_total_mb', 0)}MB{swap_s}  ", muted),
            ("NET ", hdr),
            (f"↓{rx_s}/s ", colors.get("OK", txt) | curses.A_BOLD),
            (f"↑{tx_s}/s ", colors.get("WARN", txt) | curses.A_BOLD),
            (f"{net_spark} ", colors.get("INIT", txt)),
            (f"(Σ↓{rx_tot} ↑{tx_tot})  ", muted),
            ("TOP ", hdr),
            (top_s, colors.get("agent", txt)),
        ]
        _draw_segments(stdscr, y_start + 1, w, row_mid)
        disk_y = y_start + 2
        used_rows = 3
    else:
        disk_y = y_start + 1
        used_rows = 2

    disks = sys_m.get("disks") or []
    show_sizes = w >= 115
    bar_w = 5 if w >= 115 else 0
    row_disk: List[Tuple[str, int]] = [(" DISK ", hdr)]
    for i, d in enumerate(disks):
        if i > 0:
            row_disk.append((" │ ", muted))
        pct = float(d.get("pct", 0.0))
        d_attr = colors.get(_pct_level(pct), txt) | curses.A_BOLD
        pct_txt = f"{pct:.0f}%" + (f" {_pct_bar(pct, bar_w)}" if bar_w else "")
        row_disk.append((f"{d['label']} ", txt))
        row_disk.append((pct_txt, d_attr))
        if show_sizes:
            row_disk.append((f" ({d['used']}/{d['size']})", muted))
    _draw_segments(stdscr, disk_y, w, row_disk)
    return used_rows


def _draw_braille_widget(
    stdscr: Any,
    iy: int,
    ix: int,
    ih: int,
    iw: int,
    grid: List[List[Tuple[str, int]]],
    series_attrs: Sequence[int],
) -> None:
    max_r = min(ih, len(grid))
    max_c = min(iw, len(grid[0]) if grid else 0)
    for r in range(max_r):
        line_cells = grid[r][:max_c]
        col_x = 0
        while col_x < len(line_cells):
            ch, s_idx = line_cells[col_x]
            if s_idx < 0:
                col_x += 1
                continue
            attr = series_attrs[s_idx % len(series_attrs)] if series_attrs else 0
            # Batch contiguous same-style cells to keep curses calls minimal.
            run_end = col_x + 1
            chars = [ch]
            while run_end < len(line_cells) and line_cells[run_end][1] == s_idx:
                chars.append(line_cells[run_end][0])
                run_end += 1
            stdscr.addnstr(iy + r, ix + col_x, "".join(chars), max_c - col_x, attr)
            col_x = run_end


def _draw_donut_card(
    stdscr: Any,
    y: int,
    x: int,
    h: int,
    w: int,
    title: str,
    pct: float,
    value_label: str,
    sub_label: str,
    colors: Dict[str, int],
    fill_attr: int,
) -> None:
    muted = colors.get("muted", 0)
    txt = colors.get("canvas", 0)
    iy, ix, ih, iw = _draw_box(stdscr, y, x, h, w, title, muted, colors.get("topbar_gold", curses.A_BOLD))
    if ih < 2 or iw < 8:
        return
    donut_w = min(8, max(5, iw // 2 - 1))
    donut_h = min(ih, 4)
    donut = _render_braille_donut(pct, width=donut_w, height=donut_h)
    top_off = max(0, (ih - donut_h) // 2)
    _draw_braille_widget(stdscr, iy + top_off, ix + 1, donut_h, donut_w, donut, [muted, fill_attr])
    tx = ix + donut_w + 3
    rem_w = (ix + iw) - tx
    if rem_w >= 4:
        mid_y = iy + max(0, ih // 2 - 1)
        stdscr.addnstr(mid_y, tx, f"{pct:.0f}%", rem_w, fill_attr | curses.A_BOLD)
        if mid_y + 1 < iy + ih:
            stdscr.addnstr(mid_y + 1, tx, value_label[:rem_w], rem_w, txt | curses.A_BOLD)
        if mid_y + 2 < iy + ih and sub_label:
            stdscr.addnstr(mid_y + 2, tx, sub_label[:rem_w], rem_w, muted)


def _draw_tui_dash(
    stdscr: Any,
    colors: Dict[str, int],
    sys_m: Optional[Dict[str, Any]],
    h: int,
    w: int,
    proc_selected: int,
    proc_sort: str,
    proc_reverse: bool,
) -> int:
    """Render full interactive hardware & process dashboard adapted to Akai MPC Live telemetry."""
    muted = colors.get("muted", 0)
    txt = colors.get("canvas", 0)
    hdr = colors.get("sidebar_hdr", curses.A_BOLD)
    title_attr = colors.get("topbar_gold", curses.A_BOLD)
    series_palette = [
        colors.get("midi", curses.A_BOLD),   # CPU1 / RAM magenta
        colors.get("INIT", curses.A_BOLD),   # CPU2 / Swap teal-cyan
        colors.get("agent", curses.A_BOLD),  # CPU3 cobalt blue
        colors.get("WARN", curses.A_BOLD),   # CPU4 gold
    ]
    scr_w = w - 1  # Keep rightmost column empty to avoid curses bottom-right wrap errors
    avail_h = h - 3
    if avail_h < 12 or scr_w < 56:
        stdscr.addnstr(2, 2, "Widen terminal for Dash view (min 56x16)", max(1, scr_w - 3), colors.get("WARN", 0))
        return proc_selected

    if sys_m is None:
        _draw_box(stdscr, 1, 0, avail_h, scr_w, "Dash — MPC Live System", muted, title_attr)
        stdscr.addnstr(3, 3, "Waiting for MPC Live telemetry (SSH mpc-live.local)...", max(1, scr_w - 6), colors.get("INIT", 0))
        return proc_selected

    hist = sys_m.get("history") or {}
    cpu_h = max(6, avail_h * 31 // 100)
    mid_h = max(6, avail_h * 31 // 100)
    bot_h = max(6, avail_h - cpu_h - mid_h)

    # 1) Top Row: Multi-core CPU History (Braille line chart + legend)
    iy, ix, ih, iw = _draw_box(stdscr, 1, 0, cpu_h, scr_w, "CPU History (Cores 1-4)", muted, title_attr)
    per_core = sys_m.get("per_core") or [float(sys_m.get("cpu_pct", 0.0))]
    core_hist = hist.get("cpu_cores") or [[float(v)] for v in per_core]
    legend_w = 27 if iw >= 64 else 18
    y_axis_w = 5
    chart_w = max(8, iw - legend_w - y_axis_w)
    if ih >= 2 and chart_w >= 8:
        for yr, lbl in ((0, "100%"), (max(0, ih // 2), " 50%"), (ih - 1, "  0%")):
            if yr < ih:
                stdscr.addnstr(iy + yr, ix, lbl, y_axis_w, muted)
        cpu_grid = _render_braille_chart(core_hist, chart_w, ih, max_val=100.0)
        _draw_braille_widget(stdscr, iy, ix + y_axis_w, ih, chart_w, cpu_grid, series_palette)
    lx = ix + y_axis_w + chart_w + 1
    rem_lw = max(0, (ix + iw) - lx)
    if rem_lw >= 10:
        for c_idx, val in enumerate(per_core[: max(0, ih)]):
            attr = series_palette[c_idx % len(series_palette)]
            lbl = f"● CPU{c_idx + 1} {val:>5.1f}%"
            stdscr.addnstr(iy + c_idx, lx, lbl[:rem_lw], rem_lw, attr)
        meta_y = iy + min(len(per_core), ih)
        temp_txt = f"{sys_m['temp_c']}°C" if sys_m.get("temp_c") is not None else "-"
        meta_lines = [
            (f"Total: {sys_m.get('cpu_pct', 0.0):.1f}%  Temp:{temp_txt}", colors.get(_pct_level(float(sys_m.get("cpu_pct", 0.0))), txt) | curses.A_BOLD),
            (f"Load: {sys_m.get('load', '?')}", muted),
            (f"IP: {sys_m.get('ip', '?')} ({sys_m.get('iface', '?')})", colors.get("INIT", txt)),
            (f"UP: {sys_m.get('uptime_str', '?')}", muted),
        ]
        for off, (mtxt, mattr) in enumerate(meta_lines):
            if meta_y + off < iy + ih:
                stdscr.addnstr(meta_y + off, lx, mtxt[:rem_lw], rem_lw, mattr)

    # 2) Middle Row: Memory & Swap History + Circular Donut Gauges
    mid_y = 1 + cpu_h
    mem_chart_w = max(24, (scr_w * 60) // 100)
    donuts_w = scr_w - mem_chart_w
    miy, mix, mih, miw = _draw_box(stdscr, mid_y, 0, mid_h, mem_chart_w, "Memory & Swap History", muted, title_attr)
    mem_series = [
        hist.get("mem_pct") or [float(sys_m.get("ram_pct", 0.0))],
        hist.get("swap_pct") or [float(sys_m.get("swap_pct", 0.0))],
    ]
    mem_palette = [colors.get("OK", curses.A_BOLD), colors.get("INIT", curses.A_BOLD)]
    chart_rows = max(1, mih - 1)
    if mih >= 2 and miw > y_axis_w + 4:
        for yr, lbl in ((0, "100%"), (chart_rows - 1, "  0%")):
            if yr < chart_rows:
                stdscr.addnstr(miy + yr, mix, lbl, y_axis_w, muted)
        mgrid = _render_braille_chart(mem_series, miw - y_axis_w - 1, chart_rows, max_val=100.0)
        _draw_braille_widget(stdscr, miy, mix + y_axis_w, chart_rows, miw - y_axis_w - 1, mgrid, mem_palette)
        leg_y = miy + chart_rows
        if leg_y < miy + mih:
            _draw_segments(
                stdscr,
                leg_y,
                w,
                [
                    ("● RAM ", mem_palette[0]),
                    (f"{sys_m.get('ram_pct', 0.0):.1f}% ({_human_bytes(sys_m.get('ram_used_bytes', 0))})  ", txt),
                    ("● Swap ", mem_palette[1]),
                    (f"{sys_m.get('swap_pct', 0.0):.1f}% ({_human_bytes(sys_m.get('swap_used_bytes', 0))})", muted),
                ],
                x_start=mix + 1,
                x_limit=mix + miw,
            )

    if donuts_w >= 36:
        d1_w = donuts_w // 2
        d2_w = donuts_w - d1_w
        _draw_donut_card(
            stdscr,
            mid_y,
            mem_chart_w,
            mid_h,
            d1_w,
            "Memory",
            float(sys_m.get("ram_pct", 0.0)),
            _human_bytes(float(sys_m.get("ram_used_bytes", 0))),
            f"/ {_human_bytes(float(sys_m.get('ram_total_bytes', 0)))}",
            colors,
            colors.get(_pct_level(float(sys_m.get("ram_pct", 0.0))), curses.A_BOLD),
        )
        swap_total_b = float(sys_m.get("swap_total_bytes", 0))
        _draw_donut_card(
            stdscr,
            mid_y,
            mem_chart_w + d1_w,
            mid_h,
            d2_w,
            "Swap",
            float(sys_m.get("swap_pct", 0.0)),
            _human_bytes(float(sys_m.get("swap_used_bytes", 0))) if swap_total_b > 0 else "None",
            f"/ {_human_bytes(swap_total_b)}" if swap_total_b > 0 else "Swap off",
            colors,
            colors.get("INIT", curses.A_BOLD),
        )
    elif donuts_w >= 18:
        _draw_donut_card(
            stdscr,
            mid_y,
            mem_chart_w,
            mid_h,
            donuts_w,
            "Memory",
            float(sys_m.get("ram_pct", 0.0)),
            _human_bytes(float(sys_m.get("ram_used_bytes", 0))),
            f"/ {_human_bytes(float(sys_m.get('ram_total_bytes', 0)))}",
            colors,
            colors.get(_pct_level(float(sys_m.get("ram_pct", 0.0))), curses.A_BOLD),
        )

    # 3) Bottom Row: Left = Network History + Disk Usage; Right = Sortable Processes Table
    bot_y = mid_y + mid_h
    left_w = max(24, (scr_w * 46) // 100)
    right_w = scr_w - left_w
    net_h = max(4, bot_h // 2)
    disk_h = max(4, bot_h - net_h)

    net = sys_m.get("net") or {}
    net_title = f"Network History ({net.get('iface', '?')})"
    niy, nix, nih, niw = _draw_box(stdscr, bot_y, 0, net_h, left_w, net_title, muted, title_attr)
    if nih >= 2 and niw >= 12:
        rx_hist = hist.get("net_rx_sec") or [float(net.get("rx_sec", 0.0))]
        tx_hist = hist.get("net_tx_sec") or [float(net.get("tx_sec", 0.0))]
        peak_net = max(1024.0, max(rx_hist, default=0.0), max(tx_hist, default=0.0))
        sp_w = max(8, niw - 28) if niw >= 38 else max(4, niw - 18)
        rx_line = [
            (f"↓ Down: {_human_bytes(net.get('rx_sec', 0.0))}/s ", colors.get("OK", txt) | curses.A_BOLD),
            (_render_sparkline(rx_hist, sp_w, peak_net), colors.get("OK", txt)),
        ]
        _draw_segments(stdscr, niy, w, rx_line, x_start=nix + 1, x_limit=nix + niw)
        tx_line = [
            (f"↑ Up:   {_human_bytes(net.get('tx_sec', 0.0))}/s ", colors.get("WARN", txt) | curses.A_BOLD),
            (_render_sparkline(tx_hist, sp_w, peak_net), colors.get("WARN", txt)),
        ]
        if nih >= 2:
            _draw_segments(stdscr, niy + 1, w, tx_line, x_start=nix + 1, x_limit=nix + niw)
        if nih >= 3:
            tot_s = f"Total: ↓{_human_bytes(net.get('rx_bytes', 0))}  ↑{_human_bytes(net.get('tx_bytes', 0))}"
            stdscr.addnstr(niy + 2, nix + 1, tot_s[: max(1, niw - 2)], max(1, niw - 2), muted)

    diy, dix, dih, diw = _draw_box(stdscr, bot_y + net_h, 0, disk_h, left_w, "Disk Usage", muted, title_attr)
    disks = sys_m.get("disks") or []
    show_disk_donut = diw >= 46 and dih >= 4 and len(disks) > 0
    dx_off = dix + 1
    if show_disk_donut:
        primary_d = disks[0]
        p_pct = float(primary_d.get("pct", 0.0))
        d_donut = _render_braille_donut(p_pct, width=8, height=min(4, dih))
        _draw_braille_widget(stdscr, diy, dx_off, min(4, dih), 8, d_donut, [muted, colors.get(_pct_level(p_pct), txt)])
        dx_off += 10
    rem_dw = max(8, (dix + diw) - dx_off)
    bar_len = 8 if rem_dw >= 38 else (5 if rem_dw >= 26 else 0)
    for idx, d in enumerate(disks[: max(0, dih)]):
        pct = float(d.get("pct", 0.0))
        dattr = colors.get(_pct_level(pct), txt) | curses.A_BOLD
        bar_part = f" {_pct_bar(pct, bar_len)}" if bar_len else ""
        segs = [
            (f"{d['label']:<10} ", txt),
            (f"{pct:>3.0f}%{bar_part} ", dattr),
            (f"{d['used']}/{d['size']}", muted),
        ]
        _draw_segments(stdscr, diy + idx, w, segs, x_start=dx_off, x_limit=dix + diw)

    # Right Bottom Box: Interactive Sortable Process Table
    procs = _sort_processes(sys_m.get("processes") or [], sort_key=proc_sort, reverse=proc_reverse)
    proc_selected = max(0, min(proc_selected, max(0, len(procs) - 1)))
    piy, pix, pih, piw = _draw_box(
        stdscr,
        bot_y,
        left_w,
        bot_h,
        right_w,
        "Processes [p:PID  c:%CPU  m:%MEM]",
        muted,
        title_attr,
    )
    if pih >= 2 and piw >= 24:
        arrow = "▲" if proc_reverse else "▼"
        h_pid = f"PID{arrow if proc_sort == 'pid' else ' '}"
        h_cpu = f"%CPU{arrow if proc_sort == 'cpu' else ' '}"
        h_mem = f"%MEM{arrow if proc_sort == 'mem' else ' '}"
        cmd_w = max(8, piw - 32)
        header_txt = f" {h_pid:<6} {'COMMAND':<{cmd_w}} {h_cpu:>6} {h_mem:>6} {'RSS':>9}"
        stdscr.addnstr(piy, pix, header_txt[:piw].ljust(piw), piw, hdr)

        vis_p = max(1, pih - 1)
        scroll_p = max(0, proc_selected - vis_p + 1)
        for row_i in range(vis_p):
            p_idx = scroll_p + row_i
            if p_idx >= len(procs):
                break
            pr = procs[p_idx]
            py = piy + 1 + row_i
            is_psel = p_idx == proc_selected
            row_attr = colors.get("row_sel", curses.A_REVERSE) if is_psel else txt
            mark = "▸" if is_psel else " "
            rss_str = _human_bytes(float(pr.get("rss_kb", 0)) * 1024.0)
            line_txt = f"{mark}{pr['pid']:<6} {pr['command'][:cmd_w]:<{cmd_w}} {pr['cpu_pct']:>5.1f}% {pr['mem_pct']:>5.1f}% {rss_str:>9}"
            stdscr.addnstr(py, pix, line_txt[:piw].ljust(piw), piw, row_attr)

    return proc_selected


def _draw_tui(
    stdscr: Any,
    engine: MonitorEngine,
    colors: Dict[str, int],
    selected: int,
    filter_target: Optional[str],
    status_flash: str,
    view_mode: str = "overview",
    proc_selected: int = 0,
    proc_sort: str = "cpu",
    proc_reverse: bool = False,
) -> Tuple[int, int]:
    rows, events = engine.snapshot()
    h, w = stdscr.getmaxyx()
    stdscr.bkgd(" ", colors.get("canvas", 0))
    stdscr.erase()
    if h < 6 or w < 24:
        if h >= 1 and w >= 2:
            stdscr.addnstr(0, 0, "Window too small", w - 1)
        stdscr.refresh()
        return selected, proc_selected

    selected = max(0, min(selected, max(0, len(rows) - 1)))
    ok_cnt = sum(1 for r in rows if r["status"] == "OK")
    pause_tag = "PAUSED" if engine.paused else "LIVE"
    filt_tag = f"  FILTER:{filter_target}" if (filter_target and view_mode != "dash") else ""

    # Top MPC 3.x Menu header bar with view tabs [1:OVERVIEW] [2:DASH] [3:STREAM]
    badge = " MPC LIVE "
    stdscr.addnstr(0, 0, badge, w - 1, colors.get("badge_red", curses.A_REVERSE))
    meter = f" TARGETS {ok_cnt}/{len(rows)} | {_now_iso()} "
    meter_x = max(len(badge) + 4, w - len(meter) - 1)
    if len(badge) < w - 1:
        stdscr.addnstr(0, len(badge), " " * max(1, w - len(badge) - 1), w - len(badge) - 1, colors.get("topbar", curses.A_REVERSE))
    tabs = [("1:OVERVIEW", "overview"), ("2:DASH", "dash"), ("3:STREAM", "stream")]
    tx = len(badge) + 1
    for t_lbl, t_mode in tabs:
        if tx + len(t_lbl) + 2 >= meter_x:
            break
        t_attr = colors.get("key_gold", curses.A_BOLD) if view_mode == t_mode else colors.get("topbar", curses.A_BOLD)
        stdscr.addnstr(0, tx, f"[{t_lbl}]", w - tx - 1, t_attr)
        tx += len(t_lbl) + 3
    if tx < meter_x:
        stdscr.addnstr(0, tx, f"| {pause_tag}{filt_tag}", max(1, meter_x - tx), colors.get("topbar", curses.A_REVERSE))
    if w - len(meter) - 1 > len(badge) + 4:
        stdscr.addnstr(0, w - len(meter) - 1, meter, len(meter), colors.get("topbar_ok", curses.A_REVERSE))

    sys_m = _find_system_metrics(rows)
    if view_mode == "dash":
        proc_selected = _draw_tui_dash(stdscr, colors, sys_m, h, w, proc_selected, proc_sort, proc_reverse)
    else:
        budget = 0 if view_mode == "stream" else (3 if h >= 16 else 2)
        sys_rows = _draw_tui_sysinfo(stdscr, colors, sys_m, w, 1, rows_budget=budget) if (sys_m and h >= 12 and budget > 0) else 0

        # Pane 1: Targets table (Sidebar dark bar header)
        table_hdr_y = 1 + sys_rows
        table_start_y = table_hdr_y + 1
        max_targets_pane = max(5, min(len(rows) + 3, max(6, (h - sys_rows) // 2)))
        cols_hdr = f"  {'TARGET ID':<18} {'KIND':<10} {'STATE':<5} {'MS':>5}  {'LIVE SUMMARY'}".ljust(max(1, w - 1))
        if table_hdr_y < h:
            stdscr.addnstr(table_hdr_y, 0, cols_hdr, w - 1, colors.get("sidebar_hdr", curses.A_BOLD))

        visible_rows = max(1, max_targets_pane - 2)
        scroll_top = max(0, selected - visible_rows + 1)
        for idx in range(visible_rows):
            r_idx = scroll_top + idx
            line_y = table_start_y + idx
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

        feed_hdr_y = table_start_y + min(visible_rows, len(rows)) if rows else table_start_y + 1
        if feed_hdr_y < h - 2:
            feed_title = f" LIVE EVENT STREAM ({len(events)} events) ".ljust(max(1, w - 1), "─")
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
        if view_mode == "dash":
            shortcuts = [
                ("[c]", "%CPU"),
                ("[m]", "%MEM"),
                ("[p]", "PID"),
                ("[↑/↓]", "Proc"),
                ("[Tab/g]", "View"),
                ("[Space]", "Pause"),
                ("[r]", "Refresh"),
                ("[q]", "Quit"),
            ]
        else:
            shortcuts = [
                ("[↑/↓]", "Select"),
                ("[Enter]", "Filter"),
                ("[a]", "Add"),
                ("[d]", "Delete"),
                ("[t]", "Toggle"),
                ("[g/Tab]", "Dash"),
                ("[Space]", "Pause"),
                ("[c]", "Clear"),
                ("[r]", "Refresh"),
                ("[q]", "Quit"),
            ]
        fx = 1
        for key_lbl, desc_lbl in shortcuts:
            if fx + len(key_lbl) + len(desc_lbl) + 3 >= w:
                break
            stdscr.addnstr(h - 2, fx, key_lbl, w - fx - 1, colors.get("key_gold", curses.A_BOLD))
            fx += len(key_lbl) + 1
            stdscr.addnstr(h - 2, fx, desc_lbl, w - fx - 1, colors.get("footer_tile", 0))
            fx += len(desc_lbl) + 2

        flash_txt = _safe_str(status_flash or f"Targets file: {engine.targets_path}", w - 1)
        stdscr.addnstr(h - 1, 0, flash_txt, w - 1, colors.get("muted", 0))
    stdscr.refresh()
    return selected, proc_selected


def run_curses_tui(engine: MonitorEngine, initial_view: str = "overview") -> int:
    locale.setlocale(locale.LC_ALL, "")
    valid_views = ("overview", "dash", "stream")
    norm_view = "dash" if initial_view in ("dash", "gtop") else initial_view
    start_view = norm_view if norm_view in valid_views else "overview"

    def _tui_main(stdscr: Any) -> int:
        curses.curs_set(0)
        stdscr.keypad(True)
        stdscr.timeout(250)
        colors = _init_curses_colors()
        engine.start()

        selected = 0
        proc_selected = 0
        proc_sort = "cpu"
        proc_reverse = False
        view_mode = start_view
        filter_target: Optional[str] = None
        flash = ""

        def _toggle_sort(col: str) -> None:
            nonlocal proc_sort, proc_reverse, proc_selected, flash
            if proc_sort == col:
                proc_reverse = not proc_reverse
            else:
                proc_sort = col
                proc_reverse = False
            proc_selected = 0
            flash = f"Dash sort: {col.upper()} ({'asc ▲' if proc_reverse else 'desc ▼'})"

        try:
            while True:
                selected, proc_selected = _draw_tui(
                    stdscr,
                    engine,
                    colors,
                    selected,
                    filter_target,
                    flash,
                    view_mode=view_mode,
                    proc_selected=proc_selected,
                    proc_sort=proc_sort,
                    proc_reverse=proc_reverse,
                )
                key = stdscr.getch()
                if key == -1:
                    continue
                rows, _ = engine.snapshot()
                sys_m = _find_system_metrics(rows)
                proc_count = len((sys_m or {}).get("processes") or [])

                if key in (ord("q"), ord("Q")):
                    break
                elif key in (ord("1"),):
                    view_mode = "overview"
                    flash = "View: OVERVIEW"
                elif key in (ord("2"),):
                    view_mode = "dash"
                    flash = "View: DASH (System Dashboard)"
                elif key in (ord("3"),):
                    view_mode = "stream"
                    flash = "View: STREAM (Full Feed)"
                elif key in (ord("\t"), ord("v"), ord("V")):
                    idx = (valid_views.index(view_mode) + 1) % len(valid_views)
                    view_mode = valid_views[idx]
                    flash = f"View: {view_mode.upper()}"
                elif key in (ord("g"), ord("G")):
                    view_mode = "overview" if view_mode == "dash" else "dash"
                    flash = f"View: {'DASH (System Dashboard)' if view_mode == 'dash' else 'OVERVIEW'}"
                elif key in (curses.KEY_UP, ord("k")):
                    if view_mode == "dash":
                        proc_selected = max(0, proc_selected - 1)
                    else:
                        selected = max(0, selected - 1)
                elif key in (curses.KEY_DOWN, ord("j")):
                    if view_mode == "dash":
                        proc_selected = min(max(0, proc_count - 1), proc_selected + 1)
                    else:
                        selected = min(max(0, len(rows) - 1), selected + 1)
                elif key == curses.KEY_PPAGE and view_mode == "dash":
                    proc_selected = max(0, proc_selected - 5)
                elif key == curses.KEY_NPAGE and view_mode == "dash":
                    proc_selected = min(max(0, proc_count - 1), proc_selected + 5)
                elif key == ord(" "):
                    engine.paused = not engine.paused
                    flash = "Stream paused." if engine.paused else "Stream resumed."
                elif key in (ord("r"), ord("R")):
                    engine.reload_targets()
                    engine.force_refresh()
                    flash = "All targets refreshed."
                elif view_mode == "dash" and key in (ord("p"), ord("P")):
                    _toggle_sort("pid")
                elif view_mode == "dash" and key in (ord("c"), ord("C")):
                    _toggle_sort("cpu")
                elif view_mode == "dash" and key in (ord("m"), ord("M")):
                    _toggle_sort("mem")
                elif key in (10, 13, curses.KEY_ENTER) and view_mode != "dash":
                    if rows:
                        tid = rows[selected]["id"]
                        filter_target = None if filter_target == tid else tid
                        flash = f"Filter: {filter_target or 'ALL'}"
                elif key in (ord("f"), ord("F")) and view_mode != "dash":
                    filter_target = "MIDI" if filter_target != "MIDI" else None
                    flash = f"Filter: {filter_target or 'ALL'}"
                elif key in (ord("p"), ord("P")) and view_mode != "dash":
                    engine.paused = not engine.paused
                    flash = "Stream paused." if engine.paused else "Stream resumed."
                elif key in (ord("c"), ord("C")) and view_mode != "dash":
                    engine.clear_events()
                    flash = "Feed cleared."
                elif key in (ord("t"), ord("T")) and rows and view_mode != "dash":
                    tid = rows[selected]["id"]
                    set_target_enabled(tid, None, engine.targets_path)
                    engine.reload_targets()
                    flash = f"Toggled: {tid}"
                elif key in (ord("d"), ord("D")) and rows and view_mode != "dash":
                    tid = rows[selected]["id"]
                    ans = _prompt_line(stdscr, f"Delete '{tid}'? [y/n]: ", colors)
                    if ans and ans.lower() in ("y", "yes", "e", "evet"):
                        remove_target(tid, engine.targets_path)
                        engine.reload_targets()
                        selected = max(0, selected - 1)
                        flash = f"Deleted: {tid}"
                    else:
                        flash = "Delete cancelled."
                elif key in (ord("a"), ord("A")) and view_mode != "dash":
                    try:
                        flash = _interactive_add_wizard(stdscr, engine, colors)
                    except Exception as exc:
                        flash = f"Error: {exc}"
        finally:
            engine.stop()
        return 0

    return curses.wrapper(_tui_main)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def run_monitor_cli(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Live Mac Terminal Monitor (MPC + Dash + Antigravity)")
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS_PATH, help="Targets config JSON path")
    parser.add_argument("--once", action="store_true", help="Probe all targets once and print summary + recent events")
    parser.add_argument("--json", action="store_true", help="Print --once or --list output as JSON")
    parser.add_argument("--stream", action="store_true", help="Plain terminal stream mode instead of TUI (Ctrl+C to exit)")
    parser.add_argument("--dash", action="store_true", help="Launch TUI directly in full Dash hardware view")
    parser.add_argument("--gtop", dest="dash", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--interval", type=float, default=0.6, help="Stream refresh interval (sec)")
    parser.add_argument("--list", action="store_true", help="List all configured monitor targets")
    parser.add_argument("--add", action="store_true", help="Add a new monitor target")
    parser.add_argument("--id", dest="target_id", default=None, help="Target ID to add")
    parser.add_argument("--name", default=None, help="Display name")
    parser.add_argument("--kind", choices=sorted(PROBE_HANDLERS.keys()), default=None, help="Target kind")
    parser.add_argument("--spec", default=None, help="URL, host:port, user@host::cmd, file/folder path, or shell command")
    parser.add_argument("--parser", choices=sorted(LOG_PARSERS.keys()), default="plain", help="log_tail line parser")
    parser.add_argument("--target-interval", type=float, default=2.0, help="New target probe interval (sec)")
    parser.add_argument("--remove", metavar="ID", default=None, help="Remove target by ID")
    parser.add_argument("--enable", metavar="ID", default=None, help="Enable target")
    parser.add_argument("--disable", metavar="ID", default=None, help="Disable target")
    parser.add_argument("--emit", nargs=3, metavar=("SOURCE", "ACTION", "DETAIL"), default=None, help="Emit a test/info event")
    args = parser.parse_args(argv)

    tpath = args.targets
    if args.emit:
        emit_activity(args.emit[0], args.emit[1], args.emit[2])
        print(f"Event logged -> {ACTIVITY_LOG_PATH}")
        return 0

    if args.add:
        if not (args.target_id and args.kind and args.spec):
            parser.error("--add requires --id, --kind, and --spec")
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

    return run_curses_tui(engine, initial_view="dash" if args.dash else "overview")


if __name__ == "__main__":
    raise SystemExit(run_monitor_cli())
