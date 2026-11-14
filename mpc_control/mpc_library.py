#!/usr/bin/env python3
"""Host-side coordinator, Camelot/BPM search engine, and watcher for MPC Sample Library."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from mpc_audio import (
    _probe_audio_meta,
    analyze_audio,
    detect_key,
    parse_key_label,
)
from mpc_client import emit_activity

DEFAULT_MPC_HOST = os.environ.get("MPC_HOST", "10.101.132.233")
DEFAULT_MPC_USER = os.environ.get("MPC_SSH_USER", "root")
REMOTE_LIBRARY_DIR = "/data/mpc-control/sample_library"
REMOTE_MIRROR_DIR = "/media/okcomputer/MPC_Sample_Library"
LOCAL_LIBRARY_DIR = HERE / "sample_library"

_HIT_KW_RE = re.compile(
    r"(?:^|[^a-z0-9])(kick|kik|bd|snare|snr|sd|clap|clp|hat|hh|oh|ch|perc|tom|"
    r"rim|cym|crash|ride|shaker|shk|808|stab|hit|shot|one[\s_-]?shot|vox|fx|"
    r"riser|impact|bell|pluck|keys|lead)(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)
_PITCH_TAG_RE = re.compile(r"[-_ ][A-G][b#]?[0-8](?:[^A-Za-z0-9]|$)")
_CAMELOT_RE = re.compile(r"^([1-9]|1[0-2])([AB])$", re.IGNORECASE)


def _ssh_target(host: str, user: str) -> str:
    return host if "@" in host else f"{user}@{host}"


def _ssh_run(
    host: str,
    user: str,
    command: str,
    timeout_s: float = 15.0,
    binary: bool = False,
) -> bytes | str:
    target = _ssh_target(host, user)
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", target, command],
        capture_output=True,
        text=not binary,
        timeout=timeout_s,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace") if binary else str(proc.stderr)
        raise RuntimeError(f"SSH failed ({proc.returncode}): {err.strip()}")
    return proc.stdout


def classify_sample_category(
    rel_path: str,
    duration_s: float,
    oneshot_max_s: float = 3.0,
) -> str:
    """Match the MPC C indexer's classification rules cleanly."""
    if duration_s <= 0.0 or duration_s < 0.22:
        return "ultra_short"
    name = os.path.basename(rel_path)
    if _HIT_KW_RE.search(rel_path) or _PITCH_TAG_RE.search(name):
        if duration_s <= 8.5:
            return "one_shot"
    if duration_s <= oneshot_max_s:
        return "one_shot"
    if duration_s <= 32.0:
        return "loop"
    if duration_s <= 120.0:
        return "phrase"
    return "track"


def camelot_compatible(key_or_camelot: str) -> List[str]:
    """Return Camelot wheel codes harmonically compatible with a key name or code."""
    raw = key_or_camelot.strip()
    m = _CAMELOT_RE.match(raw)
    if m:
        num = int(m.group(1))
        letter = m.group(2).upper()
    else:
        meta = parse_key_label(raw)
        cam = str(meta["camelot"])
        cm = _CAMELOT_RE.match(cam)
        if not cm:
            return []
        num = int(cm.group(1))
        letter = cm.group(2).upper()

    prev_n = 12 if num == 1 else num - 1
    next_n = 1 if num == 12 else num + 1
    rel_letter = "B" if letter == "A" else "A"
    codes = [
        f"{num}{letter}",
        f"{num}{rel_letter}",
        f"{prev_n}{letter}",
        f"{next_n}{letter}",
    ]
    return list(dict.fromkeys(codes))


def fetch_status(
    host: str = DEFAULT_MPC_HOST,
    user: str = DEFAULT_MPC_USER,
    local_dir: Path = LOCAL_LIBRARY_DIR,
) -> Dict[str, Any]:
    """Read live progress, summary stats, and systemd daemon state from MPC."""
    remote_cmd = (
        f"echo '===PROG==='; cat {shlex.quote(REMOTE_LIBRARY_DIR + '/progress.json')} 2>/dev/null || echo '{{}}'; "
        f"echo '===STATS==='; cat {shlex.quote(REMOTE_LIBRARY_DIR + '/stats.json')} 2>/dev/null || echo '{{}}'; "
        "echo '===SVC==='; systemctl is-active mpc-sample-library.service 2>/dev/null || echo unknown; "
        f"echo '===COUNT==='; wc -l < {shlex.quote(REMOTE_LIBRARY_DIR + '/library.jsonl')} 2>/dev/null || echo 0; "
        f"echo '===MIRROR==='; ls -1 {shlex.quote(REMOTE_MIRROR_DIR)} 2>/dev/null | tr '\\n' ' '"
    )
    out: Dict[str, Any] = {
        "mpc_host": host,
        "remote_dir": REMOTE_LIBRARY_DIR,
        "remote_mirror_dir": REMOTE_MIRROR_DIR,
        "local_dir": str(local_dir),
    }
    try:
        raw = str(_ssh_run(host, user, remote_cmd, timeout_s=10.0))
        sections: Dict[str, str] = {}
        cur = None
        buf: List[str] = []
        for line in raw.splitlines():
            if line.startswith("===") and line.endswith("==="):
                if cur is not None:
                    sections[cur] = "\n".join(buf).strip()
                cur = line.strip("=")
                buf = []
            else:
                buf.append(line)
        if cur is not None:
            sections[cur] = "\n".join(buf).strip()

        out["reachable"] = True
        out["service_active"] = sections.get("SVC", "unknown").strip()
        out["remote_jsonl_lines"] = int(sections.get("COUNT", "0") or 0)
        out["mirror_files"] = [f for f in sections.get("MIRROR", "").split() if f]
        try:
            out["progress"] = json.loads(sections.get("PROG") or "{}")
        except json.JSONDecodeError:
            out["progress"] = {}
        try:
            out["stats"] = json.loads(sections.get("STATS") or "{}")
        except json.JSONDecodeError:
            out["stats"] = {}
    except Exception as exc:
        out["reachable"] = False
        out["error"] = str(exc)

    local_jsonl = local_dir / "library.jsonl"
    out["local_cache_exists"] = local_jsonl.is_file()
    if local_jsonl.is_file():
        st = local_jsonl.stat()
        out["local_cache_bytes"] = st.st_size
        out["local_cache_mtime"] = int(st.st_mtime)
    return out


def sync_from_mpc(
    host: str = DEFAULT_MPC_HOST,
    user: str = DEFAULT_MPC_USER,
    out_dir: Path = LOCAL_LIBRARY_DIR,
    include_jsonl: bool = True,
) -> Dict[str, Any]:
    """Stream-compress library artifacts from MPC to Mac via SSH + gzip."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files = ["stats.json", "progress.json", "host_resolved.jsonl"]
    if include_jsonl:
        files.extend(["library.jsonl", "library.csv"])

    synced: Dict[str, int] = {}
    missing: List[str] = []
    for fname in files:
        rpath = f"{REMOTE_LIBRARY_DIR}/{fname}"
        cmd = f"if [ -f {shlex.quote(rpath)} ]; then gzip -c {shlex.quote(rpath)}; fi"
        blob = _ssh_run(host, user, cmd, timeout_s=60.0, binary=True)
        assert isinstance(blob, (bytes, bytearray))
        if not blob:
            missing.append(fname)
            continue
        data = gzip.decompress(blob)
        dest = out_dir / fname
        tmp = out_dir / f".{fname}.tmp"
        tmp.write_bytes(data)
        tmp.replace(dest)
        synced[fname] = len(data)

    emit_activity(
        "library",
        "sync",
        f"Synced {len(synced)} files from {host} ({sum(synced.values()) // 1024} KB)",
        target=f"mpc:{host}",
    )
    return {"ok": True, "host": host, "out_dir": str(out_dir), "synced_bytes": synced, "missing": missing}


def load_entries(
    jsonl_path: Optional[Path] = None,
    host_resolved_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Load sample entries from JSONL, merging host_resolved overrides and deduplicating by full_path."""
    base_dir = LOCAL_LIBRARY_DIR
    primary = Path(jsonl_path) if jsonl_path else (base_dir / "library.jsonl")
    sidecar = Path(host_resolved_path) if host_resolved_path else (base_dir / "host_resolved.jsonl")

    by_path: Dict[str, Dict[str, Any]] = {}
    for src in (primary, sidecar):
        if not src.is_file():
            continue
        with src.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                fp = rec.get("full_path")
                if isinstance(fp, str) and fp:
                    by_path[fp] = rec
    return list(by_path.values())


def _bpm_matches(
    sample_bpm: Optional[float],
    target_bpm: float,
    tol: float,
    half_double: bool,
) -> bool:
    if sample_bpm is None or sample_bpm <= 0:
        return False
    if abs(sample_bpm - target_bpm) <= tol:
        return True
    if half_double:
        if abs((sample_bpm * 2.0) - target_bpm) <= tol:
            return True
        if abs((sample_bpm * 0.5) - target_bpm) <= tol:
            return True
    return False


def search_library(
    entries: Optional[Iterable[Dict[str, Any]]] = None,
    *,
    query: Optional[str] = None,
    bpm: Optional[float] = None,
    bpm_tol: float = 3.0,
    half_double: bool = True,
    key: Optional[str] = None,
    harmonic: bool = True,
    category: Optional[str] = None,
    drive: Optional[str] = None,
    status: Optional[str] = None,
    min_duration: Optional[float] = None,
    max_duration: Optional[float] = None,
    limit: int = 50,
    jsonl_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Query indexed MPC samples by text, BPM (including half/double), Camelot harmonic key, drive, or category."""
    rows = list(entries) if entries is not None else load_entries(jsonl_path=jsonl_path)

    q_lower = query.strip().lower() if query and query.strip() else None
    allowed_camelot: Optional[set[str]] = None
    primary_cam: Optional[str] = None
    exact_short: Optional[str] = None
    if key and key.strip():
        compat = camelot_compatible(key)
        if compat:
            primary_cam = compat[0].upper()
        if harmonic and compat:
            allowed_camelot = {c.upper() for c in compat}
        elif _CAMELOT_RE.match(key.strip()):
            allowed_camelot = {key.strip().upper()}
            primary_cam = key.strip().upper()
        else:
            meta = parse_key_label(key)
            exact_short = str(meta["short"]).lower()
            primary_cam = str(meta["camelot"]).upper()
            allowed_camelot = {primary_cam}

    matched: List[Dict[str, Any]] = []
    for rec in rows:
        if drive and str(rec.get("drive", "")).lower() != drive.strip().lower():
            continue
        if category and str(rec.get("category", "")).lower() != category.strip().lower():
            continue
        if status and str(rec.get("status", "")).lower() != status.strip().lower():
            continue

        dur = rec.get("duration_s")
        if min_duration is not None and (dur is None or float(dur) < min_duration):
            continue
        if max_duration is not None and (dur is None or float(dur) > max_duration):
            continue

        if q_lower:
            hay = " ".join(
                str(rec.get(k) or "")
                for k in ("rel_path", "drive", "key", "short_key", "camelot", "category")
            ).lower()
            if not all(tok in hay for tok in q_lower.split()):
                continue

        if bpm is not None:
            sbpm = rec.get("bpm")
            if not _bpm_matches(float(sbpm) if sbpm is not None else None, float(bpm), float(bpm_tol), half_double):
                continue

        if allowed_camelot is not None:
            rec_cam = str(rec.get("camelot") or "").upper()
            rec_short = str(rec.get("short_key") or "").lower()
            if harmonic:
                if rec_cam not in allowed_camelot:
                    continue
            else:
                if exact_short and rec_short != exact_short and rec_cam not in allowed_camelot:
                    continue
                if not exact_short and rec_cam not in allowed_camelot:
                    continue

        matched.append(rec)

    def _score(rec: Dict[str, Any]) -> Tuple[int, float, float, str]:
        cam = str(rec.get("camelot") or "").upper()
        exact_cam = 1 if (primary_cam and cam == primary_cam) else 0
        bpm_prox = 0.0
        if bpm is not None and rec.get("bpm") is not None:
            sb = float(rec["bpm"])
            diffs = [abs(sb - bpm), abs(sb * 2.0 - bpm), abs(sb * 0.5 - bpm)]
            bpm_prox = -min(diffs)
        conf = float(rec.get("bpm_conf") or 0.0) + float(rec.get("key_conf") or 0.0)
        return (exact_cam, round(bpm_prox, 3), round(conf, 3), str(rec.get("rel_path") or ""))

    matched.sort(key=_score, reverse=True)
    capped = matched[: max(1, int(limit))]
    return {
        "total_indexed": len(rows),
        "matched": len(matched),
        "returned": len(capped),
        "filters": {
            "query": query,
            "bpm": bpm,
            "bpm_tol": bpm_tol,
            "half_double": half_double,
            "key": key,
            "harmonic": harmonic,
            "compatible_camelot": sorted(allowed_camelot) if allowed_camelot else None,
            "category": category,
            "drive": drive,
        },
        "results": capped,
    }


def _analyze_compressed_local(
    local_file: Path,
    meta_stub: Dict[str, Any],
    oneshot_max_s: float = 3.0,
) -> Dict[str, Any]:
    """Decode and analyze a compressed sample (.flac/.ogg/.m4a) locally, skipping BPM on one-shots."""
    rel_path = str(meta_stub.get("rel_path") or local_file.name)
    probe = _probe_audio_meta(local_file)
    dur = float(probe.get("duration_s") or 0.0)
    category = classify_sample_category(rel_path, dur, oneshot_max_s=oneshot_max_s)

    rec = dict(meta_stub)
    rec["category"] = category
    rec["duration_s"] = round(dur, 4)
    rec["sr"] = int(probe.get("sample_rate") or 0)
    rec["ch"] = int(probe.get("channels") or 0)
    rec["bits"] = int(probe.get("bits_per_sample") or 16)
    rec["analyzer"] = "mac_host_audio"

    if category == "ultra_short" or dur < 0.22:
        rec.update({
            "bpm": None,
            "bpm_conf": None,
            "key": None,
            "short_key": None,
            "camelot": None,
            "key_conf": 0.0,
            "atonal": None,
            "status": "ok",
        })
        return rec

    if category == "one_shot":
        kres = detect_key(local_file, backend="ensemble")
        kconf = float(kres.get("confidence") or 0.0)
        klabel = kres.get("key")
        atonal = kconf < 0.28 or not klabel or str(klabel).lower() == "silence"
        rec.update({
            "bpm": None,
            "bpm_conf": None,
            "key": None if atonal else klabel,
            "short_key": None if atonal else kres.get("short"),
            "camelot": None if atonal else kres.get("camelot"),
            "key_conf": round(kconf, 3),
            "atonal": atonal,
            "status": "ok",
        })
        return rec

    full = analyze_audio(local_file, beat_backend="auto", key_backend="ensemble")
    kconf = float(full.get("key_confidence") or 0.0)
    klabel = full.get("key")
    atonal = kconf < 0.28 or not klabel or str(klabel).lower() == "silence"
    bval = full.get("bpm")
    bconf = float(full.get("bpm_confidence") or full.get("beat_confidence") or 0.0)
    rec.update({
        "bpm": round(float(bval), 1) if bval else None,
        "bpm_conf": round(bconf, 3) if bval else None,
        "key": None if atonal else klabel,
        "short_key": None if atonal else full.get("short_key"),
        "camelot": None if atonal else full.get("camelot"),
        "key_conf": round(kconf, 3),
        "atonal": atonal,
        "status": "ok",
    })
    return rec


def _push_host_resolved(host: str, user: str, local_host_jsonl: Path) -> None:
    target = _ssh_target(host, user)
    remote_tmp = f"{REMOTE_LIBRARY_DIR}/host_resolved.jsonl.tmp"
    remote_dst = f"{REMOTE_LIBRARY_DIR}/host_resolved.jsonl"
    subprocess.run(
        ["scp", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", str(local_host_jsonl), f"{target}:{remote_tmp}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    _ssh_run(host, user, f"mv {shlex.quote(remote_tmp)} {shlex.quote(remote_dst)}", timeout_s=10.0)


def resolve_pending_host_decode(
    host: str = DEFAULT_MPC_HOST,
    user: str = DEFAULT_MPC_USER,
    out_dir: Path = LOCAL_LIBRARY_DIR,
    oneshot_max_s: float = 3.0,
    limit: int = 0,
    sync_first: bool = True,
) -> Dict[str, Any]:
    """Pull pending_host_decode (.flac/.ogg/.m4a) files from MPC, analyze on Mac, push host_resolved.jsonl."""
    if sync_first:
        sync_from_mpc(host=host, user=user, out_dir=out_dir, include_jsonl=True)

    host_jsonl = out_dir / "host_resolved.jsonl"
    existing_resolved: Dict[str, Dict[str, Any]] = {}
    if host_jsonl.is_file():
        for rec in load_entries(jsonl_path=host_jsonl, host_resolved_path=host_jsonl):
            fp = str(rec.get("full_path") or "")
            if fp:
                existing_resolved[fp] = rec

    all_entries = load_entries(jsonl_path=out_dir / "library.jsonl", host_resolved_path=host_jsonl)
    pending: List[Dict[str, Any]] = []
    for rec in all_entries:
        if rec.get("status") != "pending_host_decode":
            continue
        fp = str(rec.get("full_path") or "")
        prev = existing_resolved.get(fp)
        if prev and prev.get("status") == "ok" and prev.get("size") == rec.get("size") and prev.get("mtime") == rec.get("mtime"):
            continue
        pending.append(rec)

    if limit > 0:
        pending = pending[:limit]

    resolved_count = 0
    errors: List[Dict[str, str]] = []

    if pending:
        with tempfile.TemporaryDirectory(prefix="mpc_host_decode_") as td:
            td_path = Path(td)
            for idx, stub in enumerate(pending, start=1):
                rpath = str(stub["full_path"])
                local_tmp = td_path / f"{idx}_{os.path.basename(rpath)}"
                try:
                    raw_blob = _ssh_run(host, user, f"cat {shlex.quote(rpath)}", timeout_s=120.0, binary=True)
                    assert isinstance(raw_blob, (bytes, bytearray))
                    local_tmp.write_bytes(raw_blob)
                    res = _analyze_compressed_local(local_tmp, stub, oneshot_max_s=oneshot_max_s)
                    existing_resolved[rpath] = res
                    resolved_count += 1
                    emit_activity(
                        "library",
                        "resolve_host",
                        f"{os.path.basename(rpath)} -> {res['category']} bpm={res['bpm']} key={res['short_key']}",
                        target=f"mpc:{host}",
                    )
                except Exception as exc:
                    errors.append({"full_path": rpath, "error": str(exc)})
                finally:
                    if local_tmp.exists():
                        local_tmp.unlink()

        if resolved_count > 0:
            lines = [json.dumps(v, ensure_ascii=False, separators=(",", ":")) for v in existing_resolved.values()]
            tmp_local = out_dir / ".host_resolved.jsonl.tmp"
            tmp_local.write_text("\n".join(lines) + "\n", encoding="utf-8")
            tmp_local.replace(host_jsonl)
            _push_host_resolved(host, user, host_jsonl)

    return {
        "ok": len(errors) == 0,
        "pending_found": len(pending),
        "resolved": resolved_count,
        "total_host_resolved": len(existing_resolved),
        "errors": errors,
    }


def watch_library(
    host: str = DEFAULT_MPC_HOST,
    user: str = DEFAULT_MPC_USER,
    out_dir: Path = LOCAL_LIBRARY_DIR,
    interval_s: float = 8.0,
    max_ticks: Optional[int] = None,
) -> int:
    """Relay MPC events.jsonl to Mac activity feed and auto-resolve newly added compressed files."""
    out_dir.mkdir(parents=True, exist_ok=True)
    events_remote = f"{REMOTE_LIBRARY_DIR}/events.jsonl"
    last_inode: Optional[int] = None
    last_offset = 0
    tick = 0
    emit_activity("library", "watch_start", f"Watching {host}:{events_remote} every {interval_s}s", target=f"mpc:{host}")

    while True:
        tick += 1
        try:
            probe_cmd = (
                f"if [ -f {shlex.quote(events_remote)} ]; then "
                f"stat -c '%i %s' {shlex.quote(events_remote)}; "
                "else echo '0 0'; fi"
            )
            meta_line = str(_ssh_run(host, user, probe_cmd, timeout_s=6.0)).strip().split()
            inode = int(meta_line[0]) if len(meta_line) >= 2 else 0
            size = int(meta_line[1]) if len(meta_line) >= 2 else 0

            if inode > 0:
                if last_inode is None:
                    # Bind near EOF so we don't replay historical events on startup, only live additions.
                    last_inode = inode
                    last_offset = size
                elif inode != last_inode or size < last_offset:
                    last_inode = inode
                    last_offset = 0

                if size > last_offset:
                    start_byte = last_offset + 1
                    chunk = str(
                        _ssh_run(host, user, f"tail -c +{start_byte} {shlex.quote(events_remote)}", timeout_s=10.0)
                    )
                    last_offset = size
                    need_host_decode = False
                    for line in chunk.splitlines():
                        line = line.strip()
                        if not line.startswith("{"):
                            continue
                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        lvl = "warn" if ev.get("status") != "ok" else "ok"
                        detail = (
                            f"[{ev.get('drive')}] {ev.get('rel_path')} "
                            f"({ev.get('category')}, bpm={ev.get('bpm')}, key={ev.get('short_key')})"
                        )
                        emit_activity("mpc_library", "indexed", detail, target=f"mpc:{host}", level=lvl)
                        if ev.get("status") == "pending_host_decode":
                            need_host_decode = True
                    if need_host_decode:
                        resolve_pending_host_decode(host=host, user=user, out_dir=out_dir, sync_first=True)

            if tick == 1 or tick % 10 == 0:
                # Keep lightweight stats + progress fresh locally without re-downloading large CSV every tick.
                sync_from_mpc(host=host, user=user, out_dir=out_dir, include_jsonl=False)
                stats_file = out_dir / "stats.json"
                if stats_file.is_file():
                    try:
                        st_obj = json.loads(stats_file.read_text(encoding="utf-8"))
                        if int(st_obj.get("pending_host_decode") or 0) > 0:
                            resolve_pending_host_decode(host=host, user=user, out_dir=out_dir, limit=8, sync_first=True)
                    except (json.JSONDecodeError, OSError):
                        pass
        except Exception as exc:
            emit_activity("library", "watch_err", str(exc), target=f"mpc:{host}", level="warn")

        if max_ticks is not None and tick >= max_ticks:
            return 0
        time.sleep(max(2.0, float(interval_s)))


def run_library_cli(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="mpc_cli library", description="MPC Sample Library Status, Sync, Search & Watch")
    parser.add_argument("--host", default=DEFAULT_MPC_HOST)
    parser.add_argument("--user", default=DEFAULT_MPC_USER)
    sub = parser.add_subparsers(dest="subcmd", required=True)

    sub.add_parser("status", help="Show live MPC indexing progress and stats")

    p_sync = sub.add_parser("sync", help="Sync library files from MPC to Mac cache")
    p_sync.add_argument("--no-jsonl", action="store_true")

    p_search = sub.add_parser("search", help="Search local synced sample library")
    p_search.add_argument("query", nargs="?", default=None)
    p_search.add_argument("--bpm", type=float, default=None)
    p_search.add_argument("--tol", type=float, default=3.0)
    p_search.add_argument("--no-half-double", action="store_true")
    p_search.add_argument("--key", default=None)
    p_search.add_argument("--exact-key", action="store_true", help="Disable Camelot harmonic compatibility")
    p_search.add_argument("--category", default=None)
    p_search.add_argument("--drive", default=None)
    p_search.add_argument("--status", default=None)
    p_search.add_argument("--min-dur", type=float, default=None)
    p_search.add_argument("--max-dur", type=float, default=None)
    p_search.add_argument("--limit", type=int, default=30)

    p_res = sub.add_parser("resolve-compressed", help="Analyze pending .flac/.ogg/.m4a files on Mac and push back")
    p_res.add_argument("--limit", type=int, default=0)
    p_res.add_argument("--oneshot-max-s", type=float, default=3.0)
    p_res.add_argument("--no-sync", action="store_true")

    p_w = sub.add_parser("watch", help="Tail MPC events.jsonl into activity feed & auto-resolve new compressed files")
    p_w.add_argument("--interval", type=float, default=8.0)

    args = parser.parse_args(argv)
    if args.subcmd == "status":
        res = fetch_status(host=args.host, user=args.user)
    elif args.subcmd == "sync":
        res = sync_from_mpc(host=args.host, user=args.user, include_jsonl=not args.no_jsonl)
    elif args.subcmd == "search":
        res = search_library(
            query=args.query,
            bpm=args.bpm,
            bpm_tol=args.tol,
            half_double=not args.no_half_double,
            key=args.key,
            harmonic=not args.exact_key,
            category=args.category,
            drive=args.drive,
            status=args.status,
            min_duration=args.min_dur,
            max_duration=args.max_dur,
            limit=args.limit,
        )
    elif args.subcmd == "resolve-compressed":
        res = resolve_pending_host_decode(
            host=args.host,
            user=args.user,
            oneshot_max_s=args.oneshot_max_s,
            limit=args.limit,
            sync_first=not args.no_sync,
        )
    elif args.subcmd == "watch":
        return watch_library(host=args.host, user=args.user, interval_s=args.interval)
    else:
        return 2

    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_library_cli())
