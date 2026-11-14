#!/usr/bin/env python3
"""Pure-Python 960-PPQN Standard MIDI File (.mid) builder and MPC deployer."""

from __future__ import annotations

import json
import os
import subprocess
import struct
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from mpc_client import MpcMap, deploy_file_to_mpc, emit_activity


PPQN = 960


def _encode_vlq(value: int) -> bytes:
    v = max(0, int(value))
    out = [v & 0x7F]
    v >>= 7
    while v > 0:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.reverse()
    return bytes(out)


def _decode_vlq(buf: bytes, offset: int) -> Tuple[int, int]:
    val = 0
    while offset < len(buf):
        b = buf[offset]
        offset += 1
        val = (val << 7) | (b & 0x7F)
        if not (b & 0x80):
            return val, offset
    raise ValueError("Truncated VLQ in MIDI stream")


def normalize_pattern_events(spec: Dict[str, Any], mpc_map: Optional[MpcMap] = None) -> List[Dict[str, Any]]:
    """Convert either explicit 'events' or compact 'pads' step map into flat note dicts."""
    mpc_map = mpc_map or MpcMap()
    default_ch = int(spec.get("channel", mpc_map.data.get("pads", {}).get("channel", 9))) & 0x0F
    bars = max(1, int(spec.get("bars", 4)))
    steps_per_bar = max(1, int(spec.get("steps_per_bar", 16)))
    swing_pct = float(spec.get("swing_pct", 50.0))

    raw_items: List[Dict[str, Any]] = []
    if "events" in spec and isinstance(spec["events"], list):
        raw_items.extend(spec["events"])

    if "pads" in spec and isinstance(spec["pads"], dict):
        for pad_key, bar_map in spec["pads"].items():
            pad_num = int(pad_key)
            for bar_key, step_map in (bar_map or {}).items():
                bar_num = int(bar_key)
                if isinstance(step_map, dict):
                    for step_key, val in step_map.items():
                        item: Dict[str, Any] = {
                            "bar": bar_num,
                            "step": int(step_key),
                            "pad": pad_num,
                        }
                        if isinstance(val, dict):
                            item.update(val)
                        else:
                            item["velocity"] = int(val)
                        raw_items.append(item)
                elif isinstance(step_map, list):
                    for entry in step_map:
                        item = {"bar": bar_num, "pad": pad_num}
                        if isinstance(entry, dict):
                            item.update(entry)
                        else:
                            item["step"] = int(entry)
                        raw_items.append(item)

    step_ticks = (4 * PPQN) // steps_per_bar
    total_ticks = bars * 4 * PPQN
    swing_offset = round(((swing_pct - 50.0) / 50.0) * step_ticks) if steps_per_bar == 16 else 0

    normalized: List[Dict[str, Any]] = []
    for ev in raw_items:
        bar_idx = max(1, int(ev.get("bar", 1))) - 1
        step_num = max(1, int(ev.get("step", 1)))
        nudge = int(ev.get("nudge_ticks", 0))
        if "note" in ev:
            note = int(ev["note"]) & 0x7F
        else:
            pad_info = mpc_map.resolve_pad(int(ev["pad"]))
            note = int(pad_info["note"]) & 0x7F

        vel = max(1, min(127, int(ev.get("velocity", 110))))
        ch = int(ev.get("channel", default_ch)) & 0x0F

        swing_shift = swing_offset if (step_num % 2 == 0) else 0
        start_tick = bar_idx * (4 * PPQN) + (step_num - 1) * step_ticks + swing_shift + nudge
        start_tick = max(0, min(total_ticks - 20, start_tick))

        if "duration_ticks" in ev:
            dur = max(10, int(ev["duration_ticks"]))
        else:
            gate = float(ev.get("gate_steps", 0.85))
            dur = max(15, round(gate * step_ticks))
        end_tick = min(total_ticks - 1, start_tick + dur)
        if end_tick <= start_tick:
            end_tick = min(total_ticks, start_tick + 15)

        normalized.append({
            "tick": start_tick,
            "end_tick": end_tick,
            "channel": ch,
            "note": note,
            "velocity": vel,
        })

    return normalized


def build_smf_bytes(spec: Dict[str, Any], mpc_map: Optional[MpcMap] = None) -> Tuple[bytes, Dict[str, Any]]:
    """Build Standard MIDI File (Format 0, 960 PPQN) bytes and summary metadata."""
    mpc_map = mpc_map or MpcMap()
    bpm = max(20.0, min(300.0, float(spec.get("bpm", 90.0))))
    bars = max(1, int(spec.get("bars", 4)))
    total_ticks = bars * 4 * PPQN
    us_per_quarter = max(100000, min(3000000, round(60_000_000.0 / bpm)))

    notes = normalize_pattern_events(spec, mpc_map=mpc_map)
    timeline: List[Tuple[int, int, bytes]] = [
        (0, 0, b"\xFF\x51\x03" + us_per_quarter.to_bytes(3, "big")),
        (0, 0, b"\xFF\x58\x04\x04\x02\x18\x08"),  # 4/4, 24 clocks, 8 32nd/quarter
    ]
    for n in notes:
        ch = n["channel"]
        timeline.append((n["tick"], 2, bytes([0x90 | ch, n["note"], n["velocity"]])))
        timeline.append((n["end_tick"], 1, bytes([0x80 | ch, n["note"], 0x00])))

    timeline.sort(key=lambda row: (row[0], row[1]))

    track_buf = bytearray()
    prev_tick = 0
    for tick, _, msg in timeline:
        track_buf.extend(_encode_vlq(tick - prev_tick))
        track_buf.extend(msg)
        prev_tick = tick

    eot_delta = max(0, total_ticks - prev_tick)
    track_buf.extend(_encode_vlq(eot_delta))
    track_buf.extend(b"\xFF\x2F\x00")

    header = b"MThd" + struct.pack(">IHHH", 6, 0, 1, PPQN)
    track_chunk = b"MTrk" + struct.pack(">I", len(track_buf)) + bytes(track_buf)
    smf = header + track_chunk

    meta = {
        "name": spec.get("name", "pattern"),
        "bpm": bpm,
        "bars": bars,
        "swing_pct": float(spec.get("swing_pct", 50.0)),
        "ppqn": PPQN,
        "note_count": len(notes),
        "bytes": len(smf),
    }
    return smf, meta


def write_midi_pattern(spec: Dict[str, Any], out_path: Path, mpc_map: Optional[MpcMap] = None) -> Dict[str, Any]:
    smf, meta = build_smf_bytes(spec, mpc_map=mpc_map)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(smf)
    meta["path"] = str(out)
    return meta


def deploy_midi_to_mpc(
    local_path: Path,
    host: str,
    remote_dir: str = "/media/okcomputer/MidiPatterns",
    ssh_user: str = "root",
) -> Dict[str, Any]:
    """Upload a .mid pattern file directly to MPC storage over SSH/SCP."""
    return deploy_file_to_mpc(
        local_path=Path(local_path),
        host=host,
        remote_dir=remote_dir,
        ssh_user=ssh_user,
        source="midi",
        action="deploy_mid",
    )


def parse_smf_summary(smf_bytes: bytes) -> Dict[str, Any]:
    """Minimal Format-0/1 SMF parser used for verification tests."""
    if smf_bytes[:4] != b"MThd":
        raise ValueError("Invalid MIDI header")
    hdr_len, fmt, ntrks, division = struct.unpack(">IHHH", smf_bytes[4:14])
    pos = 8 + hdr_len
    notes_on = 0
    notes_off = 0
    tempos_us: List[int] = []
    max_tick = 0
    for _ in range(ntrks):
        if smf_bytes[pos:pos + 4] != b"MTrk":
            raise ValueError("Missing MTrk header")
        tlen = struct.unpack(">I", smf_bytes[pos + 4:pos + 8])[0]
        trk = smf_bytes[pos + 8:pos + 8 + tlen]
        pos += 8 + tlen
        idx = 0
        tick = 0
        running = 0
        while idx < len(trk):
            delta, idx = _decode_vlq(trk, idx)
            tick += delta
            if tick > max_tick:
                max_tick = tick
            status = trk[idx]
            if status & 0x80:
                idx += 1
                running = status
            else:
                status = running
            if status == 0xFF:
                meta_type = trk[idx]
                idx += 1
                mlen, idx = _decode_vlq(trk, idx)
                mdata = trk[idx:idx + mlen]
                idx += mlen
                if meta_type == 0x51 and len(mdata) == 3:
                    tempos_us.append(int.from_bytes(mdata, "big"))
            elif status in (0xF0, 0xF7):
                slen, idx = _decode_vlq(trk, idx)
                idx += slen
            else:
                kind = status & 0xF0
                d1 = trk[idx]
                d2 = trk[idx + 1] if kind not in (0xC0, 0xD0) else 0
                idx += 1 if kind in (0xC0, 0xD0) else 2
                if kind == 0x90 and d2 > 0:
                    notes_on += 1
                elif kind == 0x80 or (kind == 0x90 and d2 == 0):
                    notes_off += 1
    return {
        "format": fmt,
        "tracks": ntrks,
        "ppqn": division,
        "notes_on": notes_on,
        "notes_off": notes_off,
        "tempos_us": tempos_us,
        "total_ticks": max_tick,
    }


BUILTIN_PRESETS: Dict[str, Dict[str, Any]] = {
    "dj_premier_fm": {
        "name": "Premier_Banks_Fm_90",
        "bpm": 90.0,
        "bars": 4,
        "steps_per_bar": 16,
        "swing_pct": 53,
        "pads": {
            "1": {"1": {"1": 124, "4": 106, "8": 118, "11": 116}, "2": {"1": 124, "4": 106, "8": 118, "11": 116}, "3": {"1": 124, "4": 106, "8": 118, "11": 116}, "4": {"1": 124, "4": 106, "11": 116}},
            "2": {"1": {"5": 124, "13": 126}, "2": {"5": 124, "13": 126}, "3": {"5": 124, "13": 126}, "4": {"5": 124, "13": 126, "15": 108}},
            "3": {"1": {"1": 98, "3": 68, "5": 94, "7": 72, "9": 96, "11": 70, "13": 94}, "2": {"1": 98, "3": 68, "5": 94, "7": 72, "9": 96, "11": 70, "13": 94, "14": 54, "15": 76}, "3": {"1": 98, "3": 68, "5": 94, "7": 72, "9": 96, "11": 70, "13": 94}, "4": {"1": 98, "3": 68, "5": 94, "7": 72, "9": 96, "11": 70, "13": 94, "15": 78}},
            "4": {"1": {"15": 90}, "3": {"15": 90}},
            "5": {"1": {"16": 90}, "2": {"10": 82, "16": 94}, "3": {"16": 90}, "4": {"16": 96}},
            "6": {"1": {"10": 84, "12": 74}, "2": {"10": 84, "14": 90}, "3": {"10": 84, "12": 74}, "4": {"10": 84, "12": 76}},
            "7": {"1": {"1": 120, "4": 108, "8": 114, "11": 108}, "2": {"1": 120, "4": 108, "8": 114, "11": 108}, "3": {"1": 120, "4": 108, "8": 114, "11": 108}, "4": {"1": 120, "4": 108}},
            "8": {"2": {"15": 104}, "4": {"9": 116, "15": 108}},
            "9": {"1": {"1": 118, "12": 106}, "2": {"8": 114}, "3": {"1": 118}},
            "10": {"1": {"8": 114}, "2": {"1": 116}, "3": {"8": 114}},
            "11": {"2": {"5": 104, "15": 96}, "3": {"11": 108}},
            "12": {"4": {"1": 116}},
            "13": {"4": {"5": 118}},
            "14": {"1": {"5": 98}, "2": {"11": 100}, "3": {"5": 98}},
            "15": {"1": {"15": 116}, "3": {"15": 118}},
            "16": {"4": {"9": 122}},
        },
    },
    "dilla_swung_boom": {
        "name": "Dilla_Human_Pocket_88",
        "bpm": 88.0,
        "bars": 4,
        "steps_per_bar": 16,
        "swing_pct": 58,
        "events": [
            # Bar 1-4 human pocket: unquantized snare (+18 ticks), lazy hats (+14..+28), pushed pickup kicks (-8)
            *[
                {"bar": b, "step": s, "pad": 1, "velocity": v, "nudge_ticks": ng}
                for b in (1, 2, 3, 4)
                for s, v, ng in [(1, 124, 0), (4, 102, 12), (8, 116, 16), (11, 114, 10)]
                if not (b == 4 and s == 8)
            ],
            *[
                {"bar": b, "step": s, "pad": 2, "velocity": v, "nudge_ticks": 18}
                for b in (1, 2, 3, 4)
                for s, v in [(5, 122), (13, 126)]
            ],
            *[
                {"bar": b, "step": s, "pad": 3, "velocity": 92 if s % 4 == 1 else 66, "nudge_ticks": 14 if s % 4 == 1 else 26}
                for b in (1, 2, 3, 4)
                for s in (1, 3, 5, 7, 9, 11, 13, 15)
            ],
            {"bar": 2, "step": 16, "pad": 5, "velocity": 86, "nudge_ticks": -8},
            {"bar": 4, "step": 15, "pad": 6, "velocity": 88, "nudge_ticks": 20},
        ],
    },
}
