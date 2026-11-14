#!/usr/bin/env python3
"""Open-source, modular audio DSP toolkit for MPC Live / MPCUTER:

- Beat, BPM & transient onset detection (`librosa`, `aubio`, `soundstretch`, consensus `auto`)
- Key & Camelot harmonic detection (`libkeyfinder` native `mpc_keyfinder`, `librosa` HPSS+Chroma, `ensemble`)
- Time-stretching & pitch-shifting (`rubberband-r3`, `rubberband`, `soundstretch`, `ffmpeg`)
- Transient / beat-grid MPC pad chopping (`soundfile` / `sox`) + SSH deployment to `/media/okcomputer`.
"""

from __future__ import annotations

import argparse
import contextlib
import contextvars
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from mpc_client import MpcMap, deploy_file_to_mpc, emit_activity

BIN_DIR = HERE / "bin"
DEFAULT_KEYFINDER_BIN = BIN_DIR / "mpc_keyfinder"
DEFAULT_AUDIO_VENV_PY = HERE.parent / ".venv-audio" / "bin" / "python"
TOOL_SEARCH_DIRS = [str(BIN_DIR), "/opt/homebrew/bin", "/usr/local/bin"]

# Canonical pitch-class mappings & Camelot wheel notation
NOTE_NAMES_FLAT = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
NOTE_NAMES_SHARP = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
CAMELOT_MAJOR = ["8B", "3B", "10B", "5B", "12B", "7B", "2B", "9B", "4B", "11B", "6B", "1B"]
CAMELOT_MINOR = ["5A", "12A", "7A", "2A", "9A", "4A", "11A", "6A", "1A", "8A", "3A", "10A"]

# Krumhansl-Schmuckler & Temperley 12-bin profiles
_KS_MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
_KS_MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
_TM_MAJOR = [5.0, 2.0, 3.5, 2.0, 4.5, 4.0, 2.0, 4.5, 2.0, 3.5, 1.5, 4.0]
_TM_MINOR = [5.0, 2.0, 3.5, 4.5, 2.0, 4.0, 2.0, 4.5, 3.5, 2.0, 1.5, 4.0]


def _augmented_env() -> Dict[str, str]:
    env = dict(os.environ)
    cur = env.get("PATH", "")
    parts = [p for p in TOOL_SEARCH_DIRS + cur.split(":") if p]
    seen: List[str] = []
    for p in parts:
        if p not in seen:
            seen.append(p)
    env["PATH"] = ":".join(seen)
    return env


def _which(name: str) -> Optional[str]:
    env = _augmented_env()
    found = shutil.which(name, path=env["PATH"])
    if found:
        return found
    for d in TOOL_SEARCH_DIRS:
        p = Path(d) / name
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def _find_audio_python() -> Optional[str]:
    override = os.environ.get("MPC_AUDIO_PYTHON")
    if override and Path(override).is_file():
        return override
    if DEFAULT_AUDIO_VENV_PY.is_file():
        return str(DEFAULT_AUDIO_VENV_PY)
    return None


def detect_backends() -> Dict[str, Any]:
    """Discover all installed open-source audio DSP tools & Python MIR libraries."""
    tools = {
        "rubberband_r3": _which("rubberband-r3"),
        "rubberband": _which("rubberband"),
        "soundstretch": _which("soundstretch"),
        "aubio": _which("aubio"),
        "aubiotrack": _which("aubiotrack"),
        "aubioonset": _which("aubioonset"),
        "keyfinder_cli": _which("mpc_keyfinder") or (str(DEFAULT_KEYFINDER_BIN) if DEFAULT_KEYFINDER_BIN.is_file() else None),
        "sox": _which("sox"),
        "ffmpeg": _which("ffmpeg"),
        "ffprobe": _which("ffprobe"),
        "audio_python": _find_audio_python(),
    }
    py_mods: Dict[str, str] = {}
    ap = tools["audio_python"]
    if ap:
        probe = (
            "import json, importlib.metadata as md; "
            "mods = ['librosa','soundfile','scipy','numpy','pyrubberband','numba']; "
            "out = {}; "
            "\nfor m in mods:\n"
            "  try: out[m] = md.version(m)\n  except Exception: pass\n"
            "print(json.dumps(out))"
        )
        proc = subprocess.run([ap, "-c", probe], capture_output=True, text=True, timeout=8)
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                py_mods = json.loads(proc.stdout.strip())
            except json.JSONDecodeError:
                pass
    return {
        "tools": tools,
        "python_mir": py_mods,
        "beat_backends": sorted(BEAT_BACKENDS.keys()),
        "key_backends": sorted(KEY_BACKENDS.keys()),
        "stretch_backends": sorted(STRETCH_BACKENDS.keys()),
    }


# ---------------------------------------------------------------------------
# Key / Camelot theory helpers
# ---------------------------------------------------------------------------

def _build_key_aliases() -> Dict[str, Tuple[int, str]]:
    table: Dict[str, Tuple[int, str]] = {}
    enharmonics = [
        ("C",), ("DB", "C#"), ("D",), ("EB", "D#"), ("E",), ("F",),
        ("GB", "F#"), ("G",), ("AB", "G#"), ("A",), ("BB", "A#"), ("B",),
    ]
    for pc, names in enumerate(enharmonics):
        for n in names:
            for maj_sfx in ("", "MAJ", "MAJOR", "_MAJOR"):
                table[f"{n}{maj_sfx}"] = (pc, "major")
            for min_sfx in ("M", "MIN", "MINOR", "_MINOR"):
                table[f"{n}{min_sfx}"] = (pc, "minor")
        table[CAMELOT_MAJOR[pc]] = (pc, "major")
        table[CAMELOT_MINOR[pc]] = (pc, "minor")
    return table


_KEY_ALIASES = _build_key_aliases()


def format_key_meta(pc: int, mode: str) -> Dict[str, Any]:
    pc = int(pc) % 12
    mode = "minor" if mode.lower().startswith("min") else "major"
    root = NOTE_NAMES_FLAT[pc]
    if mode == "major":
        camelot = CAMELOT_MAJOR[pc]
        rel_pc = (pc - 3) % 12
        rel_key = f"{NOTE_NAMES_FLAT[rel_pc]} minor"
        rel_cam = CAMELOT_MINOR[rel_pc]
    else:
        camelot = CAMELOT_MINOR[pc]
        rel_pc = (pc + 3) % 12
        rel_key = f"{NOTE_NAMES_FLAT[rel_pc]} major"
        rel_cam = CAMELOT_MAJOR[rel_pc]
    return {
        "key": f"{root} {mode}",
        "short": f"{root}{'m' if mode == 'minor' else ''}",
        "pitch_class": pc,
        "mode": mode,
        "camelot": camelot,
        "relative_key": rel_key,
        "relative_camelot": rel_cam,
    }


def parse_key_label(label: str) -> Dict[str, Any]:
    raw = str(label or "").strip()
    norm = re.sub(r"[\s\-]+", "", raw.upper()).replace("_FLAT_", "B").replace("_SHARP_", "#")
    norm = norm.replace("FLAT", "B").replace("SHARP", "#")
    if norm in _KEY_ALIASES:
        pc, mode = _KEY_ALIASES[norm]
        return format_key_meta(pc, mode)
    # Try underscores directly (e.g. A_FLAT_MINOR -> ABMINOR)
    compact = norm.replace("_", "")
    if compact in _KEY_ALIASES:
        pc, mode = _KEY_ALIASES[compact]
        return format_key_meta(pc, mode)
    raise ValueError(f"Geçersiz ton (key) biçimi: '{label}' (ör. 'Fm', 'F minor', '4A', 'C#m', 'Ab major')")


def semitones_between_keys(source_key: str, target_key: str) -> int:
    """Compute shortest pitch shift [-6..+6] semitones to map source_key -> target_key."""
    s = parse_key_label(source_key)
    t = parse_key_label(target_key)
    # If mode differs (major vs minor), align via relative major/minor first so harmonic feel matches!
    t_pc = t["pitch_class"]
    if s["mode"] != t["mode"]:
        t_pc = (t_pc + (3 if s["mode"] == "major" else -3)) % 12
    diff = (t_pc - s["pitch_class"]) % 12
    if diff > 6:
        diff -= 12
    return int(diff)


# ---------------------------------------------------------------------------
# Audio normalization & external MIR worker bridge
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _wav_input(audio_path: Path) -> Iterator[Path]:
    src = Path(audio_path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"Ses dosyası bulunamadı: {src}")
    if src.suffix.lower() == ".wav":
        yield src
        return

    ffmpeg = _which("ffmpeg")
    sox = _which("sox")
    with tempfile.TemporaryDirectory(prefix="mpc_audio_wav_") as td:
        wav_path = Path(td) / f"{src.stem}.wav"
        if ffmpeg:
            subprocess.run(
                [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-acodec", "pcm_s24le", str(wav_path)],
                env=_augmented_env(),
                check=True,
                capture_output=True,
                text=True,
            )
        elif sox:
            subprocess.run(
                [sox, str(src), "-b", "24", str(wav_path)],
                env=_augmented_env(),
                check=True,
                capture_output=True,
                text=True,
            )
        else:
            raise RuntimeError(f"{src.suffix} dosyasını WAV'a dönüştürmek için ffmpeg veya sox bulunamadı")
        yield wav_path


def _probe_audio_meta(wav_path: Path) -> Dict[str, Any]:
    ffprobe = _which("ffprobe")
    if ffprobe:
        proc = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(wav_path)],
            env=_augmented_env(),
            capture_output=True,
            text=True,
            timeout=8,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            data = json.loads(proc.stdout)
            streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
            st = streams[0] if streams else {}
            fmt = data.get("format", {})
            dur = float(fmt.get("duration") or st.get("duration") or 0.0)
            return {
                "sample_rate": int(st.get("sample_rate") or 0),
                "channels": int(st.get("channels") or 0),
                "duration_s": round(dur, 4),
                "codec": st.get("codec_name"),
                "bits_per_sample": int(st.get("bits_per_raw_sample") or st.get("bits_per_sample") or 0),
            }
    try:
        with wave.open(str(wav_path), "rb") as wf:
            sr = wf.getframerate()
            ch = wf.getnchannels()
            sw = wf.getsampwidth()
            frames = wf.getnframes()
            dur = (frames / float(sr)) if sr > 0 else 0.0
            return {
                "sample_rate": sr,
                "channels": ch,
                "duration_s": round(dur, 4),
                "codec": f"pcm_s{sw * 8}le",
                "bits_per_sample": sw * 8,
            }
    except Exception:
        pass
    return {"sample_rate": None, "channels": None, "duration_s": None}


_MIR_PREFETCH: contextvars.ContextVar[Optional[Dict[Tuple[str, str], Dict[str, Any]]]] = contextvars.ContextVar(
    "mpc_mir_prefetch",
    default=None,
)


def _call_librosa_worker(payload: Dict[str, Any], timeout_s: float = 90.0) -> Dict[str, Any]:
    prefetch = _MIR_PREFETCH.get()
    op = payload.get("op")
    wav_key = payload.get("wav_path")
    if prefetch is not None and op in ("beat", "key") and wav_key:
        cached = prefetch.get((str(wav_key), str(op)))
        if cached is not None:
            return cached

    # Run directly in-process if current Python already has librosa + soundfile!
    try:
        import librosa  # type: ignore  # noqa: F401
        return _execute_mir_payload(payload)
    except ImportError:
        pass

    py_bin = _find_audio_python()
    if not py_bin:
        raise RuntimeError(
            "librosa MIR ortamı bulunamadı (.venv-audio/bin/python). "
            "MPC_AUDIO_PYTHON env ayarlayın veya .venv-audio kurun."
        )
    proc = subprocess.run(
        [py_bin, str(Path(__file__).resolve()), "--_worker-mir"],
        input=json.dumps(payload),
        env=_augmented_env(),
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"MIR worker hatası: {proc.stderr.strip() or proc.stdout.strip()}")
    return json.loads(proc.stdout.strip())


# ---------------------------------------------------------------------------
# In-process MIR algorithms (executed inside .venv-audio)
# ---------------------------------------------------------------------------

def _pearson_corr(x: Any, y: Any) -> float:
    import numpy as np  # type: ignore

    xm = x - np.mean(x)
    ym = y - np.mean(y)
    denom = float(np.linalg.norm(xm) * np.linalg.norm(ym))
    if denom <= 1e-12:
        return 0.0
    return float(np.dot(xm, ym) / denom)


def _mir_beat(wav_path: Path, min_bpm: float, max_bpm: float) -> Dict[str, Any]:
    import librosa  # type: ignore
    import numpy as np  # type: ignore

    y, sr = librosa.load(str(wav_path), sr=22050, mono=True)
    dur = float(len(y) / sr)
    if dur < 0.25 or float(np.max(np.abs(y))) < 1e-6:
        return {"backend": "librosa", "bpm": 0.0, "beats_s": [], "onsets_s": [], "duration_s": round(dur, 4), "confidence": 0.0}

    onset_env = librosa.onset.onset_strength(y=y, sr=sr)
    start_bpm = min(max(100.0, min_bpm), max_bpm)
    tempo, beat_frames = librosa.beat.beat_track(onset_envelope=onset_env, sr=sr, start_bpm=start_bpm, trim=False)
    bpm = float(np.atleast_1d(tempo)[0])
    beat_times = [round(float(t), 4) for t in librosa.frames_to_time(beat_frames, sr=sr)]

    # Dynamic tempogram peaks for alternative / octave BPMs
    tempi = librosa.feature.tempo(onset_envelope=onset_env, sr=sr, aggregate=None)
    hist_med = float(np.median(tempi)) if tempi.size else bpm
    alt = []
    for cand in (bpm * 0.5, bpm * 2.0, hist_med):
        if min_bpm <= cand <= max_bpm and abs(cand - bpm) > 2.5:
            alt.append(round(float(cand), 2))

    onset_frames = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sr, backtrack=True)
    onset_times = [round(float(t), 4) for t in librosa.frames_to_time(onset_frames, sr=sr)]

    stability = 0.45
    if len(beat_times) >= 3:
        ibis = np.diff(np.array(beat_times))
        med = float(np.median(ibis))
        if med > 1e-6:
            cv = float(np.std(ibis) / med)
            stability = round(max(0.05, min(0.99, 1.0 - cv)), 3)

    return {
        "backend": "librosa",
        "bpm": round(bpm, 2),
        "alt_bpms": sorted(set(alt)),
        "beat_count": len(beat_times),
        "beats_s": beat_times,
        "onset_count": len(onset_times),
        "onsets_s": onset_times,
        "duration_s": round(dur, 4),
        "confidence": stability,
    }


def _mir_key(wav_path: Path) -> Dict[str, Any]:
    import librosa  # type: ignore
    import numpy as np  # type: ignore

    y, sr = librosa.load(str(wav_path), sr=22050, mono=True)
    if len(y) == 0 or float(np.max(np.abs(y))) < 1e-6:
        return {"backend": "librosa", "key": "Silence", "camelot": "--", "confidence": 0.0, "top_3": []}

    # Strip percussive transients before chroma so kicks/snares don't bias tonality
    y_harm, _ = librosa.effects.hpss(y, margin=2.0)
    if float(np.max(np.abs(y_harm))) < 1e-5:
        y_harm = y

    tuning = float(librosa.estimate_tuning(y=y_harm, sr=sr))
    chroma = librosa.feature.chroma_cqt(y=y_harm, sr=sr, tuning=tuning, bins_per_octave=36)
    chroma_vec = np.mean(chroma, axis=1)

    ks_maj = np.array(_KS_MAJOR, dtype=float)
    ks_min = np.array(_KS_MINOR, dtype=float)
    tm_maj = np.array(_TM_MAJOR, dtype=float)
    tm_min = np.array(_TM_MINOR, dtype=float)

    candidates: List[Dict[str, Any]] = []
    for pc in range(12):
        # Rotate chroma so index 0 aligns with tonic pitch class `pc`
        rot = np.roll(chroma_vec, -pc)
        maj_score = 0.5 * (_pearson_corr(rot, ks_maj) + _pearson_corr(rot, tm_maj))
        min_score = 0.5 * (_pearson_corr(rot, ks_min) + _pearson_corr(rot, tm_min))
        for mode, sc in (("major", maj_score), ("minor", min_score)):
            meta = format_key_meta(pc, mode)
            candidates.append({**meta, "score": round(float(sc), 4)})

    candidates.sort(key=lambda c: c["score"], reverse=True)
    best = candidates[0]
    margin = max(0.0, best["score"] - candidates[1]["score"])
    conf = round(max(0.1, min(0.99, 0.5 + best["score"] * 0.35 + margin * 1.4)), 3)
    return {
        "backend": "librosa",
        **{k: best[k] for k in ("key", "short", "camelot", "relative_key", "relative_camelot", "pitch_class", "mode")},
        "tuning_cents": round(tuning * 100.0, 1),
        "confidence": conf,
        "top_3": candidates[:3],
    }


def _mir_chop(payload: Dict[str, Any]) -> Dict[str, Any]:
    import numpy as np  # type: ignore
    import soundfile as sf  # type: ignore

    wav_path = Path(payload["wav_path"])
    out_dir = Path(payload["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    cuts = [float(x) for x in payload["cuts_s"]]
    prefix = str(payload.get("prefix") or wav_path.stem)
    fade_ms = float(payload.get("fade_ms", 3.0))
    subtype = "PCM_24" if int(payload.get("bit_depth", 16)) >= 24 else "PCM_16"

    data, sr = sf.read(str(wav_path), always_2d=True)
    total_frames = len(data)
    dur_total = total_frames / float(sr)
    bounds = [t for t in cuts if 0.0 <= t < dur_total - 0.015]
    if not bounds or bounds[0] > 0.02:
        bounds.insert(0, 0.0)
    max_slices = int(payload.get("max_slices") or 0)
    if max_slices > 0 and len(bounds) > max_slices:
        bounds = bounds[:max_slices]
    bounds.append(dur_total)

    fade_n = max(0, int(sr * (fade_ms / 1000.0)))
    slices: List[Dict[str, Any]] = []
    for idx in range(len(bounds) - 1):
        s_t, e_t = bounds[idx], bounds[idx + 1]
        if e_t - s_t < 0.02:
            continue
        s_f = max(0, min(total_frames, int(round(s_t * sr))))
        e_f = max(s_f + 1, min(total_frames, int(round(e_t * sr))))
        seg = np.array(data[s_f:e_f], dtype=np.float64, copy=True)
        fn = min(fade_n, len(seg) // 3)
        if fn > 1:
            ramp_in = np.linspace(0.0, 1.0, fn)[:, None]
            ramp_out = np.linspace(1.0, 0.0, fn)[:, None]
            seg[:fn] *= ramp_in
            seg[-fn:] *= ramp_out
        pad_num = len(slices) + 1
        fname = f"{prefix}_pad{pad_num:02d}.wav"
        out_file = out_dir / fname
        sf.write(str(out_file), seg, sr, subtype=subtype)
        slices.append({
            "pad": pad_num,
            "file": str(out_file),
            "name": fname,
            "start_s": round(s_t, 4),
            "end_s": round(e_t, 4),
            "duration_s": round(e_t - s_t, 4),
        })

    return {"sample_rate": sr, "count": len(slices), "slices": slices}


def _mir_analyze_combined(wav_path: Path, min_bpm: float, max_bpm: float) -> Dict[str, Any]:
    """Run librosa beat + key analysis in a single worker process and cached audio load."""
    import librosa  # type: ignore

    orig_load = librosa.load
    audio_cache: Dict[str, Tuple[Any, Any]] = {}

    def _cached_load(path: Any, sr: Any = 22050, mono: bool = True, **kwargs: Any) -> Tuple[Any, Any]:
        if kwargs.get("offset", 0.0) or kwargs.get("duration") is not None or not mono:
            return orig_load(path, sr=sr, mono=mono, **kwargs)
        key_p = str(path)
        if key_p not in audio_cache:
            y0, sr0 = orig_load(path, sr=None, mono=True)
            audio_cache[key_p] = (y0, sr0)
        y0, sr0 = audio_cache[key_p]
        if sr is None or sr == sr0:
            return y0.copy(), sr0
        return librosa.resample(y0, orig_sr=sr0, target_sr=sr), sr

    librosa.load = _cached_load
    try:
        out: Dict[str, Any] = {}
        try:
            out["beat"] = _mir_beat(wav_path, min_bpm, max_bpm)
        except Exception as exc:
            out["beat_error"] = str(exc)
        try:
            out["key"] = _mir_key(wav_path)
        except Exception as exc:
            out["key_error"] = str(exc)
        return out
    finally:
        librosa.load = orig_load


def _execute_mir_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    op = payload.get("op")
    if op == "beat":
        return _mir_beat(Path(payload["wav_path"]), float(payload.get("min_bpm", 55.0)), float(payload.get("max_bpm", 175.0)))
    if op == "key":
        return _mir_key(Path(payload["wav_path"]))
    if op == "analyze":
        return _mir_analyze_combined(
            Path(payload["wav_path"]),
            float(payload.get("min_bpm", 55.0)),
            float(payload.get("max_bpm", 175.0)),
        )
    if op == "chop":
        return _mir_chop(payload)
    raise ValueError(f"Bilinmeyen MIR komutu: {op}")


# ---------------------------------------------------------------------------
# Beat Detection Backends
# ---------------------------------------------------------------------------

def _beat_aubio(wav_path: Path, min_bpm: float, max_bpm: float) -> Dict[str, Any]:
    aubio_bin = _which("aubio")
    aubiotrack = _which("aubiotrack")
    aubioonset = _which("aubioonset")
    if not (aubio_bin or aubiotrack):
        raise RuntimeError("aubio / aubiotrack yüklü değil")

    env = _augmented_env()
    bpm = 0.0
    uncertain = False
    if aubio_bin:
        proc = subprocess.run([aubio_bin, "tempo", "-i", str(wav_path)], env=env, capture_output=True, text=True, timeout=20)
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*bpm", proc.stdout + "\n" + proc.stderr, re.IGNORECASE)
        if m:
            bpm = float(m.group(1))
        uncertain = "uncertain" in (proc.stdout + proc.stderr).lower()

    beats_s: List[float] = []
    if aubiotrack:
        tp = subprocess.run([aubiotrack, "-i", str(wav_path)], env=env, capture_output=True, text=True, timeout=20)
        beats_s = [round(float(x), 4) for x in re.findall(r"([0-9]+(?:\.[0-9]+)?)", tp.stdout)]

    onsets_s: List[float] = []
    if aubioonset:
        op = subprocess.run([aubioonset, "-i", str(wav_path)], env=env, capture_output=True, text=True, timeout=20)
        onsets_s = [round(float(x), 4) for x in re.findall(r"([0-9]+(?:\.[0-9]+)?)", op.stdout)]

    if bpm <= 0.0 and len(beats_s) >= 2:
        diffs = [beats_s[i + 1] - beats_s[i] for i in range(len(beats_s) - 1) if beats_s[i + 1] > beats_s[i]]
        if diffs:
            diffs.sort()
            med = diffs[len(diffs) // 2]
            bpm = 60.0 / med if med > 1e-4 else 0.0

    # Fold octave if outside target studio band while non-zero
    alt = []
    raw_bpm = bpm
    while bpm > max_bpm and bpm > 0:
        alt.append(round(bpm, 2))
        bpm *= 0.5
    while 0 < bpm < min_bpm:
        alt.append(round(bpm, 2))
        bpm *= 2.0

    return {
        "backend": "aubio",
        "bpm": round(bpm, 2),
        "raw_bpm": round(raw_bpm, 2),
        "alt_bpms": sorted(set(alt)),
        "beat_count": len(beats_s),
        "beats_s": beats_s,
        "onset_count": len(onsets_s),
        "onsets_s": onsets_s,
        "confidence": 0.55 if uncertain else 0.8,
    }


def _beat_librosa(wav_path: Path, min_bpm: float, max_bpm: float) -> Dict[str, Any]:
    return _call_librosa_worker({"op": "beat", "wav_path": str(wav_path), "min_bpm": min_bpm, "max_bpm": max_bpm})


def _beat_soundstretch(wav_path: Path, min_bpm: float, max_bpm: float) -> Dict[str, Any]:
    ss = _which("soundstretch")
    if not ss:
        raise RuntimeError("soundstretch yüklü değil")
    proc = subprocess.run([ss, str(wav_path), "-bpm"], env=_augmented_env(), capture_output=True, text=True, timeout=20)
    combined = proc.stdout + "\n" + proc.stderr
    m = re.search(r"Detected BPM rate\s+([0-9]+(?:\.[0-9]+)?)", combined, re.IGNORECASE)
    bpm = float(m.group(1)) if m else 0.0
    return {
        "backend": "soundstretch",
        "bpm": round(bpm, 2),
        "alt_bpms": [],
        "beat_count": 0,
        "beats_s": [],
        "onset_count": 0,
        "onsets_s": [],
        "confidence": 0.6 if bpm > 0 else 0.0,
    }


def _beat_auto(wav_path: Path, min_bpm: float, max_bpm: float) -> Dict[str, Any]:
    results: Dict[str, Dict[str, Any]] = {}
    for name, fn in (("librosa", _beat_librosa), ("aubio", _beat_aubio), ("soundstretch", _beat_soundstretch)):
        try:
            r = fn(wav_path, min_bpm, max_bpm)
            if r.get("bpm", 0.0) > 0.0:
                results[name] = r
        except Exception as exc:
            results[name] = {"error": str(exc)}

    lib_res = results.get("librosa", {})
    aub_res = results.get("aubio", {})
    lib_bpm = float(lib_res.get("bpm") or 0.0)
    aub_bpm = float(aub_res.get("bpm") or 0.0)

    alt_bpms: set[float] = set()
    for r in (lib_res, aub_res):
        for b in r.get("alt_bpms", []):
            alt_bpms.add(float(b))

    if lib_bpm > 0 and aub_bpm > 0:
        # Check direct or octave match within 2.5%
        matched_aub = aub_bpm
        for mult in (1.0, 0.5, 2.0):
            cand = aub_bpm * mult
            if abs(cand - lib_bpm) / max(1e-6, lib_bpm) <= 0.025:
                matched_aub = cand
                break
        if abs(matched_aub - lib_bpm) / max(1e-6, lib_bpm) <= 0.025:
            bpm = round(0.6 * lib_bpm + 0.4 * matched_aub, 2)
            conf = round(min(0.99, max(float(lib_res.get("confidence", 0.7)), 0.85)), 3)
        else:
            bpm = round(lib_bpm, 2)
            alt_bpms.add(round(aub_bpm, 2))
            conf = float(lib_res.get("confidence", 0.65))
    elif lib_bpm > 0:
        bpm = round(lib_bpm, 2)
        conf = float(lib_res.get("confidence", 0.65))
    elif aub_bpm > 0:
        bpm = round(aub_bpm, 2)
        conf = float(aub_res.get("confidence", 0.6))
    else:
        ss_res = results.get("soundstretch", {})
        bpm = round(float(ss_res.get("bpm") or 0.0), 2)
        conf = float(ss_res.get("confidence", 0.0))

    if bpm > 0:
        if min_bpm <= bpm * 0.5 <= max_bpm:
            alt_bpms.add(round(bpm * 0.5, 2))
        if min_bpm <= bpm * 2.0 <= max_bpm:
            alt_bpms.add(round(bpm * 2.0, 2))
        alt_bpms.discard(bpm)

    beats_s = lib_res.get("beats_s") or aub_res.get("beats_s") or []
    onsets_s = lib_res.get("onsets_s") or aub_res.get("onsets_s") or []
    return {
        "backend": "auto",
        "bpm": bpm,
        "alt_bpms": sorted(alt_bpms),
        "beat_count": len(beats_s),
        "beats_s": beats_s,
        "onset_count": len(onsets_s),
        "onsets_s": onsets_s,
        "confidence": conf,
        "engines": {k: {ek: ev for ek, ev in v.items() if ek not in ("beats_s", "onsets_s")} for k, v in results.items()},
    }


BEAT_BACKENDS: Dict[str, Callable[[Path, float, float], Dict[str, Any]]] = {
    "auto": _beat_auto,
    "librosa": _beat_librosa,
    "aubio": _beat_aubio,
    "soundstretch": _beat_soundstretch,
}


def detect_beat(
    audio_path: Path,
    backend: str = "auto",
    min_bpm: float = 55.0,
    max_bpm: float = 175.0,
) -> Dict[str, Any]:
    fn = BEAT_BACKENDS.get(backend)
    if fn is None:
        raise ValueError(f"Desteklenmeyen beat backend '{backend}'. Seçenekler: {sorted(BEAT_BACKENDS)}")
    with _wav_input(Path(audio_path)) as wav:
        res = fn(wav, min_bpm, max_bpm)
    emit_activity("audio", "beat", f"{Path(audio_path).name} -> {res.get('bpm')} BPM ({backend})")
    return res


# ---------------------------------------------------------------------------
# Key Detection Backends
# ---------------------------------------------------------------------------

def _key_keyfinder(wav_path: Path) -> Dict[str, Any]:
    kf = _which("mpc_keyfinder") or (str(DEFAULT_KEYFINDER_BIN) if DEFAULT_KEYFINDER_BIN.is_file() else None)
    if not kf:
        raise RuntimeError("mpc_keyfinder binary bulunamadı")
    proc = subprocess.run([kf, str(wav_path)], env=_augmented_env(), capture_output=True, text=True, timeout=20, check=True)
    raw = json.loads(proc.stdout.strip())
    if raw.get("key") == "Silence":
        return {"backend": "keyfinder", "key": "Silence", "camelot": "--", "confidence": 0.0}
    meta = parse_key_label(raw["key"])
    return {"backend": "keyfinder", **meta, "confidence": 0.8}


def _key_librosa(wav_path: Path) -> Dict[str, Any]:
    return _call_librosa_worker({"op": "key", "wav_path": str(wav_path)})


def _key_ensemble(wav_path: Path) -> Dict[str, Any]:
    kf_res: Optional[Dict[str, Any]] = None
    lib_res: Optional[Dict[str, Any]] = None
    errors: Dict[str, str] = {}
    try:
        kf_res = _key_keyfinder(wav_path)
    except Exception as exc:
        errors["keyfinder"] = str(exc)
    try:
        lib_res = _key_librosa(wav_path)
    except Exception as exc:
        errors["librosa"] = str(exc)

    if kf_res and kf_res.get("key") != "Silence" and lib_res and lib_res.get("key") != "Silence":
        agreed = kf_res["key"] == lib_res["key"]
        relative = kf_res["key"] == lib_res.get("relative_key")
        # Prefer librosa if tuning-corrected confidence is strong, boost when agreed!
        primary = lib_res if (lib_res.get("confidence", 0.0) >= 0.7 or agreed) else kf_res
        conf = float(primary.get("confidence", 0.75))
        if agreed:
            conf = min(0.99, round(conf + 0.15, 3))
        elif relative:
            conf = min(0.95, round(conf + 0.08, 3))
        return {
            "backend": "ensemble",
            "key": primary["key"],
            "short": primary.get("short"),
            "camelot": primary.get("camelot"),
            "relative_key": primary.get("relative_key"),
            "relative_camelot": primary.get("relative_camelot"),
            "tuning_cents": lib_res.get("tuning_cents"),
            "confidence": round(conf, 3),
            "agreed": agreed,
            "relative_match": relative,
            "engines": {
                "keyfinder": {"key": kf_res["key"], "camelot": kf_res["camelot"]},
                "librosa": {"key": lib_res["key"], "camelot": lib_res["camelot"], "confidence": lib_res["confidence"]},
            },
            **({"top_3": lib_res.get("top_3")} if "top_3" in lib_res else {}),
        }

    chosen = lib_res or kf_res
    if chosen is None:
        raise RuntimeError(f"Key tespiti başarısız: {errors}")
    return {"backend": "ensemble", **chosen, "agreed": False, **({"errors": errors} if errors else {})}


KEY_BACKENDS: Dict[str, Callable[[Path], Dict[str, Any]]] = {
    "ensemble": _key_ensemble,
    "keyfinder": _key_keyfinder,
    "librosa": _key_librosa,
}


def detect_key(audio_path: Path, backend: str = "ensemble") -> Dict[str, Any]:
    fn = KEY_BACKENDS.get(backend)
    if fn is None:
        raise ValueError(f"Desteklenmeyen key backend '{backend}'. Seçenekler: {sorted(KEY_BACKENDS)}")
    with _wav_input(Path(audio_path)) as wav:
        res = fn(wav)
    emit_activity("audio", "key", f"{Path(audio_path).name} -> {res.get('key')} ({res.get('camelot')})")
    return res


def analyze_audio(
    audio_path: Path,
    beat_backend: str = "auto",
    key_backend: str = "ensemble",
    min_bpm: float = 55.0,
    max_bpm: float = 175.0,
) -> Dict[str, Any]:
    src = Path(audio_path).expanduser().resolve()
    with _wav_input(src) as wav:
        meta = _probe_audio_meta(wav)
        prefetch_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
        if beat_backend in ("auto", "librosa") and key_backend in ("ensemble", "librosa"):
            try:
                combined = _call_librosa_worker({
                    "op": "analyze",
                    "wav_path": str(wav),
                    "min_bpm": min_bpm,
                    "max_bpm": max_bpm,
                })
                if isinstance(combined.get("beat"), dict):
                    prefetch_map[(str(wav), "beat")] = combined["beat"]
                if isinstance(combined.get("key"), dict):
                    prefetch_map[(str(wav), "key")] = combined["key"]
            except Exception:
                pass
        token = _MIR_PREFETCH.set(prefetch_map) if prefetch_map else None
        try:
            beat = BEAT_BACKENDS[beat_backend](wav, min_bpm, max_bpm)
            key = KEY_BACKENDS[key_backend](wav)
        finally:
            if token is not None:
                _MIR_PREFETCH.reset(token)

    dur = float(meta.get("duration_s") or 0.0)
    bpm = float(beat.get("bpm") or 0.0)
    est_bars = None
    if dur > 0 and bpm > 0:
        raw_bars = dur / (4.0 * 60.0 / bpm)
        est_bars = round(raw_bars, 2)

    out = {
        "file": str(src),
        "meta": meta,
        "bpm": beat.get("bpm"),
        "alt_bpms": beat.get("alt_bpms", []),
        "estimated_bars_4_4": est_bars,
        "beat_count": beat.get("beat_count"),
        "beats_s": beat.get("beats_s", []),
        "onset_count": beat.get("onset_count"),
        "onsets_s": beat.get("onsets_s", []),
        "beat_confidence": beat.get("confidence"),
        "key": key.get("key"),
        "short_key": key.get("short"),
        "camelot": key.get("camelot"),
        "relative_key": key.get("relative_key"),
        "relative_camelot": key.get("relative_camelot"),
        "tuning_cents": key.get("tuning_cents"),
        "key_confidence": key.get("confidence"),
        "key_agreed": key.get("agreed"),
        "beat_engines": beat.get("engines"),
        "key_engines": key.get("engines"),
    }
    emit_activity("audio", "analyze", f"{src.name} -> {out['bpm']} BPM | {out['key']} ({out['camelot']})")
    return out


# ---------------------------------------------------------------------------
# Time Stretching & Pitch Shifting Backends
# ---------------------------------------------------------------------------

def _stretch_rubberband(
    wav_in: Path,
    wav_out: Path,
    tempo_mult: float,
    pitch_semitones: float,
    formant: bool,
    crisp: Optional[int],
    use_r3: bool,
) -> Dict[str, Any]:
    rb = _which("rubberband-r3") if use_r3 else _which("rubberband")
    if not rb:
        rb = _which("rubberband")
    if not rb:
        raise RuntimeError("rubberband CLI yüklü değil")

    cmd = [rb, "-q", "-3" if use_r3 else "-2"]
    if formant:
        cmd.append("-F")
    if crisp is not None and not use_r3:
        cmd.extend(["-c", str(max(0, min(6, int(crisp))))])
    if abs(tempo_mult - 1.0) > 1e-6:
        cmd.extend(["-T", f"{tempo_mult:.6f}"])
    if abs(pitch_semitones) > 1e-6:
        cmd.extend(["-p", f"{pitch_semitones:.4f}"])
    # rubberband requires at least one -t/-T/-p option
    if "-T" not in cmd and "-p" not in cmd:
        cmd.extend(["-T", "1.0"])
    cmd.extend([str(wav_in), str(wav_out)])
    subprocess.run(cmd, env=_augmented_env(), check=True, capture_output=True, text=True, timeout=180)
    return {"engine": Path(rb).name, "mode": "R3" if use_r3 else "R2"}


def _stretch_soundstretch(
    wav_in: Path,
    wav_out: Path,
    tempo_mult: float,
    pitch_semitones: float,
    formant: bool,
    crisp: Optional[int],
) -> Dict[str, Any]:
    del formant, crisp
    ss = _which("soundstretch")
    if not ss:
        raise RuntimeError("soundstretch yüklü değil")
    tempo_pct = (tempo_mult - 1.0) * 100.0
    cmd = [ss, str(wav_in), str(wav_out)]
    if abs(tempo_pct) > 1e-4:
        cmd.append(f"-tempo={tempo_pct:+.4f}")
    if abs(pitch_semitones) > 1e-4:
        cmd.append(f"-pitch={pitch_semitones:+.4f}")
    subprocess.run(cmd, env=_augmented_env(), check=True, capture_output=True, text=True, timeout=180)
    return {"engine": "soundstretch", "tempo_pct": round(tempo_pct, 3)}


def _build_atempo_chain(mult: float) -> str:
    """Decompose arbitrary tempo multiplier into chained ffmpeg atempo filters in [0.5, 2.0]."""
    factors: List[float] = []
    m = max(0.05, float(mult))
    while m > 2.0:
        factors.append(2.0)
        m /= 2.0
    while m < 0.5:
        factors.append(0.5)
        m /= 0.5
    factors.append(m)
    return ",".join(f"atempo={f:.6f}" for f in factors)


def _stretch_ffmpeg(
    wav_in: Path,
    wav_out: Path,
    tempo_mult: float,
    pitch_semitones: float,
    formant: bool,
    crisp: Optional[int],
) -> Dict[str, Any]:
    del formant, crisp
    ff = _which("ffmpeg")
    if not ff:
        raise RuntimeError("ffmpeg yüklü değil")
    meta = _probe_audio_meta(wav_in)
    sr = int(meta.get("sample_rate") or 44100)
    pitch_ratio = 2.0 ** (pitch_semitones / 12.0)
    filters: List[str] = []
    if abs(pitch_semitones) > 1e-4:
        filters.append(f"asetrate={sr * pitch_ratio:.2f}")
        filters.append(f"aresample={sr}")
        # Compensate speed change caused by asetrate
        tempo_mult = tempo_mult / pitch_ratio
    if abs(tempo_mult - 1.0) > 1e-5:
        filters.append(_build_atempo_chain(tempo_mult))
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav_in)]
    if filters:
        cmd.extend(["-af", ",".join(filters)])
    cmd.extend(["-acodec", "pcm_s24le", str(wav_out)])
    subprocess.run(cmd, env=_augmented_env(), check=True, capture_output=True, text=True, timeout=180)
    return {"engine": "ffmpeg"}


STRETCH_BACKENDS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "rubberband-r3": lambda i, o, t, p, f, c: _stretch_rubberband(i, o, t, p, f, c, use_r3=True),
    "rubberband": lambda i, o, t, p, f, c: _stretch_rubberband(i, o, t, p, f, c, use_r3=False),
    "soundstretch": _stretch_soundstretch,
    "ffmpeg": _stretch_ffmpeg,
}


def _finalize_mpc_wav(src_wav: Path, out_wav: Path, sample_rate: Optional[int], bit_depth: int) -> None:
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    if sample_rate is None and bit_depth == 24:
        shutil.copy2(src_wav, out_wav)
        return
    ffmpeg = _which("ffmpeg")
    sox = _which("sox")
    codec = "pcm_s24le" if bit_depth >= 24 else "pcm_s16le"
    if ffmpeg:
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src_wav)]
        if sample_rate:
            cmd.extend(["-ar", str(int(sample_rate))])
        cmd.extend(["-acodec", codec, str(out_wav)])
        subprocess.run(cmd, env=_augmented_env(), check=True, capture_output=True, text=True)
        return
    if sox:
        cmd = [sox, str(src_wav), "-b", "24" if bit_depth >= 24 else "16", str(out_wav)]
        if sample_rate:
            cmd.extend(["rate", str(int(sample_rate))])
        subprocess.run(cmd, env=_augmented_env(), check=True, capture_output=True, text=True)
        return
    shutil.copy2(src_wav, out_wav)


def stretch_audio(
    input_path: Path,
    output_path: Optional[Path] = None,
    backend: str = "rubberband-r3",
    tempo_mult: Optional[float] = None,
    source_bpm: Optional[float] = None,
    target_bpm: Optional[float] = None,
    pitch_semitones: Optional[float] = None,
    source_key: Optional[str] = None,
    target_key: Optional[str] = None,
    formant: bool = False,
    crisp: Optional[int] = None,
    sample_rate: Optional[int] = 44100,
    bit_depth: int = 16,
    deploy: bool = False,
    host: Optional[str] = None,
    remote_dir: str = "/media/okcomputer/MPCUTER_Audio",
) -> Dict[str, Any]:
    if backend not in STRETCH_BACKENDS:
        raise ValueError(f"Desteklenmeyen stretch backend '{backend}'. Seçenekler: {sorted(STRETCH_BACKENDS)}")

    src = Path(input_path).expanduser().resolve()
    if output_path is None:
        tag = f"_{int(round(target_bpm))}bpm" if target_bpm else "_stretched"
        output_path = src.with_name(f"{src.stem}{tag}.wav")
    out_wav = Path(output_path).expanduser().resolve()

    resolved_src_bpm = source_bpm
    if target_bpm is not None and tempo_mult is None:
        if resolved_src_bpm is None:
            resolved_src_bpm = float(detect_beat(src)["bpm"])
        if resolved_src_bpm <= 0:
            raise ValueError("Kaynak BPM tespit edilemedi; lütfen --source-bpm belirtin")
        tempo_mult = float(target_bpm) / float(resolved_src_bpm)
    mult = float(tempo_mult if tempo_mult is not None else 1.0)

    resolved_src_key = source_key
    semi = float(pitch_semitones if pitch_semitones is not None else 0.0)
    if target_key is not None and pitch_semitones is None:
        if resolved_src_key is None:
            resolved_src_key = str(detect_key(src)["key"])
        semi = float(semitones_between_keys(resolved_src_key, target_key))

    with _wav_input(src) as wav_in, tempfile.TemporaryDirectory(prefix="mpc_stretch_") as td:
        raw_out = Path(td) / "stretched.wav"
        engine_meta = STRETCH_BACKENDS[backend](wav_in, raw_out, mult, semi, formant, crisp)
        _finalize_mpc_wav(raw_out, out_wav, sample_rate=sample_rate, bit_depth=bit_depth)
        in_meta = _probe_audio_meta(wav_in)
        out_meta = _probe_audio_meta(out_wav)

    result: Dict[str, Any] = {
        "input": str(src),
        "output": str(out_wav),
        "backend": backend,
        **engine_meta,
        "source_bpm": resolved_src_bpm,
        "target_bpm": target_bpm,
        "tempo_multiplier": round(mult, 6),
        "source_key": resolved_src_key,
        "target_key": target_key,
        "pitch_semitones": round(semi, 3),
        "formant": formant,
        "input_duration_s": in_meta.get("duration_s"),
        "output_duration_s": out_meta.get("duration_s"),
        "bytes": out_wav.stat().st_size,
    }
    emit_activity("audio", "stretch", f"{src.name} -> {out_wav.name} (x{mult:.3f}, {semi:+.1f}st, {backend})")

    if deploy:
        target_host = host or os.environ.get("MPC_HOST") or MpcMap().data.get("meta", {}).get("default_host", "10.101.132.233")
        dep = deploy_file_to_mpc(out_wav, target_host, remote_dir=remote_dir, source="audio", action="deploy_wav")
        result["deploy"] = dep
    return result


# ---------------------------------------------------------------------------
# Sample Slicing / Chopping for MPC Pads
# ---------------------------------------------------------------------------

def chop_audio(
    input_path: Path,
    out_dir: Optional[Path] = None,
    mode: str = "onsets",
    backend: str = "auto",
    slices: int = 16,
    min_gap_ms: int = 90,
    fade_ms: float = 3.0,
    bit_depth: int = 16,
    deploy: bool = False,
    host: Optional[str] = None,
    remote_dir: str = "/media/okcomputer/MPCUTER_Audio",
) -> Dict[str, Any]:
    src = Path(input_path).expanduser().resolve()
    dest = Path(out_dir).expanduser().resolve() if out_dir else (src.parent / f"{src.stem}_chops")

    with _wav_input(src) as wav:
        meta = _probe_audio_meta(wav)
        dur = float(meta.get("duration_s") or 0.0)
        if mode == "grid":
            n = max(1, int(slices))
            cuts = [round((dur * i) / n, 4) for i in range(n)]
        elif mode == "beats":
            b = detect_beat(wav, backend=backend)
            cuts = _debounce_timestamps(b.get("beats_s") or [0.0], min_gap_ms / 1000.0, max_count=slices)
        elif mode == "onsets":
            b = detect_beat(wav, backend=backend)
            cuts = _debounce_timestamps(b.get("onsets_s") or [0.0], min_gap_ms / 1000.0, max_count=slices)
        else:
            raise ValueError("chop mode 'onsets', 'beats' veya 'grid' olmalıdır")

        chopped = _call_librosa_worker({
            "op": "chop",
            "wav_path": str(wav),
            "out_dir": str(dest),
            "cuts_s": cuts,
            "max_slices": slices,
            "prefix": src.stem,
            "fade_ms": fade_ms,
            "bit_depth": bit_depth,
        })

    manifest = {
        "source": str(src),
        "out_dir": str(dest),
        "mode": mode,
        "count": chopped["count"],
        "sample_rate": chopped["sample_rate"],
        "slices": chopped["slices"],
    }
    manifest_path = dest / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    manifest["manifest"] = str(manifest_path)

    emit_activity("audio", "chop", f"{src.name} -> {chopped['count']} pad ({mode}) -> {dest.name}/")
    if deploy:
        target_host = host or os.environ.get("MPC_HOST") or MpcMap().data.get("meta", {}).get("default_host", "10.101.132.233")
        pack_remote = f"{remote_dir.rstrip('/')}/{dest.name}"
        deployed_files = []
        for s in chopped["slices"]:
            deployed_files.append(
                deploy_file_to_mpc(Path(s["file"]), target_host, remote_dir=pack_remote, source="audio", action="deploy_chop")
            )
        manifest["deployed_dir"] = pack_remote
        manifest["deployed_count"] = len(deployed_files)
    return manifest


def _debounce_timestamps(raw: List[float], min_gap_s: float, max_count: int) -> List[float]:
    filtered: List[float] = []
    for t in sorted(float(x) for x in raw if x >= 0.0):
        if not filtered or (t - filtered[-1]) >= min_gap_s:
            filtered.append(t)
        if len(filtered) >= max(1, int(max_count)):
            break
    return filtered or [0.0]


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------

def run_audio_cli(argv: Optional[List[str]] = None) -> int:
    raw_argv = sys.argv[1:] if argv is None else argv
    if raw_argv and raw_argv[0] == "--_worker-mir":
        payload = json.loads(sys.stdin.read())
        print(json.dumps(_execute_mir_payload(payload), ensure_ascii=False))
        return 0

    parser = argparse.ArgumentParser(description="MPCUTER Audio DSP Araçları (Beat / Key / Stretch / Chop)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("backends", help="Kurulu açık kaynak DSP motorlarını listele")

    p_an = sub.add_parser("analyze", help="BPM, beat grid, key (Camelot) ve onset analizi yap")
    p_an.add_argument("file", type=Path)
    p_an.add_argument("--beat-backend", default="auto", choices=sorted(BEAT_BACKENDS))
    p_an.add_argument("--key-backend", default="ensemble", choices=sorted(KEY_BACKENDS))
    p_an.add_argument("--min-bpm", type=float, default=55.0)
    p_an.add_argument("--max-bpm", type=float, default=175.0)
    p_an.add_argument("--no-arrays", action="store_true", help="beats_s / onsets_s listelerini gizle")

    p_st = sub.add_parser("stretch", help="Time-stretching & pitch-shifting (Rubber Band R3 / R2 / SoundTouch / FFmpeg)")
    p_st.add_argument("file", type=Path)
    p_st.add_argument("-o", "--out", type=Path, default=None)
    p_st.add_argument("--backend", default="rubberband-r3", choices=sorted(STRETCH_BACKENDS))
    p_st.add_argument("--tempo-mult", type=float, default=None, help="Doğrudan tempo çarpanı (ör. 1.1)")
    p_st.add_argument("--source-bpm", type=float, default=None)
    p_st.add_argument("--target-bpm", type=float, default=None)
    p_st.add_argument("--pitch", "--semitones", dest="pitch", type=float, default=None)
    p_st.add_argument("--source-key", default=None)
    p_st.add_argument("--target-key", default=None)
    p_st.add_argument("--formant", action="store_true", help="Pitch shift sırasında formantları koru")
    p_st.add_argument("--crisp", type=int, default=None, help="Rubber Band R2 crispness (0..6)")
    p_st.add_argument("--sr", type=int, default=44100, help="Çıkış örnekleme hızı (Hz)")
    p_st.add_argument("--bits", type=int, default=16, choices=[16, 24], help="WAV bit derinliği")
    p_st.add_argument("--deploy", action="store_true", help="MPC /media/okcomputer/MPCUTER_Audio içine SCP yükle")
    p_st.add_argument("--host", default=None)

    p_ch = sub.add_parser("chop", help="Sample'ı onset / beat / eşit pad dilimlerine böl")
    p_ch.add_argument("file", type=Path)
    p_ch.add_argument("-o", "--out-dir", type=Path, default=None)
    p_ch.add_argument("--mode", default="onsets", choices=["onsets", "beats", "grid"])
    p_ch.add_argument("--backend", default="auto", choices=sorted(BEAT_BACKENDS))
    p_ch.add_argument("--slices", type=int, default=16)
    p_ch.add_argument("--min-gap-ms", type=int, default=90)
    p_ch.add_argument("--fade-ms", type=float, default=3.0)
    p_ch.add_argument("--bits", type=int, default=16, choices=[16, 24])
    p_ch.add_argument("--deploy", action="store_true")
    p_ch.add_argument("--host", default=None)

    args = parser.parse_args(raw_argv)
    if args.cmd == "backends":
        out = detect_backends()
    elif args.cmd == "analyze":
        out = analyze_audio(
            args.file,
            beat_backend=args.beat_backend,
            key_backend=args.key_backend,
            min_bpm=args.min_bpm,
            max_bpm=args.max_bpm,
        )
        if args.no_arrays:
            out.pop("beats_s", None)
            out.pop("onsets_s", None)
    elif args.cmd == "stretch":
        out = stretch_audio(
            args.file,
            output_path=args.out,
            backend=args.backend,
            tempo_mult=args.tempo_mult,
            source_bpm=args.source_bpm,
            target_bpm=args.target_bpm,
            pitch_semitones=args.pitch,
            source_key=args.source_key,
            target_key=args.target_key,
            formant=args.formant,
            crisp=args.crisp,
            sample_rate=args.sr,
            bit_depth=args.bits,
            deploy=args.deploy,
            host=args.host,
        )
    elif args.cmd == "chop":
        out = chop_audio(
            args.file,
            out_dir=args.out_dir,
            mode=args.mode,
            backend=args.backend,
            slices=args.slices,
            min_gap_ms=args.min_gap_ms,
            fade_ms=args.fade_ms,
            bit_depth=args.bits,
            deploy=args.deploy,
            host=args.host,
        )
    else:
        return 2

    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_audio_cli())
