#!/usr/bin/env python3
"""Google Flow / Gemini Lyria sample generator and MPC Live FLOW AI menu bridge."""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

try:
    from . import mpc_audio
    from .mpc_client import MpcMap, emit_activity
except ImportError:  # pragma: no cover - direct script execution
    import mpc_audio  # type: ignore
    from mpc_client import MpcMap, emit_activity  # type: ignore


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("MPC_FLOW_CONFIG") or str(BASE_DIR / "flow_config.json"))
SECRETS_PATH = Path(os.environ.get("MPC_FLOW_SECRETS") or str(BASE_DIR / ".flow_secrets.json"))
BANKS_DIR = Path(os.environ.get("MPC_FLOW_BANKS_DIR") or str(BASE_DIR / "flow_banks"))
SEQ_STATE_PATH = BANKS_DIR / "trigger_seq.json"

DEFAULT_DROP_DIRS = [
    "~/Downloads",
    "~/Music/Flow Music",
]
AUDIO_EXTS = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".aif", ".aiff"}
GEMINI_INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
GEMINI_API_REVISION = "2026-05-20"
DEFAULT_LYRIA_MODEL = "lyria-realtime-exp"
LYRIA_REALTIME_SCRIPT = BASE_DIR / "lyria_realtime_fetch.mjs"
DEFAULT_REMOTE_FLOW_DIR = "/media/okcomputer/FlowSamples"
DEFAULT_REMOTE_KITS_DIR = "/media/okcomputer/FlowKits"
DEFAULT_SAMPLE_RATE = 44100

DEFAULT_CONFIG: Dict[str, Any] = {
    "engine": "auto",  # auto | gemini_lyria | synth_flow | flow_drop
    "model": DEFAULT_LYRIA_MODEL,  # lyria-realtime-exp | lyria-3-clip-preview | lyria-3.5
    "mpc_host": "",
    "ssh_user": "root",
    "remote_flow_dir": DEFAULT_REMOTE_FLOW_DIR,
    "remote_kits_dir": DEFAULT_REMOTE_KITS_DIR,
    "drop_dirs": list(DEFAULT_DROP_DIRS),
    "default_bpm": 92.0,
    "default_key": "C minor",
    "default_bars": 4,
    "slice_mode": "onsets",  # onsets | beats | grid
    "match_bpm": True,
    "match_key": False,
    "auto_deploy": True,
}

# Precomputed 4096-point sine LUT for fast pure-Python synthesis
_LUT_SIZE = 4096
_SIN_LUT = [math.sin((2.0 * math.pi * i) / _LUT_SIZE) for i in range(_LUT_SIZE)]


def _fast_sin(phase_01: float) -> float:
    idx = int((phase_01 % 1.0) * _LUT_SIZE)
    return _SIN_LUT[idx]


def default_mpc_host() -> str:
    cfg = load_config()
    if str(cfg.get("mpc_host") or "").strip():
        return str(cfg["mpc_host"]).strip()
    env_host = os.environ.get("MPC_HOST", "").strip()
    if env_host:
        return env_host
    try:
        return str(MpcMap().data.get("meta", {}).get("default_host", "mpc-live.local"))
    except Exception:
        return "mpc-live.local"


def load_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg.update(loaded)
        except Exception:
            pass
    return cfg


def save_config(updates: Dict[str, Any]) -> Dict[str, Any]:
    cfg = load_config()
    allowed_keys = set(DEFAULT_CONFIG.keys())
    for k, v in updates.items():
        if k not in allowed_keys:
            continue
        if k in ("default_bpm",):
            cfg[k] = max(40.0, min(240.0, float(v)))
        elif k in ("default_bars",):
            cfg[k] = max(1, min(16, int(v)))
        elif k in ("match_bpm", "match_key", "auto_deploy"):
            cfg[k] = bool(v)
        elif k == "drop_dirs":
            if isinstance(v, list):
                cfg[k] = [str(x).strip() for x in v if str(x).strip()]
        elif k == "engine":
            eng = str(v).strip()
            if eng not in ("auto", "gemini_lyria", "synth_flow", "flow_drop"):
                raise ValueError(f"Unsupported engine '{eng}'")
            cfg[k] = eng
        elif k == "slice_mode":
            sm = str(v).strip()
            if sm not in ("onsets", "beats", "grid"):
                raise ValueError(f"Unsupported slice_mode '{sm}'")
            cfg[k] = sm
        else:
            cfg[k] = str(v).strip()
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return cfg


def _load_secrets() -> Dict[str, str]:
    if not SECRETS_PATH.exists():
        return {}
    try:
        data = json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_gemini_api_key(api_key: str) -> Dict[str, Any]:
    cleaned = str(api_key or "").strip()
    SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not cleaned:
        if SECRETS_PATH.exists():
            SECRETS_PATH.unlink()
        emit_activity("flow", "config", "Gemini API key cleared")
        return get_api_key_status()
    payload = {"gemini_api_key": cleaned, "updated_at": round(time.time(), 2)}
    SECRETS_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(SECRETS_PATH, 0o600)
    except OSError:
        pass
    emit_activity("flow", "config", "Gemini API key stored locally (chmod 600)")
    return get_api_key_status()


def resolve_gemini_api_key() -> Tuple[Optional[str], str]:
    for env_name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        val = os.environ.get(env_name, "").strip()
        if val:
            return val, f"env:{env_name}"
    sec = _load_secrets()
    stored = str(sec.get("gemini_api_key") or "").strip()
    if stored:
        return stored, "secrets_file"
    return None, "none"


def get_api_key_status() -> Dict[str, Any]:
    key, source = resolve_gemini_api_key()
    if not key:
        return {"configured": False, "source": "none", "hint": ""}
    hint = f"{key[:4]}...{key[-4:]}" if len(key) >= 10 else "****"
    return {"configured": True, "source": source, "hint": hint}


def _next_trigger_seq() -> int:
    BANKS_DIR.mkdir(parents=True, exist_ok=True)
    seq = 1
    if SEQ_STATE_PATH.exists():
        try:
            data = json.loads(SEQ_STATE_PATH.read_text(encoding="utf-8"))
            seq = int(data.get("seq", 0)) + 1
        except Exception:
            seq = int(time.time()) & 0xFFFFFFFF
    SEQ_STATE_PATH.write_text(json.dumps({"seq": seq, "updated_at": time.time()}, indent=2) + "\n", encoding="utf-8")
    return seq


def _slot_dir(slot: int) -> Path:
    s = max(1, min(16, int(slot)))
    return BANKS_DIR / f"bank_{s:02d}"


def list_banks() -> List[Dict[str, Any]]:
    banks: List[Dict[str, Any]] = []
    for slot in range(1, 17):
        bdir = _slot_dir(slot)
        bjson = bdir / "bank.json"
        wav = bdir / "sample.wav"
        info: Dict[str, Any] = {
            "slot": slot,
            "folder": f"bank_{slot:02d}",
            "populated": wav.exists(),
        }
        if bjson.exists():
            try:
                meta = json.loads(bjson.read_text(encoding="utf-8"))
                info.update({
                    "label": meta.get("label") or meta.get("prompt") or f"Bank {slot:02d}",
                    "prompt": meta.get("prompt", ""),
                    "engine": meta.get("engine", ""),
                    "bpm": meta.get("bpm"),
                    "key": meta.get("key"),
                    "camelot": meta.get("camelot"),
                    "duration_s": meta.get("duration_s"),
                    "slice_count": len(meta.get("slices", [])),
                    "updated_at": meta.get("updated_at"),
                })
            except Exception:
                pass
        if wav.exists():
            info["bytes"] = wav.stat().st_size
        banks.append(info)
    return banks


def get_flow_status() -> Dict[str, Any]:
    cfg = load_config()
    key_info = get_api_key_status()
    effective_engine = cfg.get("engine", "auto")
    if effective_engine == "auto":
        effective_engine = "gemini_lyria" if key_info["configured"] else "synth_flow"
    seq_info: Dict[str, Any] = {"seq": 0}
    if SEQ_STATE_PATH.exists():
        try:
            seq_info = json.loads(SEQ_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    drop_dirs_info = []
    for raw in cfg.get("drop_dirs", []):
        p = Path(raw).expanduser()
        drop_dirs_info.append({"path": str(p), "exists": p.exists()})

    banks = list_banks()
    return {
        "ok": True,
        "plugin": {
            "name": "FLOW AI",
            "manufacturer": "MPCUTER",
            "uid": "FLGM",
            "remote_flow_dir": cfg.get("remote_flow_dir", DEFAULT_REMOTE_FLOW_DIR),
        },
        "config": cfg,
        "effective_engine": effective_engine,
        "gemini_api_key": key_info,
        "drop_dirs": drop_dirs_info,
        "trigger_seq": int(seq_info.get("seq", 0)),
        "active_slot": seq_info.get("active_slot", 1),
        "populated_banks": sum(1 for b in banks if b.get("populated")),
        "banks": banks,
    }


# ---------------------------------------------------------------------------
# Built-in Zero-Key Prompt DSP Generator (synth_flow)
# ---------------------------------------------------------------------------

_NOTE_SEMI_OFFSET = {
    "C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3, "E": 4,
    "F": 5, "F#": 6, "Gb": 6, "G": 7, "G#": 8, "Ab": 8, "A": 9,
    "A#": 10, "Bb": 10, "B": 11,
}


def _extract_prompt_hints(prompt: str, bpm: Optional[float], key: Optional[str]) -> Dict[str, Any]:
    p = (prompt or "").strip()
    low = p.lower()

    resolved_bpm = float(bpm) if bpm else None
    if resolved_bpm is None:
        m = re.search(r"\b(\d{2,3}(?:\.\d+)?)\s*bpm\b", low)
        if m:
            val = float(m.group(1))
            if 45.0 <= val <= 220.0:
                resolved_bpm = val
    if resolved_bpm is None:
        if any(w in low for w in ("trap", "drill", "dnb", "jungle")):
            resolved_bpm = 140.0 if "trap" in low or "drill" in low else 172.0
        elif any(w in low for w in ("house", "techno", "garage")):
            resolved_bpm = 124.0
        elif any(w in low for w in ("lofi", "lo-fi", "chill", "boom", "dusty", "jazz")):
            resolved_bpm = 88.0
        else:
            resolved_bpm = float(load_config().get("default_bpm", 92.0))

    resolved_key = str(key or "").strip()
    if not resolved_key:
        km = re.search(r"\b([A-G][#b]?)\s*(min|minor|maj|major|m)\b", p, re.IGNORECASE)
        if km:
            root = km.group(1)[0].upper() + (km.group(1)[1:] if len(km.group(1)) > 1 else "")
            qual = "minor" if km.group(2).lower().startswith("m") and km.group(2).lower() != "major" and km.group(2).lower() != "maj" else "major"
            resolved_key = f"{root} {qual}"
        else:
            resolved_key = str(load_config().get("default_key", "C minor"))

    parsed_key = mpc_audio.parse_key_label(resolved_key)
    root_midi = 48 + int(parsed_key["pitch_class"])  # C3..B3 range for rich chords/bass
    is_minor = parsed_key["mode"] == "minor"

    style = "lofi"
    if any(w in low for w in ("trap", "808", "drill", "dark", "hard")):
        style = "trap"
    elif any(w in low for w in ("house", "techno", "club", "acid")):
        style = "house"
    elif any(w in low for w in ("ambient", "pad", "dream", "cinematic", "space", "synthwave")):
        style = "pad"
    elif any(w in low for w in ("boom", "bap", "hiphop", "hip-hop", "break", "funk")):
        style = "boombap"

    has_drums = not any(w in low for w in ("no drum", "nodrum", "pad only", "chords only", "melody only", "bass only", "acapella"))
    if "drum" in low or "beat" in low or "break" in low or "kit" in low:
        has_drums = True
    has_chords = "drum only" not in low and "drums only" not in low
    dusty = any(w in low for w in ("lofi", "lo-fi", "vinyl", "dusty", "tape", "warm", "vintage"))

    return {
        "prompt": p or "mpc flow loop",
        "bpm": round(resolved_bpm, 2),
        "key_meta": parsed_key,
        "root_midi": root_midi,
        "is_minor": is_minor,
        "style": style,
        "has_drums": has_drums,
        "has_chords": has_chords,
        "dusty": dusty,
    }


def _midi_to_hz(midi: float) -> float:
    return 440.0 * (2.0 ** ((midi - 69.0) / 12.0))


def _render_drum_kit(sr: int, style: str) -> Dict[str, List[float]]:
    n_kick = int(0.24 * sr)
    kick = [0.0] * n_kick
    phase = 0.0
    start_f = 135.0 if style != "trap" else 120.0
    end_f = 42.0 if style == "trap" else 50.0
    decay_k = 5.5 if style == "trap" else 11.0
    for i in range(n_kick):
        t = i / sr
        f = end_f + (start_f - end_f) * math.exp(-t * 28.0)
        phase += f / sr
        env = math.exp(-t * decay_k)
        kick[i] = math.tanh(1.6 * _fast_sin(phase)) * env * 0.85

    n_snare = int(0.19 * sr)
    snare = [0.0] * n_snare
    rng_state = 0x13572468
    ph_s = 0.0
    for i in range(n_snare):
        t = i / sr
        rng_state = (rng_state * 1664525 + 1013904223) & 0xFFFFFFFF
        noise = ((rng_state >> 16) / 32768.0) - 1.0
        ph_s += 188.0 / sr
        snap = noise * math.exp(-t * 17.0) * 0.58
        body = _fast_sin(ph_s) * math.exp(-t * 24.0) * 0.42
        snare[i] = (snap + body) * 0.80

    n_hat = int(0.08 * sr)
    hat = [0.0] * n_hat
    prev_n = 0.0
    for i in range(n_hat):
        t = i / sr
        rng_state = (rng_state * 1664525 + 1013904223) & 0xFFFFFFFF
        noise = ((rng_state >> 16) / 32768.0) - 1.0
        hp = noise - 0.72 * prev_n
        prev_n = noise
        hat[i] = hp * math.exp(-t * 42.0) * 0.34

    return {"kick": kick, "snare": snare, "hat": hat}


def _mix_snippet(buf_l: List[float], buf_r: List[float], start: int, snip: List[float], gain_l: float, gain_r: float) -> None:
    total = len(buf_l)
    end = min(total, start + len(snip))
    for i in range(max(0, start), end):
        v = snip[i - start]
        buf_l[i] += v * gain_l
        buf_r[i] += v * gain_r


def synthesize_flow_wav(
    prompt: str,
    out_wav: Path,
    bpm: Optional[float] = None,
    key: Optional[str] = None,
    bars: int = 4,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> Dict[str, Any]:
    """Generate a beat-locked musical loop + 16 transient slice frames cleanly in pure Python."""
    hints = _extract_prompt_hints(prompt, bpm, key)
    bpm_val = float(hints["bpm"])
    sr = int(sample_rate)
    bars_clamped = max(1, min(8, int(bars)))
    sec_per_beat = 60.0 / bpm_val
    total_beats = bars_clamped * 4
    total_dur = total_beats * sec_per_beat
    if total_dur > 24.0:
        # Keep pure-Python synth responsive (~<= 24s) while preserving bar count
        bars_clamped = max(1, int(24.0 / (4.0 * sec_per_beat)))
        total_beats = bars_clamped * 4
        total_dur = total_beats * sec_per_beat

    n_frames = max(sr // 2, int(round(total_dur * sr)))
    buf_l = [0.0] * n_frames
    buf_r = [0.0] * n_frames

    root_midi = int(hints["root_midi"])
    is_minor = bool(hints["is_minor"])
    style = hints["style"]
    step_s = sec_per_beat / 4.0  # 16th-note step
    total_steps = bars_clamped * 16

    # Minor vs Major 4-bar chord progression (degree offsets + intervals)
    if is_minor:
        prog = [
            (0, [0, 3, 7, 10, 14]),   # i9
            (5, [0, 3, 7, 10, 14]),   # iv9
            (8, [0, 4, 7, 11, 14]),   # VI maj9
            (7, [0, 4, 7, 10, 13]),   # V7b9 / b9 color
        ]
    else:
        prog = [
            (0, [0, 4, 7, 11, 14]),   # Imaj9
            (9, [0, 3, 7, 10, 14]),   # vi9
            (5, [0, 4, 7, 11, 14]),   # IVmaj9
            (7, [0, 4, 7, 10, 14]),   # V9
        ]

    # 1) Polyphonic Rhodes / Warm Pad Chords + Bass per bar
    if hints["has_chords"]:
        for bar in range(bars_clamped):
            deg, intervals = prog[bar % len(prog)]
            chord_root = root_midi + deg
            bar_start = int(round((bar * 4 * sec_per_beat) * sr))
            bar_len = int(round((4 * sec_per_beat) * sr))
            dur_frames = min(n_frames - bar_start, bar_len)
            if dur_frames <= 32:
                continue

            freqs = [_midi_to_hz(chord_root + 12 + iv) / sr for iv in intervals]
            bass_inc = _midi_to_hz(chord_root - 12) / sr
            atk_n = max(16, int((0.09 if style == "pad" else 0.012) * sr))
            rel_n = max(64, int(0.08 * sr))
            decay_rate = 0.45 if style == "pad" else 1.35

            phases = [0.05 * idx for idx in range(len(freqs))]
            b_phase = 0.0
            for j in range(dur_frames):
                t = j / sr
                env = min(1.0, j / atk_n) * math.exp(-t * decay_rate)
                if j > dur_frames - rel_n:
                    env *= max(0.0, (dur_frames - j) / rel_n)

                ch_l = 0.0
                ch_r = 0.0
                for k_idx, inc in enumerate(freqs):
                    phases[k_idx] += inc
                    s_val = _fast_sin(phases[k_idx]) + 0.16 * _fast_sin(phases[k_idx] * 2.0)
                    pan = -0.35 + (0.70 * k_idx / max(1, len(freqs) - 1))
                    ch_l += s_val * (0.5 - 0.4 * pan)
                    ch_r += s_val * (0.5 + 0.4 * pan)

                b_phase += bass_inc
                bass_env = min(1.0, j / max(8, int(0.006 * sr))) * math.exp(-t * 1.1)
                bass = math.tanh(1.3 * _fast_sin(b_phase)) * bass_env * 0.42

                idx = bar_start + j
                buf_l[idx] += (ch_l * 0.15 * env) + bass
                buf_r[idx] += (ch_r * 0.15 * env) + bass

    # 2) Drum Kit Pattern + Transient Markers
    slice_candidates: List[int] = []
    if hints["has_drums"]:
        kit = _render_drum_kit(sr, style)
        if style == "house":
            k_steps = {0, 4, 8, 12}
            s_steps = {4, 12}
            h_steps = {2, 6, 10, 14}
        elif style == "trap":
            k_steps = {0, 6, 10, 14}
            s_steps = {8}
            h_steps = set(range(0, 16, 2)) | {11, 13}
        else:
            k_steps = {0, 7, 10}
            s_steps = {4, 12}
            h_steps = {0, 2, 4, 6, 8, 10, 12, 14}

        for step in range(total_steps):
            in_bar = step % 16
            swing = (step_s * 0.09) if (in_bar % 2 == 1 and style in ("lofi", "boombap")) else 0.0
            pos = int(round((step * step_s + swing) * sr))
            if pos >= n_frames:
                break
            hit = False
            if in_bar in k_steps:
                _mix_snippet(buf_l, buf_r, pos, kit["kick"], 0.90, 0.90)
                hit = True
            if in_bar in s_steps:
                _mix_snippet(buf_l, buf_r, pos, kit["snare"], 0.78, 0.85)
                hit = True
            if in_bar in h_steps:
                _mix_snippet(buf_l, buf_r, pos, kit["hat"], 0.42, 0.52)
            if hit:
                slice_candidates.append(pos)

    # 3) Subtle vinyl/analog warmth noise if lofi/dusty prompt
    if hints["dusty"]:
        rng = 0x5A170001
        for i in range(n_frames):
            rng = (rng * 1664525 + 1013904223) & 0xFFFFFFFF
            crackle = (((rng >> 16) / 32768.0) - 1.0) * 0.004
            buf_l[i] += crackle
            buf_r[i] += crackle * 0.85

    # Normalize cleanly to -1.5 dBFS + smooth loop edges
    fade_edge = min(256, n_frames // 20)
    peak = 1e-6
    for i in range(n_frames):
        a = abs(buf_l[i])
        b = abs(buf_r[i])
        if a > peak:
            peak = a
        if b > peak:
            peak = b
    gain = 0.84 / peak

    pcm = array_pcm16(buf_l, buf_r, n_frames, gain, fade_edge)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_wav), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm)

    slice_starts = sorted(set(slice_candidates))
    if len(slice_starts) < 16:
        slice_starts = [int((n_frames * i) / 16) for i in range(16)]
    else:
        # Spread evenly to 16 hits across available transients
        step_pick = len(slice_starts) / 16.0
        slice_starts = [slice_starts[int(i * step_pick)] for i in range(16)]

    slices: List[Dict[str, int]] = []
    for i in range(16):
        s0 = max(0, min(n_frames - 1, slice_starts[i]))
        s1 = slice_starts[i + 1] if i + 1 < 16 else n_frames
        if s1 <= s0:
            s1 = min(n_frames, s0 + max(1, n_frames // 16))
        slices.append({"start": int(s0), "end": int(s1)})

    return {
        "engine": "synth_flow",
        "prompt": hints["prompt"],
        "bpm": bpm_val,
        "key": hints["key_meta"]["key"],
        "camelot": hints["key_meta"]["camelot"],
        "root_midi": int(hints["root_midi"]),
        "sample_rate": sr,
        "num_frames": n_frames,
        "duration_s": round(n_frames / sr, 3),
        "slices": slices,
    }


def array_pcm16(buf_l: List[float], buf_r: List[float], n_frames: int, gain: float, fade_edge: int) -> bytes:
    from array import array

    out = array("h")
    out.extend([0] * (n_frames * 2))
    denom = float(max(1, fade_edge))
    for i in range(n_frames):
        env = 1.0
        if i < fade_edge:
            env = i / denom
        elif i >= n_frames - fade_edge:
            env = (n_frames - 1 - i) / denom
        l_val = int(max(-32767, min(32767, math.tanh(buf_l[i] * gain) * env * 32767.0)))
        r_val = int(max(-32767, min(32767, math.tanh(buf_r[i] * gain) * env * 32767.0)))
        idx = i * 2
        out[idx] = l_val
        out[idx + 1] = r_val
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()


# ---------------------------------------------------------------------------
# Gemini API Lyria 3 Client
# ---------------------------------------------------------------------------

def _extract_audio_from_interaction(data: Dict[str, Any]) -> Tuple[bytes, str, str]:
    """Extract base64 audio bytes, mime_type, and optional text/lyrics from Gemini Interactions JSON."""
    texts: List[str] = []
    candidates: List[Tuple[str, str]] = []

    def _collect(obj: Any) -> None:
        if isinstance(obj, dict):
            typ = obj.get("type")
            if typ == "audio" and isinstance(obj.get("data"), str):
                candidates.append((obj["data"], str(obj.get("mime_type") or obj.get("mimeType") or "audio/mpeg")))
            if typ == "text" and isinstance(obj.get("text"), str):
                texts.append(obj["text"])
            for k in ("output_audio", "outputAudio"):
                sub = obj.get(k)
                if isinstance(sub, dict) and isinstance(sub.get("data"), str):
                    candidates.append((sub["data"], str(sub.get("mime_type") or sub.get("mimeType") or "audio/mpeg")))
            for k in ("output_text", "outputText"):
                if isinstance(obj.get(k), str):
                    texts.append(obj[k])
            for v in obj.values():
                _collect(v)
        elif isinstance(obj, list):
            for item in obj:
                _collect(item)

    _collect(data)
    if not candidates:
        raise RuntimeError("Gemini Interactions yanıtında audio bloğu bulunamadı")
    b64_data, mime = candidates[-1]
    return base64.b64decode(b64_data), mime, "\n".join(texts).strip()


def call_gemini_lyria(
    prompt: str,
    out_raw_path: Path,
    bpm: Optional[float] = None,
    key: Optional[str] = None,
    model: str = "lyria-3-clip-preview",
    api_key: Optional[str] = None,
    timeout_s: float = 120.0,
) -> Dict[str, Any]:
    resolved_key = api_key
    if not resolved_key:
        resolved_key, _ = resolve_gemini_api_key()
    if not resolved_key:
        raise RuntimeError(
            "GEMINI_API_KEY tanımlı değil. Key eklemek için `mpc_cli.py flow config --api-key <KEY>` veya Web UI kullanın."
        )

    full_prompt = prompt.strip()
    meta_parts: List[str] = ["Instrumental sample loop for Akai MPC sampler, tight grid, clean mix"]
    if bpm:
        meta_parts.append(f"{float(bpm):.1f} BPM")
    if key:
        meta_parts.append(f"Key of {key}")
    enriched_input = f"{full_prompt}. ({', '.join(meta_parts)})"

    payload: Dict[str, Any] = {
        "model": model or "lyria-3-clip-preview",
        "input": enriched_input,
    }
    if payload["model"] == "lyria-3.5":
        payload["response_format"] = {"type": "audio"}

    req = urllib.request.Request(
        GEMINI_INTERACTIONS_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Api-Revision": GEMINI_API_REVISION,
            "x-goog-api-key": resolved_key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            resp_json = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Gemini Lyria API HTTP {exc.code}: {body}") from exc

    raw_bytes, mime, notes_text = _extract_audio_from_interaction(resp_json)
    ext = ".wav" if (raw_bytes[:4] == b"RIFF" or "wav" in mime.lower()) else ".mp3"
    final_raw = out_raw_path.with_suffix(ext)
    final_raw.parent.mkdir(parents=True, exist_ok=True)
    final_raw.write_bytes(raw_bytes)
    return {
        "raw_file": final_raw,
        "mime_type": mime,
        "model": payload["model"],
        "notes_text": notes_text,
        "bytes": len(raw_bytes),
    }


_LYRIA_SCALES_BY_MAJOR_PC = (
    "C_MAJOR_A_MINOR",
    "D_FLAT_MAJOR_B_FLAT_MINOR",
    "D_MAJOR_B_MINOR",
    "E_FLAT_MAJOR_C_MINOR",
    "E_MAJOR_D_FLAT_MINOR",
    "F_MAJOR_D_MINOR",
    "G_FLAT_MAJOR_E_FLAT_MINOR",
    "G_MAJOR_E_MINOR",
    "A_FLAT_MAJOR_F_MINOR",
    "A_MAJOR_G_FLAT_MINOR",
    "B_FLAT_MAJOR_G_MINOR",
    "B_MAJOR_A_FLAT_MINOR",
)


def _key_to_lyria_scale(key_str: Optional[str]) -> Optional[str]:
    """Map musical key label (e.g. 'C minor', '5A') to Lyria RealTime Scale enum."""
    if not str(key_str or "").strip():
        return None
    try:
        km = mpc_audio.parse_key_label(str(key_str))
        pc = int(km["pitch_class"])
        rel_pc = pc if km.get("mode") == "major" else (pc + 3) % 12
        return _LYRIA_SCALES_BY_MAJOR_PC[rel_pc]
    except Exception:
        return None


def _find_node_binary() -> str:
    env = mpc_audio._augmented_env()
    found = shutil.which("node", path=env.get("PATH"))
    if found:
        return found
    for cand in ("/opt/homebrew/bin/node", "/usr/local/bin/node", "/usr/bin/node"):
        if Path(cand).is_file():
            return cand
    raise RuntimeError("Node.js bulunamadı; Lyria RealTime WebSocket köprüsü için Node.js 22+ gerekir.")


def _trim_pcm16_warmup(pcm: bytes, sr: int = 48000, channels: int = 2, keep_min_s: float = 2.0, max_trim_s: float = 1.6) -> bytes:
    """Drop leading quiet startup frames (< -36 dBFS) while preserving the first attack transient."""
    from array import array

    frame_bytes = max(2, channels * 2)
    usable = (len(pcm) // frame_bytes) * frame_bytes
    if usable <= 0:
        return b""
    min_keep_bytes = int(keep_min_s * sr) * frame_bytes
    max_trim_bytes = min(int(max_trim_s * sr) * frame_bytes, max(0, usable - min_keep_bytes))
    block_bytes = int(0.05 * sr) * frame_bytes
    if max_trim_bytes < block_bytes:
        return pcm[:usable]

    trim_at = 0
    for off in range(0, max_trim_bytes, block_bytes):
        samples = array("h")
        samples.frombytes(pcm[off : off + block_bytes])
        if sys.byteorder != "little":
            samples.byteswap()
        sub = samples[::16]
        if not sub:
            continue
        peak = max(abs(min(sub)), abs(max(sub)))
        if peak >= 500:
            back_bytes = int(0.05 * sr) * frame_bytes
            trim_at = max(0, off - back_bytes)
            break
    return pcm[trim_at:usable]


def call_lyria_realtime(
    prompt: str,
    out_raw_path: Path,
    bpm: Optional[float] = None,
    key: Optional[str] = None,
    bars: int = 4,
    model: str = DEFAULT_LYRIA_MODEL,
    api_key: Optional[str] = None,
    timeout_s: float = 45.0,
) -> Dict[str, Any]:
    """Stream 48kHz stereo PCM from Google Gemini Lyria RealTime (BidiGenerateMusic) via WebSocket."""
    resolved_key = api_key
    if not resolved_key:
        resolved_key, _ = resolve_gemini_api_key()
    if not resolved_key:
        raise RuntimeError(
            "GEMINI_API_KEY tanımlı değil. Key eklemek için `mpc_cli.py flow config --api-key <KEY>` veya Web UI kullanın."
        )
    if not LYRIA_REALTIME_SCRIPT.is_file():
        raise RuntimeError(f"Lyria RealTime bridge script missing: {LYRIA_REALTIME_SCRIPT}")

    use_bpm = float(bpm or 92.0)
    target_s = max(1.0, min(28.0, float(max(1, int(bars))) * 4.0 * (60.0 / max(40.0, use_bpm))))
    capture_s = min(32.0, max(6.0, target_s + 2.2))

    raw_pcm_path = out_raw_path.with_suffix(".pcm")
    final_wav = out_raw_path.with_suffix(".wav")
    final_wav.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "api_key": resolved_key,
        "prompt": prompt.strip(),
        "model": model or DEFAULT_LYRIA_MODEL,
        "bpm": max(60, min(200, int(round(use_bpm)))),
        "scale": _key_to_lyria_scale(key),
        "duration_s": round(capture_s, 3),
        "out_pcm": str(raw_pcm_path),
        "timeout_ms": int(max(15000, timeout_s * 1000)),
    }
    node_bin = _find_node_binary()
    proc = subprocess.run(
        [node_bin, str(LYRIA_REALTIME_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=mpc_audio._augmented_env(),
        timeout=max(20.0, timeout_s + 5.0),
    )
    if proc.returncode != 0:
        err_msg = (proc.stderr or proc.stdout or "unknown error").strip()
        err_msg = re.sub(r"key=[^&\s\"']+", "key=REDACTED", err_msg)
        raise RuntimeError(f"Gemini Lyria RealTime failed: {err_msg[:400]}")

    try:
        meta = json.loads((proc.stdout or "{}").strip().splitlines()[-1])
    except Exception:
        meta = {}
    if not raw_pcm_path.is_file() or raw_pcm_path.stat().st_size == 0:
        raise RuntimeError("Gemini Lyria RealTime boş ses çıktısı üretti")

    sr = int(meta.get("sample_rate") or 48000)
    ch = int(meta.get("channels") or 2)
    raw_pcm = raw_pcm_path.read_bytes()
    trimmed_pcm = _trim_pcm16_warmup(raw_pcm, sr=sr, channels=ch, keep_min_s=max(1.0, min(target_s, 4.0)))
    raw_pcm_path.unlink(missing_ok=True)

    with wave.open(str(final_wav), "wb") as wf:
        wf.setnchannels(ch)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(trimmed_pcm)

    return {
        "raw_file": final_wav,
        "mime_type": meta.get("mime_type", "audio/l16;rate=48000;channels=2"),
        "model": str(model or DEFAULT_LYRIA_MODEL),
        "notes_text": f"Lyria RealTime WebSocket stream ({meta.get('chunks', 0)} chunks @ {sr}Hz, scale={payload['scale'] or 'auto'})",
        "bytes": len(trimmed_pcm),
    }


# ---------------------------------------------------------------------------
# MPC 16-Slice Packaging & Live Trigger Deployment
# ---------------------------------------------------------------------------

def _build_slices_for_wav(wav_path: Path, n_frames: int, dur_s: float, slice_mode: str) -> List[Dict[str, int]]:
    n_frames = max(16, int(n_frames))
    if slice_mode == "grid":
        return [{"start": int((n_frames * i) / 16), "end": int((n_frames * (i + 1)) / 16)} for i in range(16)]

    try:
        beat_info = mpc_audio.detect_beat(wav_path, backend="auto")
        raw_times = beat_info.get("beats_s") if slice_mode == "beats" else beat_info.get("onsets_s")
        cuts = mpc_audio._debounce_timestamps(raw_times or [0.0], 0.08, 16)
    except Exception:
        cuts = []

    if len(cuts) < 4 or dur_s <= 0.0:
        return [{"start": int((n_frames * i) / 16), "end": int((n_frames * (i + 1)) / 16)} for i in range(16)]

    # Convert timestamps to sample frames and pad up to 16 regions evenly if fewer
    frames = [max(0, min(n_frames - 1, int(round((t / dur_s) * n_frames)))) for t in cuts]
    frames = sorted(set([0] + frames))[:16]
    while len(frames) < 16:
        # Split the largest remaining interval in half
        best_idx = 0
        best_gap = -1
        extended = frames + [n_frames]
        for idx in range(len(frames)):
            gap = extended[idx + 1] - extended[idx]
            if gap > best_gap:
                best_gap = gap
                best_idx = idx
        mid = frames[best_idx] + best_gap // 2
        if mid <= frames[best_idx]:
            break
        frames.append(mid)
        frames.sort()

    while len(frames) < 16:
        frames.append(min(n_frames - 1, (n_frames * len(frames)) // 16))
        frames.sort()

    slices: List[Dict[str, int]] = []
    for i in range(16):
        s0 = frames[i]
        s1 = frames[i + 1] if i + 1 < 16 else n_frames
        if s1 <= s0:
            s1 = min(n_frames, s0 + max(1, n_frames // 16))
        slices.append({"start": int(s0), "end": int(s1)})
    return slices


def _read_wav_frame_count(wav_path: Path) -> Tuple[int, int, float]:
    with wave.open(str(wav_path), "rb") as wf:
        sr = wf.getframerate()
        nf = wf.getnframes()
        return sr, nf, (nf / sr if sr else 0.0)


def _trim_wav_bars(src_wav: Path, dst_wav: Path, bpm: float, bars: int) -> None:
    target_s = max(1.0, min(28.0, float(bars) * 4.0 * (60.0 / max(40.0, float(bpm)))))
    ff = mpc_audio._which("ffmpeg")
    if ff:
        cmd = [
            ff, "-y", "-v", "error", "-i", str(src_wav),
            "-t", f"{target_s:.4f}",
            "-af", f"afade=t=in:st=0:d=0.008,afade=t=out:st={max(0.1, target_s - 0.015):.4f}:d=0.015",
            "-ar", str(DEFAULT_SAMPLE_RATE), "-ac", "2", "-c:a", "pcm_s16le", str(dst_wav),
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True, env=mpc_audio._augmented_env())
        return
    shutil.copy2(src_wav, dst_wav)


def _write_trigger_text(seq: int, slot: int, bpm: float, key: str, prompt: str) -> str:
    clean_prompt = " ".join(str(prompt or "").split())[:96]
    return (
        f"seq={int(seq)}\n"
        f"slot={max(1, min(16, int(slot)))}\n"
        f"bpm={float(bpm):.2f}\n"
        f"key={key}\n"
        f"prompt={clean_prompt}\n"
    )


def deploy_bank_to_mpc(
    slot: int,
    bank_dir: Path,
    seq: int,
    bpm: float,
    key: str,
    prompt: str,
    host: Optional[str] = None,
    remote_flow_dir: Optional[str] = None,
    ssh_user: Optional[str] = None,
) -> Dict[str, Any]:
    cfg = load_config()
    target_host = host or default_mpc_host()
    user = ssh_user or str(cfg.get("ssh_user") or "root")
    remote_root = (remote_flow_dir or str(cfg.get("remote_flow_dir") or DEFAULT_REMOTE_FLOW_DIR)).rstrip("/")
    remote_bank = f"{remote_root}/bank_{int(slot):02d}"

    wav_local = bank_dir / "sample.wav"
    json_local = bank_dir / "bank.json"
    trig_local = bank_dir / "live_trigger.txt"
    trig_local.write_text(_write_trigger_text(seq, slot, bpm, key, prompt), encoding="utf-8")

    subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", f"{user}@{target_host}", f"mkdir -p {shlex.quote(remote_bank)}"],
        check=True, capture_output=True, text=True,
    )
    for fname, remote_dest in (
        (wav_local, f"{remote_bank}/sample.wav"),
        (json_local, f"{remote_bank}/bank.json"),
        (trig_local, f"{remote_root}/live_trigger.txt"),
    ):
        tmp_dest = f"{remote_dest}.tmp"
        subprocess.run(
            ["scp", "-o", "ConnectTimeout=5", str(fname), f"{user}@{target_host}:{tmp_dest}"],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", f"{user}@{target_host}",
             f"mv -f {shlex.quote(tmp_dest)} {shlex.quote(remote_dest)}"],
            check=True, capture_output=True, text=True,
        )

    emit_activity(
        "flow", "deploy_bank",
        f"bank_{int(slot):02d} seq={seq} ({wav_local.stat().st_size}B) -> {target_host}:{remote_bank}",
        target=f"mpc:{target_host}",
    )
    return {
        "deployed": True,
        "host": target_host,
        "remote_bank": remote_bank,
        "trigger_file": f"{remote_root}/live_trigger.txt",
        "seq": seq,
    }


def select_bank(slot: int, deploy: bool = True, host: Optional[str] = None) -> Dict[str, Any]:
    s = max(1, min(16, int(slot)))
    bdir = _slot_dir(s)
    bjson = bdir / "bank.json"
    meta: Dict[str, Any] = {}
    if bjson.exists():
        try:
            meta = json.loads(bjson.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    seq = _next_trigger_seq()
    SEQ_STATE_PATH.write_text(
        json.dumps({"seq": seq, "active_slot": s, "updated_at": time.time()}, indent=2) + "\n",
        encoding="utf-8",
    )

    bpm = float(meta.get("bpm") or load_config().get("default_bpm", 92.0))
    key = str(meta.get("key") or load_config().get("default_key", "C minor"))
    prompt = str(meta.get("prompt") or meta.get("label") or f"Bank {s:02d}")

    out: Dict[str, Any] = {"ok": True, "slot": s, "seq": seq, "meta": meta}
    if deploy:
        if (bdir / "sample.wav").exists() and bjson.exists():
            out["deploy"] = deploy_bank_to_mpc(s, bdir, seq, bpm, key, prompt, host=host)
        else:
            # Bank exists only on MPC (or user selects slot before audio push); update live_trigger.txt remotely
            cfg = load_config()
            target_host = host or default_mpc_host()
            user = str(cfg.get("ssh_user") or "root")
            remote_root = str(cfg.get("remote_flow_dir") or DEFAULT_REMOTE_FLOW_DIR).rstrip("/")
            trig_txt = _write_trigger_text(seq, s, bpm, key, prompt)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tf:
                tf.write(trig_txt)
                tmp_local = Path(tf.name)
            try:
                subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=5", f"{user}@{target_host}", f"mkdir -p {shlex.quote(remote_root)}"],
                    check=True, capture_output=True, text=True,
                )
                tmp_dest = f"{remote_root}/live_trigger.txt.tmp"
                subprocess.run(
                    ["scp", "-o", "ConnectTimeout=5", str(tmp_local), f"{user}@{target_host}:{tmp_dest}"],
                    check=True, capture_output=True, text=True,
                )
                subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=5", f"{user}@{target_host}",
                     f"mv -f {shlex.quote(tmp_dest)} {shlex.quote(remote_root + '/live_trigger.txt')}"],
                    check=True, capture_output=True, text=True,
                )
            finally:
                tmp_local.unlink(missing_ok=True)
            out["deploy"] = {"deployed": True, "host": target_host, "trigger_only": True, "seq": seq}
    emit_activity("flow", "select_bank", f"Active FLOW AI bank -> bank_{s:02d} (seq={seq})")
    return out


def package_audio_into_bank(
    source_audio: Path,
    slot: int = 1,
    prompt: str = "",
    engine: str = "flow_drop",
    target_bpm: Optional[float] = None,
    target_key: Optional[str] = None,
    bars: int = 4,
    slice_mode: str = "onsets",
    match_bpm: bool = True,
    match_key: bool = False,
    deploy: bool = True,
    host: Optional[str] = None,
    notes_text: str = "",
) -> Dict[str, Any]:
    """Analyze, optionally time-stretch/key-match, compute 16 slices, store in flow_banks/, and push to MPC."""
    s = max(1, min(16, int(slot)))
    bdir = _slot_dir(s)
    bdir.mkdir(parents=True, exist_ok=True)
    final_wav = bdir / "sample.wav"

    src = Path(source_audio).expanduser().resolve()
    analysis = mpc_audio.analyze_audio(src)
    detected_bpm = float(analysis.get("bpm") or target_bpm or 120.0)
    detected_key = str(analysis.get("key") or target_key or "C minor")

    with tempfile.TemporaryDirectory(prefix="flow_pkg_") as td:
        tmp_dir = Path(td)
        staged_wav = tmp_dir / "staged.wav"
        mpc_audio._finalize_mpc_wav(src, staged_wav, sample_rate=DEFAULT_SAMPLE_RATE, bit_depth=16)

        need_bpm = bool(match_bpm and target_bpm and abs(float(target_bpm) - detected_bpm) > 1.5 and detected_bpm > 20.0)
        need_key = bool(match_key and target_key and target_key != detected_key)

        final_bpm = float(target_bpm) if (target_bpm and need_bpm) else detected_bpm
        final_key = str(target_key) if (target_key and need_key) else detected_key

        if need_bpm or need_key:
            stretched_wav = tmp_dir / "stretched.wav"
            try:
                mpc_audio.stretch_audio(
                    staged_wav,
                    output_path=stretched_wav,
                    backend="rubberband-r3",
                    source_bpm=detected_bpm if need_bpm else None,
                    target_bpm=float(target_bpm) if need_bpm else None,
                    source_key=detected_key if need_key else None,
                    target_key=target_key if need_key else None,
                    sample_rate=DEFAULT_SAMPLE_RATE,
                    bit_depth=16,
                )
                staged_wav = stretched_wav
            except Exception:
                final_bpm = detected_bpm
                final_key = detected_key

        _trim_wav_bars(staged_wav, final_wav, final_bpm, bars)

    sr, n_frames, dur_s = _read_wav_frame_count(final_wav)
    slices = _build_slices_for_wav(final_wav, n_frames, dur_s, slice_mode)
    key_meta = mpc_audio.parse_key_label(final_key)
    root_midi = 48 + int(key_meta["pitch_class"])

    label = (" ".join((prompt or src.stem).split()))[:56] or f"Flow Bank {s:02d}"
    bank_meta: Dict[str, Any] = {
        "slot": s,
        "label": label,
        "prompt": prompt or src.stem,
        "engine": engine,
        "bpm": round(final_bpm, 2),
        "detected_bpm": round(detected_bpm, 2),
        "key": key_meta["key"],
        "camelot": key_meta["camelot"],
        "root_midi": root_midi,
        "sample_rate": sr,
        "num_frames": n_frames,
        "duration_s": round(dur_s, 3),
        "slice_mode": slice_mode,
        "slices": slices,
        "notes_text": notes_text[:400] if notes_text else "",
        "updated_at": round(time.time(), 2),
    }
    (bdir / "bank.json").write_text(json.dumps(bank_meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    seq = _next_trigger_seq()
    SEQ_STATE_PATH.write_text(
        json.dumps({"seq": seq, "active_slot": s, "updated_at": time.time()}, indent=2) + "\n",
        encoding="utf-8",
    )

    deploy_res: Optional[Dict[str, Any]] = None
    if deploy:
        deploy_res = deploy_bank_to_mpc(s, bdir, seq, bank_meta["bpm"], bank_meta["key"], bank_meta["prompt"], host=host)

    return {
        "ok": True,
        "slot": s,
        "seq": seq,
        "wav": str(final_wav),
        "bank_json": str(bdir / "bank.json"),
        "meta": bank_meta,
        "deploy": deploy_res,
    }


def generate_sample_bank(
    prompt: str,
    slot: int = 1,
    bpm: Optional[float] = None,
    key: Optional[str] = None,
    bars: Optional[int] = None,
    engine: Optional[str] = None,
    model: Optional[str] = None,
    slice_mode: Optional[str] = None,
    match_bpm: Optional[bool] = None,
    match_key: Optional[bool] = None,
    deploy: Optional[bool] = None,
    host: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate audio via Gemini Lyria 3 or SynthFlow fallback, slice to 16 pads, and push to FLOW AI banks."""
    cfg = load_config()
    s = max(1, min(16, int(slot)))
    bdir = _slot_dir(s)
    bdir.mkdir(parents=True, exist_ok=True)

    chosen_engine = (engine or cfg.get("engine") or "auto").strip()
    if chosen_engine == "auto":
        has_key = bool(api_key or resolve_gemini_api_key()[0])
        chosen_engine = "gemini_lyria" if has_key else "synth_flow"

    use_bpm = float(bpm) if bpm is not None else None
    use_key = str(key).strip() if key else None
    use_bars = int(bars if bars is not None else cfg.get("default_bars", 4))
    use_slice = slice_mode or str(cfg.get("slice_mode") or "onsets")
    use_deploy = bool(cfg.get("auto_deploy", True) if deploy is None else deploy)
    use_match_bpm = bool(cfg.get("match_bpm", True) if match_bpm is None else match_bpm)
    use_match_key = bool(cfg.get("match_key", False) if match_key is None else match_key)

    emit_activity("flow", "generate_start", f"bank_{s:02d} [{chosen_engine}] prompt='{prompt[:48]}'")

    if chosen_engine == "gemini_lyria":
        selected_model = str(model or cfg.get("model") or DEFAULT_LYRIA_MODEL).strip()
        target_bpm_val = use_bpm or float(cfg.get("default_bpm", 92.0))
        target_key_val = use_key or str(cfg.get("default_key", "C minor"))
        with tempfile.TemporaryDirectory(prefix="flow_lyria_") as td:
            raw_path = Path(td) / "lyria_raw"
            if "realtime" in selected_model.lower():
                raw_res = call_lyria_realtime(
                    prompt=prompt,
                    out_raw_path=raw_path,
                    bpm=target_bpm_val,
                    key=target_key_val,
                    bars=use_bars,
                    model=selected_model,
                    api_key=api_key,
                )
            else:
                try:
                    raw_res = call_gemini_lyria(
                        prompt=prompt,
                        out_raw_path=raw_path,
                        bpm=target_bpm_val,
                        key=target_key_val,
                        model=selected_model,
                        api_key=api_key,
                    )
                except RuntimeError as exc:
                    msg = str(exc)
                    if "HTTP 429" in msg and ("Free Tier" in msg or "limit: 0" in msg):
                        raw_res = call_lyria_realtime(
                            prompt=prompt,
                            out_raw_path=raw_path,
                            bpm=target_bpm_val,
                            key=target_key_val,
                            bars=use_bars,
                            model=DEFAULT_LYRIA_MODEL,
                            api_key=api_key,
                        )
                    else:
                        raise
            res = package_audio_into_bank(
                source_audio=raw_res["raw_file"],
                slot=s,
                prompt=prompt,
                engine="gemini_lyria",
                target_bpm=use_bpm,
                target_key=use_key,
                bars=use_bars,
                slice_mode=use_slice,
                match_bpm=use_match_bpm,
                match_key=use_match_key,
                deploy=use_deploy,
                host=host,
                notes_text=raw_res.get("notes_text", ""),
            )
            res["lyria_model"] = raw_res["model"]
            emit_activity("flow", "generate_done", f"Lyria -> bank_{s:02d} {res['meta']['bpm']} BPM {res['meta']['key']}")
            return res

    if chosen_engine == "synth_flow":
        final_wav = bdir / "sample.wav"
        meta = synthesize_flow_wav(
            prompt=prompt,
            out_wav=final_wav,
            bpm=use_bpm,
            key=use_key,
            bars=use_bars,
        )
        bank_meta: Dict[str, Any] = {
            "slot": s,
            "label": (" ".join(prompt.split()))[:56] or f"Synth Flow {s:02d}",
            "prompt": prompt,
            "engine": "synth_flow",
            "bpm": meta["bpm"],
            "detected_bpm": meta["bpm"],
            "key": meta["key"],
            "camelot": meta["camelot"],
            "root_midi": meta["root_midi"],
            "sample_rate": meta["sample_rate"],
            "num_frames": meta["num_frames"],
            "duration_s": meta["duration_s"],
            "slice_mode": use_slice,
            "slices": (
                _build_slices_for_wav(final_wav, meta["num_frames"], meta["duration_s"], "grid")
                if use_slice == "grid"
                else meta["slices"]
            ),
            "updated_at": round(time.time(), 2),
        }
        (bdir / "bank.json").write_text(json.dumps(bank_meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        seq = _next_trigger_seq()
        SEQ_STATE_PATH.write_text(
            json.dumps({"seq": seq, "active_slot": s, "updated_at": time.time()}, indent=2) + "\n",
            encoding="utf-8",
        )
        deploy_res = None
        if use_deploy:
            deploy_res = deploy_bank_to_mpc(s, bdir, seq, bank_meta["bpm"], bank_meta["key"], prompt, host=host)

        emit_activity("flow", "generate_done", f"SynthFlow -> bank_{s:02d} {bank_meta['bpm']} BPM {bank_meta['key']}")
        return {
            "ok": True,
            "slot": s,
            "seq": seq,
            "wav": str(final_wav),
            "bank_json": str(bdir / "bank.json"),
            "meta": bank_meta,
            "deploy": deploy_res,
        }

    raise ValueError(f"generate_sample_bank için uygun engine seçin ('gemini_lyria' veya 'synth_flow', verilen: '{chosen_engine}')")


# ---------------------------------------------------------------------------
# Flow Drop Watcher (watches ~/Downloads and ~/Music/Flow Music)
# ---------------------------------------------------------------------------

def _iter_drop_files(dirs: Iterable[Path]) -> Dict[Path, float]:
    found: Dict[Path, float] = {}
    for d in dirs:
        if not d.exists() or not d.is_dir():
            continue
        for entry in d.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in AUDIO_EXTS:
                continue
            try:
                found[entry.resolve()] = entry.stat().st_mtime
            except OSError:
                continue
    return found


def watch_flow_drops(
    slot: int = 1,
    interval_s: float = 2.0,
    auto_advance_slot: bool = True,
    deploy: bool = True,
    host: Optional[str] = None,
    once: bool = False,
    on_imported: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> int:
    cfg = load_config()
    watch_dirs = [Path(p).expanduser() for p in cfg.get("drop_dirs", DEFAULT_DROP_DIRS)]
    seen = _iter_drop_files(watch_dirs)
    cur_slot = max(1, min(16, int(slot)))

    print(f"[flow-watch] İzlenen klasörler: {', '.join(str(p) for p in watch_dirs)}", flush=True)
    emit_activity("flow", "watch_start", f"Flow drops izleniyor ({len(watch_dirs)} klasör)")

    while True:
        time.sleep(max(0.4, interval_s))
        now_files = _iter_drop_files(watch_dirs)
        new_files = [p for p, mtime in sorted(now_files.items(), key=lambda x: x[1]) if seen.get(p) != mtime]
        for fpath in new_files:
            seen[fpath] = now_files[fpath]
            try:
                res = package_audio_into_bank(
                    source_audio=fpath,
                    slot=cur_slot,
                    prompt=fpath.stem,
                    engine="flow_drop",
                    target_bpm=float(cfg.get("default_bpm", 92.0)) if cfg.get("match_bpm") else None,
                    bars=int(cfg.get("default_bars", 4)),
                    slice_mode=str(cfg.get("slice_mode") or "onsets"),
                    match_bpm=bool(cfg.get("match_bpm", True)),
                    deploy=deploy,
                    host=host,
                )
                print(
                    f"[flow-watch] Aktarıldı: {fpath.name} -> bank_{cur_slot:02d} "
                    f"({res['meta']['bpm']} BPM | {res['meta']['key']})",
                    flush=True,
                )
                if on_imported:
                    on_imported(res)
                if auto_advance_slot:
                    cur_slot = (cur_slot % 16) + 1
            except Exception as exc:
                print(f"[flow-watch] Hata ({fpath.name}): {exc}", flush=True)
            if once:
                return 0
        if once:
            return 0


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_flow_cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="mpc_cli.py flow", description="Google Flow -> MPC FLOW AI kontrol köprüsü")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Engine, API key durumu ve 16 banka slotunu göster")

    p_banks = sub.add_parser("banks", help="16 FlowGen bankasını listele")
    p_banks.add_argument("--json", action="store_true")

    p_cfg = sub.add_parser("config", help="Flow çalışma ayarlarını veya Gemini API key'i güncelle")
    p_cfg.add_argument("--engine", choices=["auto", "gemini_lyria", "synth_flow", "flow_drop"])
    p_cfg.add_argument("--model", help="gemini_lyria modeli: lyria-realtime-exp | lyria-3-clip-preview | lyria-3.5")
    p_cfg.add_argument("--api-key", help="Gemini API anahtarı (.flow_secrets.json chmod 600 olarak saklanır)")
    p_cfg.add_argument("--clear-api-key", action="store_true")
    p_cfg.add_argument("--bpm", type=float)
    p_cfg.add_argument("--key")
    p_cfg.add_argument("--bars", type=int)
    p_cfg.add_argument("--slice-mode", choices=["onsets", "beats", "grid"])
    p_cfg.add_argument("--host")

    p_gen = sub.add_parser("generate", help="Prompt ile sample üret, 16 slice'a böl ve MPC'ye sıcak yükle")
    p_gen.add_argument("prompt", help="Ör: 'dusty lofi rhodes chords with vinyl crackle'")
    p_gen.add_argument("--slot", type=int, default=1)
    p_gen.add_argument("--bpm", type=float)
    p_gen.add_argument("--key")
    p_gen.add_argument("--bars", type=int)
    p_gen.add_argument("--engine", choices=["auto", "gemini_lyria", "synth_flow"])
    p_gen.add_argument("--model")
    p_gen.add_argument("--slice-mode", choices=["onsets", "beats", "grid"])
    p_gen.add_argument("--no-deploy", action="store_true")
    p_gen.add_argument("--host")

    p_imp = sub.add_parser("import", help="Hazır bir WAV/MP3 (veya Flow Music çıktısı) dosyasını FlowGen bankasına yükle")
    p_imp.add_argument("file", help="Ses dosyası yolu")
    p_imp.add_argument("--slot", type=int, default=1)
    p_imp.add_argument("--prompt", default="")
    p_imp.add_argument("--bpm", type=float)
    p_imp.add_argument("--key")
    p_imp.add_argument("--bars", type=int, default=4)
    p_imp.add_argument("--slice-mode", choices=["onsets", "beats", "grid"], default="onsets")
    p_imp.add_argument("--no-deploy", action="store_true")
    p_imp.add_argument("--host")

    p_sel = sub.add_parser("select", help="MPC üzerindeki FlowGen bank slotunu uzaktan değiştir (hot-swap)")
    p_sel.add_argument("slot", type=int)
    p_sel.add_argument("--no-deploy", action="store_true")
    p_sel.add_argument("--host")

    p_w = sub.add_parser("watch-drops", help="~/Music/Flow Music ve ~/Downloads için otomatik içe aktarma izleyici")
    p_w.add_argument("--slot", type=int, default=1)
    p_w.add_argument("--interval", type=float, default=2.0)
    p_w.add_argument("--no-advance", action="store_true")
    p_w.add_argument("--no-deploy", action="store_true")
    p_w.add_argument("--once", action="store_true")
    p_w.add_argument("--host")


    p_kit = sub.add_parser("drumkit", help="Prompt ile Akai MPC Drum Program (.xpm + 16 pad WAV) oluştur ve MPC'ye yükle")
    p_kit.add_argument("prompt", help="Ör: '90s boom bap dusty vinyl drum kit'")
    p_kit.add_argument("--name", default=None)
    p_kit.add_argument("--bpm", type=float)
    p_kit.add_argument("--key")
    p_kit.add_argument("--engine", choices=["auto", "gemini_lyria", "synth_flow"])
    p_kit.add_argument("--model")
    p_kit.add_argument("--no-deploy", action="store_true")
    p_kit.add_argument("--host")

    sub.add_parser("kits", help="Üretilmiş Flow Drum Kit programlarını (.xpm) listele")

    args = ap.parse_args(argv)

    if args.cmd == "drumkit":
        try:
            from . import mpc_drumkit
        except ImportError:
            import mpc_drumkit  # type: ignore
        res = mpc_drumkit.generate_drum_kit(
            prompt=args.prompt,
            kit_name=args.name,
            bpm=args.bpm,
            key=args.key,
            engine=args.engine,
            model=args.model,
            deploy=not args.no_deploy,
            host=args.host,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0
    if args.cmd == "kits":
        try:
            from . import mpc_drumkit
        except ImportError:
            import mpc_drumkit  # type: ignore
        print(json.dumps(mpc_drumkit.list_drum_kits(), indent=2, ensure_ascii=False))
        return 0
    if args.cmd == "status":
        print(json.dumps(get_flow_status(), indent=2, ensure_ascii=False))
        return 0
    if args.cmd == "banks":
        print(json.dumps(list_banks(), indent=2, ensure_ascii=False))
        return 0
    if args.cmd == "config":
        updates: Dict[str, Any] = {}
        if args.engine:
            updates["engine"] = args.engine
        if args.model:
            updates["model"] = args.model
        if args.bpm is not None:
            updates["default_bpm"] = args.bpm
        if args.key:
            updates["default_key"] = args.key
        if args.bars is not None:
            updates["default_bars"] = args.bars
        if args.slice_mode:
            updates["slice_mode"] = args.slice_mode
        if args.host:
            updates["mpc_host"] = args.host
        if updates:
            save_config(updates)
        if args.clear_api_key:
            save_gemini_api_key("")
        elif args.api_key:
            save_gemini_api_key(args.api_key)
        print(json.dumps(get_flow_status(), indent=2, ensure_ascii=False))
        return 0
    if args.cmd == "generate":
        res = generate_sample_bank(
            prompt=args.prompt,
            slot=args.slot,
            bpm=args.bpm,
            key=args.key,
            bars=args.bars,
            engine=args.engine,
            model=args.model,
            slice_mode=args.slice_mode,
            deploy=not args.no_deploy,
            host=args.host,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0
    if args.cmd == "import":
        res = package_audio_into_bank(
            source_audio=Path(args.file),
            slot=args.slot,
            prompt=args.prompt,
            engine="flow_drop",
            target_bpm=args.bpm,
            target_key=args.key,
            bars=args.bars,
            slice_mode=args.slice_mode,
            deploy=not args.no_deploy,
            host=args.host,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0
    if args.cmd == "select":
        print(json.dumps(select_bank(args.slot, deploy=not args.no_deploy, host=args.host), indent=2, ensure_ascii=False))
        return 0
    if args.cmd == "watch-drops":
        return watch_flow_drops(
            slot=args.slot,
            interval_s=args.interval,
            auto_advance_slot=not args.no_advance,
            deploy=not args.no_deploy,
            host=args.host,
            once=args.once,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(run_flow_cli())
