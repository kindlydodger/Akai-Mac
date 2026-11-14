#!/usr/bin/env python3
"""Akai MPC Live Drum Program (.xpm + 16 named WAVs) generator powered by Lyria stems + spectral slicing."""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import shutil
import struct
import subprocess
import tempfile
import time
import wave
import xml.sax.saxutils as xml_escape
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from . import mpc_audio
    from .mpc_client import MpcBridgeClient, emit_activity
except ImportError:  # pragma: no cover - direct script execution
    import mpc_audio  # type: ignore
    from mpc_client import MpcBridgeClient, emit_activity  # type: ignore

BASE_DIR = Path(__file__).resolve().parent
KITS_DIR = Path(os.environ.get("MPC_FLOW_KITS_DIR") or str(BASE_DIR / "flow_kits"))
DEFAULT_REMOTE_KITS_DIR = "/media/okcomputer/FlowKits"
SAMPLE_RATE = 44100

# Standard Akai MPC PadNoteMap (Banks A..H = 1..128; Pad 1..16 are Bank A01..A16)
MPC_BANK_A_NOTES: Tuple[int, ...] = (37, 36, 42, 82, 40, 38, 46, 44, 48, 47, 45, 43, 49, 55, 51, 53)
_EXTRA_PAD_NOTES: Tuple[int, ...] = tuple(
    [54, 69, 81, 80, 65, 66, 76, 77, 56, 62, 63, 64, 73, 74, 71, 39]
    + [n for n in range(128) if n not in {37, 36, 42, 82, 40, 38, 46, 44, 48, 47, 45, 43, 49, 55, 51, 53, 54, 69, 81, 80, 65, 66, 76, 77, 56, 62, 63, 64, 73, 74, 71, 39}]
)
MPC_PAD_NOTES_128: Tuple[int, ...] = (MPC_BANK_A_NOTES + _EXTRA_PAD_NOTES)[:128]

# 16 structured Bank A pad slots matching finger-drumming layout & Akai Expansion conventions
DRUM_KIT_SLOTS: Tuple[Dict[str, Any], ...] = (
    {"pad": 1,  "pad_id": "A01", "role": "kick",       "slug": "01_Kick_Main",  "short": "KICK 1",   "mute_group": 0, "mono": True,  "poly": 1, "color": 0xD82820, "max_s": 0.65, "fade_ms": 18.0},
    {"pad": 2,  "pad_id": "A02", "role": "snare",      "slug": "02_Snare_Main", "short": "SNARE 1",  "mute_group": 0, "mono": True,  "poly": 1, "color": 0xD89010, "max_s": 0.55, "fade_ms": 16.0},
    {"pad": 3,  "pad_id": "A03", "role": "hat_closed", "slug": "03_Hat_Closed", "short": "CL HAT",   "mute_group": 1, "mono": True,  "poly": 1, "color": 0x18A8D8, "max_s": 0.18, "fade_ms": 12.0},
    {"pad": 4,  "pad_id": "A04", "role": "hat_open",   "slug": "04_Hat_Open",   "short": "OP HAT",   "mute_group": 1, "mono": True,  "poly": 1, "color": 0x2058E8, "max_s": 0.85, "fade_ms": 28.0},
    {"pad": 5,  "pad_id": "A05", "role": "sub808",     "slug": "05_808_Sub",    "short": "808 SUB",  "mute_group": 2, "mono": True,  "poly": 1, "color": 0x9820D0, "max_s": 1.15, "fade_ms": 30.0},
    {"pad": 6,  "pad_id": "A06", "role": "clap",       "slug": "06_Clap",       "short": "CLAP",     "mute_group": 0, "mono": True,  "poly": 1, "color": 0xD8C018, "max_s": 0.45, "fade_ms": 18.0},
    {"pad": 7,  "pad_id": "A07", "role": "rim",        "slug": "07_Rimshot",    "short": "RIM",      "mute_group": 0, "mono": True,  "poly": 1, "color": 0xC87820, "max_s": 0.22, "fade_ms": 12.0},
    {"pad": 8,  "pad_id": "A08", "role": "snare_alt",  "slug": "08_Snare_Alt",  "short": "SNARE 2",  "mute_group": 0, "mono": True,  "poly": 1, "color": 0xD06018, "max_s": 0.48, "fade_ms": 16.0},
    {"pad": 9,  "pad_id": "A09", "role": "kick_alt",   "slug": "09_Kick_Punch", "short": "KICK 2",   "mute_group": 2, "mono": True,  "poly": 1, "color": 0xB82040, "max_s": 0.55, "fade_ms": 16.0},
    {"pad": 10, "pad_id": "A10", "role": "tom_low",    "slug": "10_Tom_Low",    "short": "TOM LO",   "mute_group": 0, "mono": True,  "poly": 1, "color": 0x7838D0, "max_s": 0.60, "fade_ms": 20.0},
    {"pad": 11, "pad_id": "A11", "role": "tom_mid",    "slug": "11_Tom_Mid",    "short": "TOM MID",  "mute_group": 0, "mono": True,  "poly": 1, "color": 0x5848D8, "max_s": 0.52, "fade_ms": 18.0},
    {"pad": 12, "pad_id": "A12", "role": "perc",       "slug": "12_Perc_Hit",   "short": "PERC",     "mute_group": 0, "mono": True,  "poly": 1, "color": 0x20B060, "max_s": 0.42, "fade_ms": 15.0},
    {"pad": 13, "pad_id": "A13", "role": "shaker",     "slug": "13_Shaker",     "short": "SHAKER",   "mute_group": 0, "mono": True,  "poly": 1, "color": 0x20B8A8, "max_s": 0.30, "fade_ms": 16.0},
    {"pad": 14, "pad_id": "A14", "role": "crash",      "slug": "14_Crash",      "short": "CRASH",    "mute_group": 3, "mono": True,  "poly": 1, "color": 0x4088E8, "max_s": 1.35, "fade_ms": 40.0},
    {"pad": 15, "pad_id": "A15", "role": "bell",       "slug": "15_Bell_Ride",  "short": "BELL",     "mute_group": 3, "mono": True,  "poly": 1, "color": 0x50A8E8, "max_s": 0.75, "fade_ms": 25.0},
    {"pad": 16, "pad_id": "A16", "role": "fx_stab",    "slug": "16_FX_Stab",    "short": "FX STAB",  "mute_group": 0, "mono": False, "poly": 4, "color": 0x24FF42, "max_s": 1.10, "fade_ms": 28.0},
)

DRUM_PRESETS: Tuple[Dict[str, str], ...] = (
    {"label": "90s Boom Bap",  "prompt": "90s new york boom bap dusty vinyl crate drum kit punchy kick crisp snare"},
    {"label": "Dark Trap 808", "prompt": "dark metro boomin trap 808 drum kit hard punchy kick sharp clap rattling hats"},
    {"label": "Lo-Fi Dusty",   "prompt": "warm lofi sp404 tape saturated chill hip hop drum kit brushed snare sub kick"},
    {"label": "UK Drill",      "prompt": "uk drill hard sliding 808 punchy kick counter snare sharp hi-hats percussive"},
    {"label": "House 909",     "prompt": "classic chicago house tr909 punchy club drum kit driving kick 909 clap ride"},
    {"label": "Memphis Phonk", "prompt": "memphis phonk lo-fi distorted cowbell 808 cowbell crunchy snare dark tape kit"},
    {"label": "Jazz Brush",    "prompt": "acoustic jazz trio brushed snare warm upright woody kick ride cymbal rimshot"},
    {"label": "Afro Perc",     "prompt": "afrobeats log drum conga rimshot crisp shaker warm pocket kick drum kit"},
)


def sanitize_kit_name(text: str, custom_name: Optional[str] = None, max_len: int = 24) -> str:
    """Create a filesystem-safe, readable Akai kit name (ASCII alphanumeric + underscore)."""
    raw = (custom_name or "").strip()
    if not raw:
        stop_words = {
            "a", "an", "the", "with", "and", "for", "of", "in", "on", "to", "kit", "drum", "drums",
            "bana", "bir", "su", "tarzda", "tarzi", "stilde", "gibi", "olustur", "yap",
        }
        tokens = [t for t in re.findall(r"[A-Za-z0-9]+", text or "") if t.lower() not in stop_words]
        if not tokens:
            tokens = re.findall(r"[A-Za-z0-9]+", text or "")
        raw = "_".join(t.capitalize() if not t.isdigit() else t for t in tokens[:4])
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_")
    if not cleaned:
        cleaned = f"FlowKit_{int(time.time()) % 10000:04d}"
    return cleaned[:max_len].rstrip("_") or "Flow_Kit"


def _wav_prefix_from_kit(kit_name: str) -> str:
    tokens = [t for t in kit_name.split("_") if t]
    if not tokens:
        return "FG"
    abbr = "".join(t[:3] for t in tokens[:2])
    return (abbr[:8] or "FG")


def _build_program_pads_json(pad_colors: Dict[int, int]) -> str:
    pads_dict = {f"value{i}": int(pad_colors.get(i + 1, 0)) for i in range(128)}
    payload = {
        "ProgramPads": {
            "Universal": {"value0": False},
            "Type": {"value0": 5},
            "universalPad": int(pad_colors.get(1, 0xD82820)),
            "pads": pads_dict,
        }
    }
    raw_json = json.dumps(payload, indent=4)
    return xml_escape.escape(raw_json, {'"': "&quot;"})


def _instrument_xml(inst_num: int, spec: Optional[Dict[str, Any]], sample_stem: str) -> str:
    mono = "True" if (spec is None or spec.get("mono", True)) else "False"
    poly = int(spec.get("poly", 1) if spec else 1)
    mute_group = int(spec.get("mute_group", 0) if spec else 0)
    oneshot = "True"
    safe_sample = xml_escape.escape(sample_stem)

    lines = [
        f'      <Instrument number="{inst_num}">',
        "        <AudioRoute>",
        "          <AudioRoute>0</AudioRoute>",
        "          <AudioRouteSubIndex>0</AudioRouteSubIndex>",
        "          <InsertsEnabled>True</InsertsEnabled>",
        "        </AudioRoute>",
        "        <Send1>0.000000</Send1>",
        "        <Send2>0.000000</Send2>",
        "        <Send3>0.000000</Send3>",
        "        <Send4>0.000000</Send4>",
        "        <Volume>0.707946</Volume>",
        "        <Mute>False</Mute>",
        "        <Pan>0.500000</Pan>",
        "        <TuneCoarse>0</TuneCoarse>",
        "        <TuneFine>0</TuneFine>",
        f"        <Mono>{mono}</Mono>",
        f"        <Polyphony>{poly}</Polyphony>",
        "        <FilterKeytrack>0.000000</FilterKeytrack>",
        "        <LowNote>0</LowNote>",
        "        <HighNote>127</HighNote>",
        "        <IgnoreBaseNote>False</IgnoreBaseNote>",
        "        <ZonePlay>1</ZonePlay>",
        f"        <MuteGroup>{mute_group}</MuteGroup>",
        "        <MuteTarget1>0</MuteTarget1>",
        "        <MuteTarget2>0</MuteTarget2>",
        "        <MuteTarget3>0</MuteTarget3>",
        "        <MuteTarget4>0</MuteTarget4>",
        "        <SimultTarget1>0</SimultTarget1>",
        "        <SimultTarget2>0</SimultTarget2>",
        "        <SimultTarget3>0</SimultTarget3>",
        "        <SimultTarget4>0</SimultTarget4>",
        "        <LfoPitch>0.000000</LfoPitch>",
        "        <LfoCutoff>0.000000</LfoCutoff>",
        "        <LfoVolume>0.000000</LfoVolume>",
        "        <LfoPan>0.000000</LfoPan>",
        f"        <OneShot>{oneshot}</OneShot>",
        "        <FilterType>0</FilterType>",
        "        <Cutoff>1.000000</Cutoff>",
        "        <Resonance>0.000000</Resonance>",
        "        <FilterEnvAmt>0.000000</FilterEnvAmt>",
        "        <AfterTouchToFilter>0.000000</AfterTouchToFilter>",
        "        <VelocityToStart>0.000000</VelocityToStart>",
        "        <VelocityToFilterAttack>0.000000</VelocityToFilterAttack>",
        "        <VelocityToFilter>0.000000</VelocityToFilter>",
        "        <VelocityToFilterEnvelope>0.000000</VelocityToFilterEnvelope>",
        "        <FilterAttack>0.000000</FilterAttack>",
        "        <FilterDecay>0.050000</FilterDecay>",
        "        <FilterSustain>1.000000</FilterSustain>",
        "        <FilterRelease>0.000000</FilterRelease>",
        "        <FilterHold>0.000000</FilterHold>",
        "        <FilterDecayType>True</FilterDecayType>",
        "        <FilterADEnvelope>True</FilterADEnvelope>",
        "        <VolumeHold>0.000000</VolumeHold>",
        "        <VolumeDecayType>True</VolumeDecayType>",
        "        <VolumeADEnvelope>True</VolumeADEnvelope>",
        "        <VolumeAttack>0.000000</VolumeAttack>",
        "        <VolumeDecay>0.050000</VolumeDecay>",
        "        <VolumeSustain>1.000000</VolumeSustain>",
        "        <VolumeRelease>0.000000</VolumeRelease>",
        "        <VelocityToPitch>0.000000</VelocityToPitch>",
        "        <VelocityToVolumeAttack>0.000000</VelocityToVolumeAttack>",
        "        <VelocitySensitivity>1.000000</VelocitySensitivity>",
        "        <VelocityToPan>0.000000</VelocityToPan>",
        "        <LFO>",
        "          <Type>Sine</Type>",
        "          <Rate>0.500000</Rate>",
        "          <Sync>0</Sync>",
        "          <Reset>False</Reset>",
        "        </LFO>",
        "        <Layers>",
    ]
    for layer_num in range(1, 5):
        sname = safe_sample if layer_num == 1 else ""
        lines.extend([
            f'          <Layer number="{layer_num}">',
            "            <Active>True</Active>",
            "            <Volume>1.000000</Volume>",
            "            <Pan>0.500000</Pan>",
            "            <Pitch>0.000000</Pitch>",
            "            <TuneCoarse>0</TuneCoarse>",
            "            <TuneFine>0</TuneFine>",
            "            <VelStart>0</VelStart>",
            "            <VelEnd>127</VelEnd>",
            "            <SampleStart>0</SampleStart>",
            "            <SampleEnd>0</SampleEnd>",
            "            <Loop>False</Loop>",
            "            <LoopStart>0</LoopStart>",
            "            <LoopEnd>0</LoopEnd>",
            "            <LoopTune>0</LoopTune>",
            "            <Mute>False</Mute>",
            "            <RootNote>0</RootNote>",
            "            <KeyTrack>False</KeyTrack>",
            f"            <SampleName>{sname}</SampleName>",
            "            <SampleFile></SampleFile>",
            "            <SliceIndex>128</SliceIndex>",
            "            <Direction>0</Direction>",
            "            <Offset>0</Offset>",
            "            <SliceStart>0</SliceStart>",
            "            <SliceEnd>0</SliceEnd>",
            "            <SliceLoopStart>0</SliceLoopStart>",
            "            <SliceLoop>0</SliceLoop>",
            "          </Layer>",
        ])
    lines.extend([
        "        </Layers>",
        "      </Instrument>",
    ])
    return "\n".join(lines)


def build_drum_program_xpm(program_name: str, pads: List[Dict[str, Any]]) -> str:
    """Render an Akai MPC Live compatible Drum Program (.xpm, File_Version 1.7)."""
    by_pad: Dict[int, Dict[str, Any]] = {int(p["pad"]): p for p in pads}
    colors = {i: int(by_pad[i].get("color", 0)) for i in range(1, 17) if i in by_pad}
    safe_name = xml_escape.escape(program_name)

    instruments_xml = [
        _instrument_xml(i, by_pad.get(i), str(by_pad[i]["sample_stem"]) if i in by_pad else "")
        for i in range(1, 129)
    ]
    pad_note_xml = [
        f'      <PadNote number="{i}">\n        <Note>{MPC_PAD_NOTES_128[i - 1]}</Note>\n      </PadNote>'
        for i in range(1, 129)
    ]
    qlinks_xml = [
        "\n".join([
            f'      <Qlink number="{i}">',
            "        <Controller>513</Controller>",
            "        <Min>-120.000000</Min>",
            "        <Max>120.000000</Max>",
            "        <TouchToMax>True</TouchToMax>",
            f"        <PadAssign>{i}</PadAssign>",
            "        <ChangeRealTime>True</ChangeRealTime>",
            "        <NoteOnValue>0.500000</NoteOnValue>",
            "      </Qlink>",
        ])
        for i in range(1, 17)
    ]

    return "\n".join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        "",
        "<MPCVObject>",
        "  <Version>",
        "    <File_Version>1.7</File_Version>",
        "    <Application>MPC-V</Application>",
        "    <Application_Version>2.15.1.1</Application_Version>",
        "    <Platform>Linux</Platform>",
        "  </Version>",
        '  <Program type="Drum">',
        f"    <Name>{safe_name}</Name>",
        f"    <ProgramPads>{_build_program_pads_json(colors)}</ProgramPads>",
        "    <AudioRoute>",
        "      <AudioRoute>2</AudioRoute>",
        "      <AudioRouteSubIndex>0</AudioRouteSubIndex>",
        "      <InsertsEnabled>True</InsertsEnabled>",
        "    </AudioRoute>",
        "    <Send1>0.000000</Send1>",
        "    <Send2>0.000000</Send2>",
        "    <Send3>0.000000</Send3>",
        "    <Send4>0.000000</Send4>",
        "    <Volume>0.707946</Volume>",
        "    <Mute>False</Mute>",
        "    <Pan>0.500000</Pan>",
        "    <Pitch>0.000000</Pitch>",
        "    <TuneCoarse>0</TuneCoarse>",
        "    <TuneFine>0</TuneFine>",
        "    <Mono>False</Mono>",
        "    <Program_Polyphony>0</Program_Polyphony>",
        "    <Instruments>",
        *instruments_xml,
        "    </Instruments>",
        "    <PadNoteMap>",
        *pad_note_xml,
        "    </PadNoteMap>",
        "    <Qlinks>",
        *qlinks_xml,
        "    </Qlinks>",
        "  </Program>",
        "</MPCVObject>",
        "",
    ])


# ---------------------------------------------------------------------------
# Lyria Stem Transient Slicer + Spectral Role Classifier (.venv-audio helper)
# ---------------------------------------------------------------------------

_SLICE_CLASSIFIER_CODE = r"""
import json, math, sys, wave
from pathlib import Path
import numpy as np
import librosa

payload = json.loads(sys.stdin.read())
stems = payload["stems"]
out_dir = Path(payload["out_dir"])
sr = int(payload.get("sr", 44100))

candidates = []
for stem_idx, item in enumerate(stems):
    wav_path = Path(item["path"])
    kind_hint = item.get("stem_kind", "core")
    if not wav_path.is_file():
        continue
    y_st, _ = librosa.load(str(wav_path), sr=sr, mono=False)
    if y_st.ndim == 1:
        y_st = np.vstack([y_st, y_st])
    elif y_st.shape[0] > 2:
        y_st = y_st[:2]
    y_mono = np.mean(y_st, axis=0)
    if y_mono.size < int(0.1 * sr):
        continue

    onset_frames = librosa.onset.onset_detect(
        y=y_mono, sr=sr, hop_length=256, backtrack=True, delta=0.055, wait=6
    )
    onset_samples = librosa.frames_to_samples(onset_frames, hop_length=256).tolist()
    if not onset_samples or onset_samples[0] > int(0.05 * sr):
        onset_samples = [0] + onset_samples

    n_total = y_mono.shape[0]
    for i, start in enumerate(onset_samples):
        # Backtrack slightly to nearest low-energy zero crossing (max 2ms earlier)
        s0 = max(0, start - int(0.002 * sr))
        next_s = onset_samples[i + 1] if (i + 1 < len(onset_samples)) else n_total
        hard_cap = s0 + int(1.35 * sr)
        s1 = min(n_total, next_s - int(0.004 * sr), hard_cap)
        if s1 - s0 < int(0.065 * sr):
            continue

        seg_m = y_mono[s0:s1]
        seg_s = y_st[:, s0:s1]
        # Trim trailing near-silence below -46 dBFS
        abs_m = np.abs(seg_m)
        peak = float(np.max(abs_m))
        if peak < 0.02:
            continue
        thresh = max(0.003, peak * 0.012)
        active_idx = np.where(abs_m >= thresh)[0]
        if active_idx.size < int(0.045 * sr):
            continue
        end_idx = min(len(seg_m), int(active_idx[-1]) + int(0.025 * sr))
        start_idx = max(0, int(active_idx[0]) - int(0.001 * sr))
        seg_m = seg_m[start_idx:end_idx]
        seg_s = seg_s[:, start_idx:end_idx].copy()
        n_len = seg_m.shape[0]
        if n_len < int(0.05 * sr):
            continue

        rms = float(np.sqrt(np.mean(seg_m ** 2)))
        if 20.0 * math.log10(max(rms, 1e-6)) < -40.0:
            continue

        # Spectral features from FFT
        win = np.hanning(n_len)
        spec = np.abs(np.fft.rfft(seg_m * win)) ** 2
        freqs = np.fft.rfftfreq(n_len, d=1.0 / sr)
        tot_e = float(np.sum(spec)) + 1e-12
        sub_ratio = float(np.sum(spec[(freqs >= 24) & (freqs < 85)]) / tot_e)
        low_ratio = float(np.sum(spec[(freqs >= 24) & (freqs < 170)]) / tot_e)
        mid_ratio = float(np.sum(spec[(freqs >= 170) & (freqs < 2400)]) / tot_e)
        high_ratio = float(np.sum(spec[freqs >= 4200]) / tot_e)
        centroid = float(np.sum(freqs * spec) / tot_e)
        geo = float(np.exp(np.mean(np.log(spec[spec > 0] + 1e-12))))
        arith = float(np.mean(spec) + 1e-12)
        flatness = min(1.0, geo / arith)
        zcr = float(np.mean(np.abs(np.diff(np.signbit(seg_m)))))
        crest = peak / max(rms, 1e-6)
        dur_s = n_len / float(sr)

        # Normalize + clickless edges
        seg_s -= np.mean(seg_s, axis=1, keepdims=True)
        seg_peak = float(np.max(np.abs(seg_s)))
        if seg_peak > 1e-6:
            seg_s *= (0.84 / seg_peak)
        fi = min(int(0.0012 * sr), max(4, n_len // 20))
        fo = min(int(0.022 * sr), max(16, n_len // 6))
        seg_s[:, :fi] *= np.linspace(0.0, 1.0, fi)[None, :]
        seg_s[:, -fo:] *= np.linspace(1.0, 0.0, fo)[None, :]

        cand_path = out_dir / f"cand_{stem_idx}_{len(candidates):03d}.wav"
        inter = np.clip(seg_s.T, -0.999, 0.999)
        pcm = (inter * 32767.0).astype("<i2")
        with wave.open(str(cand_path), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(pcm.tobytes())

        candidates.append({
            "path": str(cand_path),
            "stem_kind": kind_hint,
            "dur_s": round(dur_s, 4),
            "sub_ratio": round(sub_ratio, 4),
            "low_ratio": round(low_ratio, 4),
            "mid_ratio": round(mid_ratio, 4),
            "high_ratio": round(high_ratio, 4),
            "centroid_hz": round(centroid, 1),
            "flatness": round(flatness, 4),
            "zcr": round(zcr, 4),
            "crest": round(crest, 2),
        })

print(json.dumps({"ok": True, "candidates": candidates}))
"""


def _extract_lyria_candidates(stem_specs: List[Dict[str, str]], out_dir: Path, sr: int = SAMPLE_RATE) -> List[Dict[str, Any]]:
    """Use .venv-audio librosa worker when present to slice and analyze Lyria drum stems."""
    py = mpc_audio._find_audio_python()
    if not py:
        return []
    payload = {"stems": stem_specs, "out_dir": str(out_dir), "sr": int(sr)}
    try:
        proc = subprocess.run(
            [py, "-c", _SLICE_CLASSIFIER_CODE],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=mpc_audio._augmented_env(),
            timeout=60.0,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        data = json.loads(proc.stdout.strip().splitlines()[-1])
        return list(data.get("candidates") or []) if data.get("ok") else []
    except Exception:
        return []


def _score_candidate_for_role(c: Dict[str, Any], role: str) -> float:
    low = float(c["low_ratio"])
    sub = float(c["sub_ratio"])
    mid = float(c["mid_ratio"])
    high = float(c["high_ratio"])
    dur = float(c["dur_s"])
    flat = float(c["flatness"])
    cent = float(c["centroid_hz"])
    stem = c.get("stem_kind", "core")
    bonus = 0.25 if (stem == "core" and role in ("kick", "kick_alt", "sub808", "snare", "snare_alt", "clap", "tom_low", "tom_mid")) else (
        0.25 if (stem == "top" and role in ("hat_closed", "hat_open", "rim", "shaker", "crash", "bell", "perc", "fx_stab")) else 0.0
    )

    if role == "kick":
        return 3.2 * low + 1.0 * sub - 2.0 * high + (0.6 if 0.14 <= dur <= 0.55 else -0.3) + bonus
    if role == "kick_alt":
        return 2.6 * low + 1.2 * mid - 1.8 * high + (0.4 if 0.12 <= dur <= 0.45 else -0.2) + bonus
    if role == "sub808":
        return 3.8 * sub + 1.4 * low - 2.5 * high + (0.9 if dur >= 0.35 else -0.8) + bonus
    if role == "snare":
        return 2.4 * mid + 1.4 * high + 1.2 * flat - 1.5 * sub + (0.6 if 0.12 <= dur <= 0.45 else -0.2) + bonus
    if role == "snare_alt":
        return 2.0 * mid + 1.2 * high + 0.9 * flat - 1.2 * low + (0.4 if 0.09 <= dur <= 0.40 else -0.2) + bonus
    if role == "clap":
        return 2.2 * mid + 2.0 * high + 1.6 * flat - 2.2 * low + (0.5 if 0.12 <= dur <= 0.40 else -0.3) + bonus
    if role == "rim":
        return 2.2 * mid + 1.2 * high - 2.0 * low + (0.9 if dur <= 0.18 else -0.9) + bonus
    if role == "hat_closed":
        return 3.4 * high - 3.0 * low - 0.8 * mid + (1.0 if dur <= 0.20 else -1.1) + bonus
    if role == "hat_open":
        return 3.2 * high - 2.8 * low + (1.1 if 0.24 <= dur <= 1.0 else -0.8) + bonus
    if role == "shaker":
        return 2.8 * high + 1.2 * flat - 2.5 * low + (0.6 if 0.09 <= dur <= 0.32 else -0.3) + bonus
    if role == "tom_low":
        return 2.1 * mid + 1.5 * low - 1.8 * high - 1.2 * flat + (0.4 if 0.16 <= dur <= 0.65 else -0.2) + bonus
    if role == "tom_mid":
        return 2.5 * mid + 0.7 * low - 1.6 * high - 1.2 * flat + (0.4 if 0.14 <= dur <= 0.55 else -0.2) + bonus
    if role == "perc":
        return 2.2 * mid + 0.8 * high - 1.5 * low - 0.6 * flat + (0.5 if 0.08 <= dur <= 0.38 else -0.2) + bonus
    if role == "crash":
        return 3.0 * high + 1.5 * flat - 2.4 * low + (1.2 if dur >= 0.45 else -1.2) + bonus
    if role == "bell":
        return 1.8 * high + 1.5 * mid - 2.2 * low - 1.4 * flat + (0.5 if cent >= 1400 else -0.3) + bonus
    if role == "fx_stab":
        return 1.8 * mid + 0.8 * low + 0.6 * high - 1.0 * flat + (0.9 if dur >= 0.28 else -0.4) + bonus
    return 0.0


def _assign_candidates_to_slots(candidates: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """Greedily pick the cleanest distinct candidate for each high-value pad role."""
    used: set[int] = set()
    assigned: Dict[int, Dict[str, Any]] = {}
    # Prioritize foundational roles first so Kick/Snare/Hats/808 always get the strongest match
    priority_order = [1, 3, 2, 4, 5, 6, 14, 7, 13, 10, 11, 12, 8, 9, 15, 16]
    slot_by_pad = {int(s["pad"]): s for s in DRUM_KIT_SLOTS}
    min_threshold = {
        "kick": 1.15, "kick_alt": 1.05, "sub808": 1.35, "snare": 1.25, "snare_alt": 1.15,
        "clap": 1.35, "rim": 1.15, "hat_closed": 1.45, "hat_open": 1.45, "shaker": 1.35,
        "tom_low": 1.05, "tom_mid": 1.05, "perc": 1.0, "crash": 1.45, "bell": 1.0, "fx_stab": 1.0,
    }

    for pad_num in priority_order:
        slot = slot_by_pad[pad_num]
        role = slot["role"]
        best_idx = -1
        best_score = min_threshold.get(role, 1.0)
        for idx, cand in enumerate(candidates):
            if idx in used:
                continue
            sc = _score_candidate_for_role(cand, role)
            if sc > best_score:
                best_score = sc
                best_idx = idx
        if best_idx >= 0:
            used.add(best_idx)
            assigned[pad_num] = {**candidates[best_idx], "match_score": round(best_score, 3)}
    return assigned


# ---------------------------------------------------------------------------
# Pure-Python Style-Adaptive 16-Voice DSP Drum One-Shot Synthesizer
# ---------------------------------------------------------------------------

def _trim_and_write_stereo_wav(out_wav: Path, buf_l: List[float], buf_r: List[float], sr: int, fade_ms: float) -> Tuple[int, float]:
    n = min(len(buf_l), len(buf_r))
    if n < 64:
        buf_l = [0.0] * 1024
        buf_r = [0.0] * 1024
        n = 1024

    mean_l = sum(buf_l[:n]) / n
    mean_r = sum(buf_r[:n]) / n
    peak = max((max(abs(buf_l[i] - mean_l), abs(buf_r[i] - mean_r)) for i in range(n)), default=1e-6)
    gain = 0.84 / max(peak, 1e-6)
    fi = max(4, min(int(0.0012 * sr), n // 16))
    fo = max(16, min(int((fade_ms / 1000.0) * sr), n // 4))

    pcm = bytearray(n * 4)
    for i in range(n):
        env = 1.0
        if i < fi:
            env = i / float(fi)
        elif i >= n - fo:
            env = (n - 1 - i) / float(max(1, fo))
        vl = max(-0.999, min(0.999, (buf_l[i] - mean_l) * gain * env))
        vr = max(-0.999, min(0.999, (buf_r[i] - mean_r) * gain * env))
        struct.pack_into("<hh", pcm, i * 4, int(vl * 32767.0), int(vr * 32767.0))

    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_wav), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(bytes(pcm))
    return n, round(n / float(sr), 4)


def _synth_drum_slot(
    role: str,
    hints: Dict[str, Any],
    pad_num: int,
    out_wav: Path,
    sr: int = SAMPLE_RATE,
) -> Tuple[int, float]:
    """Synthesize a crisp, style-matched stereo one-shot WAV when an isolated stem hit is unavailable."""
    style = str(hints.get("style", "lofi"))
    dusty = bool(hints.get("dusty", False))
    root_midi = int(hints.get("root_midi", 48))
    is_minor = bool(hints.get("is_minor", True))
    low_prompt = str(hints.get("prompt", "")).lower()
    sr_f = float(sr)

    rng = (0x9E3779B9 ^ (pad_num * 0x45D9F3B) ^ (sum(ord(ch) for ch in low_prompt) * 131)) & 0xFFFFFFFF

    def rand_bipolar() -> float:
        nonlocal rng
        rng = (rng * 1664525 + 1013904223) & 0xFFFFFFFF
        return ((rng >> 16) / 32768.0) - 1.0

    def midi_hz(m: float) -> float:
        return 440.0 * (2.0 ** ((m - 69.0) / 12.0))

    # Duration per role
    dur_map = {
        "kick": 0.34, "kick_alt": 0.28, "sub808": 0.85, "snare": 0.24, "snare_alt": 0.21,
        "clap": 0.26, "rim": 0.11, "hat_closed": 0.085, "hat_open": 0.52, "shaker": 0.16,
        "tom_low": 0.36, "tom_mid": 0.30, "perc": 0.22, "crash": 0.95, "bell": 0.45, "fx_stab": 0.65,
    }
    dur_s = dur_map.get(role, 0.28)
    n = int(dur_s * sr_f)
    buf_l = [0.0] * n
    buf_r = [0.0] * n

    drive = 2.1 if ("hard" in low_prompt or "distort" in low_prompt or "phonk" in low_prompt) else (1.5 if style in ("trap", "boombap") else 1.15)
    lp_alpha = 0.42 if dusty else 0.78

    if role in ("kick", "kick_alt"):
        f0 = 155.0 if (role == "kick_alt" or style == "house") else 128.0
        f1 = 44.0 if style == "trap" else 52.0
        dec = 7.5 if style == "trap" else (11.5 if role == "kick" else 14.0)
        ph = 0.0
        prev = 0.0
        for i in range(n):
            t = i / sr_f
            freq = f1 + (f0 - f1) * math.exp(-t * 34.0)
            ph += (2.0 * math.pi * freq) / sr_f
            click = rand_bipolar() * math.exp(-t * 210.0) * 0.28
            body = math.sin(ph) * math.exp(-t * dec)
            val = math.tanh(drive * (body + click))
            prev = prev + lp_alpha * (val - prev)
            buf_l[i] = buf_r[i] = prev

    elif role == "sub808":
        base_hz = midi_hz((root_midi % 12) + 36)  # C2..B2 sub register
        ph = 0.0
        for i in range(n):
            t = i / sr_f
            freq = base_hz + 95.0 * math.exp(-t * 36.0)
            ph += (2.0 * math.pi * freq) / sr_f
            sat = math.tanh(drive * 1.45 * math.sin(ph)) * math.exp(-t * 2.9)
            buf_l[i] = buf_r[i] = sat

    elif role in ("snare", "snare_alt"):
        tone_f = 192.0 if role == "snare" else 224.0
        dec_n = 15.0 if role == "snare" else 19.0
        ph1 = ph2 = prev_l = prev_r = 0.0
        for i in range(n):
            t = i / sr_f
            ph1 += (2.0 * math.pi * (tone_f + 45.0 * math.exp(-t * 40.0))) / sr_f
            ph2 += (2.0 * math.pi * (tone_f * 1.63)) / sr_f
            body = (0.65 * math.sin(ph1) + 0.35 * math.sin(ph2)) * math.exp(-t * 24.0)
            nz_l = rand_bipolar()
            nz_r = nz_l * 0.8 + rand_bipolar() * 0.2
            snap_l = (nz_l - 0.55 * prev_l) * math.exp(-t * dec_n) * 0.7
            snap_r = (nz_r - 0.55 * prev_r) * math.exp(-t * dec_n) * 0.7
            prev_l, prev_r = nz_l, nz_r
            buf_l[i] = math.tanh(drive * (body + snap_l))
            buf_r[i] = math.tanh(drive * (body + snap_r))

    elif role == "clap":
        burst_offsets = (0.0, 0.011, 0.023, 0.036)
        lp1 = lp2 = 0.0
        for i in range(n):
            t = i / sr_f
            env = 0.0
            for bo in burst_offsets:
                dt = t - bo
                if dt >= 0.0:
                    env += math.exp(-dt * (22.0 if bo > 0.03 else 95.0))
            nz1 = rand_bipolar()
            nz2 = rand_bipolar()
            bp1 = nz1 - 0.55 * lp1
            bp2 = nz2 - 0.55 * lp2
            lp1, lp2 = nz1, nz2
            buf_l[i] = math.tanh(1.25 * bp1 * env * 0.48)
            buf_r[i] = math.tanh(1.25 * (bp1 * 0.65 + bp2 * 0.35) * env * 0.48)

    elif role == "rim":
        ph1 = ph2 = 0.0
        for i in range(n):
            t = i / sr_f
            ph1 += (2.0 * math.pi * 490.0) / sr_f
            ph2 += (2.0 * math.pi * 1680.0) / sr_f
            v = (0.6 * math.sin(ph1) + 0.4 * math.sin(ph2)) * math.exp(-t * 48.0) + rand_bipolar() * math.exp(-t * 110.0) * 0.3
            buf_l[i] = buf_r[i] = math.tanh(1.4 * v)

    elif role in ("hat_closed", "hat_open", "crash"):
        dec = 46.0 if role == "hat_closed" else (5.8 if role == "hat_open" else 3.2)
        ratios = (263.0, 400.0, 421.0, 474.0, 587.0, 845.0)
        phases = [0.0] * 6
        prev_m = 0.0
        for i in range(n):
            t = i / sr_f
            metal = 0.0
            for idx_r, rf in enumerate(ratios):
                phases[idx_r] += (2.0 * math.pi * rf * (1.0 if role != "crash" else 1.18)) / sr_f
                metal += 1.0 if math.sin(phases[idx_r]) >= 0.0 else -1.0
            raw = (metal / 6.0) * 0.55 + rand_bipolar() * 0.45
            hp = raw - 0.80 * prev_m
            prev_m = raw
            env = math.exp(-t * dec)
            w_l = hp * env
            w_r = (hp * 0.86 + rand_bipolar() * 0.14 * env) * env if role != "hat_closed" else w_l
            buf_l[i], buf_r[i] = w_l, w_r

    elif role == "shaker":
        prev = 0.0
        for i in range(n):
            t = i / sr_f
            nz = rand_bipolar()
            hp = nz - 0.72 * prev
            prev = nz
            env = math.sin(min(1.0, t / 0.025) * math.pi * 0.5) * math.exp(-t * 17.0)
            buf_l[i] = hp * env
            buf_r[i] = (hp * 0.75 + rand_bipolar() * 0.25) * env

    elif role in ("tom_low", "tom_mid"):
        base = 88.0 if role == "tom_low" else 146.0
        ph = 0.0
        for i in range(n):
            t = i / sr_f
            ph += (2.0 * math.pi * (base + base * 0.9 * math.exp(-t * 28.0))) / sr_f
            buf_l[i] = buf_r[i] = math.tanh(1.3 * math.sin(ph)) * math.exp(-t * 8.5)

    elif role in ("perc", "bell"):
        if role == "bell" and "phonk" in low_prompt:
            f1, f2, dec = 560.0, 845.0, 7.5
        elif role == "bell":
            f1, f2, dec = 880.0, 2110.0, 6.0
        else:
            f1, f2, dec = 310.0, 465.0, 14.0
        ph1 = ph2 = 0.0
        for i in range(n):
            t = i / sr_f
            ph1 += (2.0 * math.pi * (f1 + 40.0 * math.exp(-t * 50.0))) / sr_f
            ph2 += (2.0 * math.pi * f2) / sr_f
            s1 = 1.0 if (role == "bell" and "phonk" in low_prompt and math.sin(ph1) > 0) else math.sin(ph1)
            v = (0.65 * s1 + 0.35 * math.sin(ph2)) * math.exp(-t * dec)
            buf_l[i], buf_r[i] = v, v * 0.92

    else:  # fx_stab (harmonic chord stab tuned to requested Key!)
        chord_offsets = (0, 3, 7, 10) if is_minor else (0, 4, 7, 11)
        freqs = [midi_hz(root_midi + 12 + o) for o in chord_offsets]
        phases = [0.0] * len(freqs)
        lp_l = lp_r = 0.0
        for i in range(n):
            t = i / sr_f
            acc_l = acc_r = 0.0
            for idx_f, freq in enumerate(freqs):
                det = 1.0 + (idx_f - 1.5) * 0.0025
                phases[idx_f] += (2.0 * math.pi * freq * det) / sr_f
                saw = ((phases[idx_f] / (2.0 * math.pi)) % 1.0) * 2.0 - 1.0
                pan = (idx_f / max(1, len(freqs) - 1))
                acc_l += saw * (1.2 - pan * 0.5)
                acc_r += saw * (0.7 + pan * 0.5)
            cutoff_alpha = 0.12 + 0.45 * math.exp(-t * 7.5)
            lp_l = lp_l + cutoff_alpha * (acc_l * 0.25 - lp_l)
            lp_r = lp_r + cutoff_alpha * (acc_r * 0.25 - lp_r)
            env = math.exp(-t * 4.2)
            buf_l[i] = math.tanh(1.3 * lp_l) * env
            buf_r[i] = math.tanh(1.3 * lp_r) * env

    return _trim_and_write_stereo_wav(out_wav, buf_l, buf_r, sr, fade_ms=14.0)


# ---------------------------------------------------------------------------
# Drum Kit Orchestrator, Storage, Deploy & Audition
# ---------------------------------------------------------------------------

def _read_kit_summary(kdir: Path) -> Optional[Dict[str, Any]]:
    meta_file = kdir / "kit.json"
    if not meta_file.is_file():
        return None
    try:
        return json.loads(meta_file.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_drum_kits() -> List[Dict[str, Any]]:
    if not KITS_DIR.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for sub in sorted(KITS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if sub.is_dir():
            meta = _read_kit_summary(sub)
            if meta:
                out.append(meta)
    return out


def deploy_drum_kit_to_mpc(
    kit_dir: Path,
    kit_meta: Dict[str, Any],
    host: Optional[str] = None,
    remote_kits_dir: Optional[str] = None,
    ssh_user: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        from . import mpc_flow  # type: ignore
    except ImportError:
        import mpc_flow  # type: ignore
    cfg = mpc_flow.load_config()
    target_host = host or mpc_flow.default_mpc_host()
    user = ssh_user or str(cfg.get("ssh_user") or "root")
    remote_root = (remote_kits_dir or str(cfg.get("remote_kits_dir") or DEFAULT_REMOTE_KITS_DIR)).rstrip("/")
    kit_name = kit_meta["kit_name"]
    remote_kit = f"{remote_root}/{kit_name}"
    remote_tmp = f"{remote_kit}.staging"

    subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", f"{user}@{target_host}",
         f"rm -rf {shlex.quote(remote_tmp)} && mkdir -p {shlex.quote(remote_tmp)} {shlex.quote(remote_root)}"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["scp", "-r", "-o", "ConnectTimeout=5", str(kit_dir) + "/.", f"{user}@{target_host}:{remote_tmp}/"],
        check=True, capture_output=True, text=True,
    )
    latest_payload = {
        "kit_name": kit_name,
        "xpm_path": f"{remote_kit}/{kit_name}.xpm",
        "kit_dir": remote_kit,
        "bpm": kit_meta["bpm"],
        "key": kit_meta["key"],
        "prompt": kit_meta["prompt"],
        "engine": kit_meta["engine"],
        "updated_at": kit_meta["updated_at"],
        "pads": [
            {
                "pad": p["pad"],
                "pad_id": p["pad_id"],
                "short": p["short"],
                "role": p["role"],
                "source": p["source"],
                "wav": p["wav_name"],
                "note": MPC_BANK_A_NOTES[p["pad"] - 1],
            }
            for p in kit_meta["pads"]
        ],
    }
    latest_tmp = kit_dir / ".latest_kit_tmp.json"
    latest_tmp.write_text(json.dumps(latest_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        subprocess.run(
            ["scp", "-o", "ConnectTimeout=5", str(latest_tmp), f"{user}@{target_host}:{remote_root}/latest_kit.json.tmp"],
            check=True, capture_output=True, text=True,
        )
    finally:
        latest_tmp.unlink(missing_ok=True)

    subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", f"{user}@{target_host}",
         f"rm -rf {shlex.quote(remote_kit)} && mv -f {shlex.quote(remote_tmp)} {shlex.quote(remote_kit)} && mv -f {shlex.quote(remote_root + '/latest_kit.json.tmp')} {shlex.quote(remote_root + '/latest_kit.json')}"],
        check=True, capture_output=True, text=True,
    )

    emit_activity("flow", "deploy_kit", f"{kit_name}.xpm (16 pads) -> {target_host}:{remote_kit}", target=f"mpc:{target_host}")
    return {
        "deployed": True,
        "host": target_host,
        "remote_dir": remote_kit,
        "remote_xpm": f"{remote_kit}/{kit_name}.xpm",
        "latest_json": f"{remote_root}/latest_kit.json",
    }


def generate_drum_kit(
    prompt: str,
    kit_name: Optional[str] = None,
    bpm: Optional[float] = None,
    key: Optional[str] = None,
    engine: Optional[str] = None,
    model: Optional[str] = None,
    deploy: Optional[bool] = None,
    host: Optional[str] = None,
    api_key: Optional[str] = None,
    progress_cb: Optional[Callable[[str, int, str], None]] = None,
) -> Dict[str, Any]:
    """Generate a ready-to-play 16-pad Akai MPC Live Drum Program (.xpm + 16 named WAVs)."""
    try:
        from . import mpc_flow  # type: ignore
    except ImportError:
        import mpc_flow  # type: ignore

    def report(stage: str, pct: int, msg: str) -> None:
        if progress_cb:
            try:
                progress_cb(stage, pct, msg)
            except Exception:
                pass

    cfg = mpc_flow.load_config()
    hints = mpc_flow._extract_prompt_hints(prompt, bpm, key)
    use_bpm = float(hints["bpm"])
    use_key = str(hints["key_meta"]["key"])
    chosen_engine = (engine or cfg.get("engine") or "auto").strip()
    if chosen_engine == "auto":
        has_key = bool(api_key or mpc_flow.resolve_gemini_api_key()[0])
        chosen_engine = "gemini_lyria" if has_key else "synth_flow"
    use_deploy = bool(cfg.get("auto_deploy", True) if deploy is None else deploy)

    resolved_name = sanitize_kit_name(prompt, custom_name=kit_name)
    prefix = _wav_prefix_from_kit(resolved_name)
    kit_dir = KITS_DIR / resolved_name

    report("init", 8, f"Preparing kit '{resolved_name}' ({chosen_engine})")
    emit_activity("flow", "drumkit_start", f"{resolved_name} [{chosen_engine}] prompt='{prompt[:48]}'")

    assigned: Dict[int, Dict[str, Any]] = {}
    lyria_notes: List[str] = []
    selected_model = str(model or cfg.get("model") or mpc_flow.DEFAULT_LYRIA_MODEL).strip()

    with tempfile.TemporaryDirectory(prefix="flow_kit_") as td:
        tmp_dir = Path(td)
        if chosen_engine == "gemini_lyria":
            # Execute Lyria RealTime stems sequentially: concurrent WebSockets on one API key stall/time out.
            stem_prompts = [
                ("core", 4, 22, "Lyria Core Drums: kick, snare, clap, 808, tom...",
                 f"{prompt}, dry isolated solo drum kit hits with punchy kick, snare, clap, deep 808, rimshot and tom hits"),
                ("top",  3, 44, "Lyria Top Kit: hi-hats, crash, shaker, stab...",
                 f"{prompt}, dry isolated solo percussion hits with closed hi-hat, open hi-hat, shaker, ride crash cymbal and stab"),
            ]
            stems_out: List[Dict[str, str]] = []
            for idx, (kind, sbars, spct, smsg, sprompt) in enumerate(stem_prompts):
                report("lyria", spct, smsg)
                out_raw = tmp_dir / f"stem_{idx}_{kind}"
                try:
                    res = mpc_flow.call_lyria_realtime(
                        prompt=sprompt,
                        out_raw_path=out_raw,
                        bpm=use_bpm,
                        key=use_key,
                        bars=sbars,
                        model=selected_model,
                        api_key=api_key,
                        timeout_s=28.0,
                    )
                    stems_out.append({"path": str(res["raw_file"]), "stem_kind": kind})
                    lyria_notes.append(str(res.get("notes_text", "")))
                except Exception as exc:
                    if idx == 0 or not stems_out:
                        raise
                    lyria_notes.append(f"top stem skipped: {exc}")

            report("slice", 62, "Slicing & spectral-classifying drum one-shots...")
            cand_dir = tmp_dir / "cands"
            cand_dir.mkdir(parents=True, exist_ok=True)
            candidates = _extract_lyria_candidates(stems_out, cand_dir, sr=SAMPLE_RATE)
            assigned = _assign_candidates_to_slots(candidates)

        report("build", 74, "Building 16 Akai MPC Pad WAVs + Drum Program (.xpm)...")
        kit_dir.mkdir(parents=True, exist_ok=True)
        pads_meta: List[Dict[str, Any]] = []
        lyria_count = 0
        for slot in DRUM_KIT_SLOTS:
            pnum = int(slot["pad"])
            sample_stem = f"{prefix}_{slot['slug']}"
            wav_path = kit_dir / f"{sample_stem}.wav"
            cand = assigned.get(pnum)
            if cand and Path(cand["path"]).is_file():
                shutil.copy2(cand["path"], wav_path)
                n_frames, dur_s = _read_wav_frames_simple(wav_path)
                source_kind = "lyria_hit"
                lyria_count += 1
            else:
                n_frames, dur_s = _synth_drum_slot(slot["role"], hints, pnum, wav_path, sr=SAMPLE_RATE)
                source_kind = "dsp_synth"

            pads_meta.append({
                "pad": pnum,
                "pad_id": slot["pad_id"],
                "role": slot["role"],
                "short": slot["short"],
                "sample_stem": sample_stem,
                "wav_name": f"{sample_stem}.wav",
                "wav_path": str(wav_path),
                "mute_group": slot["mute_group"],
                "mono": slot["mono"],
                "poly": slot["poly"],
                "color": slot["color"],
                "midi_note": MPC_BANK_A_NOTES[pnum - 1],
                "num_frames": n_frames,
                "duration_s": dur_s,
                "source": source_kind,
            })

        xpm_path = kit_dir / f"{resolved_name}.xpm"
        xpm_path.write_text(build_drum_program_xpm(resolved_name, pads_meta), encoding="utf-8")

    kit_meta: Dict[str, Any] = {
        "ok": True,
        "kit_name": resolved_name,
        "prompt": prompt,
        "engine": chosen_engine,
        "style": hints["style"],
        "bpm": round(use_bpm, 2),
        "key": use_key,
        "camelot": hints["key_meta"]["camelot"],
        "lyria_hits": lyria_count,
        "synth_hits": 16 - lyria_count,
        "kit_dir": str(kit_dir),
        "xpm_path": str(xpm_path),
        "notes_text": "; ".join(n for n in lyria_notes if n)[:320],
        "pads": pads_meta,
        "updated_at": round(time.time(), 2),
    }
    (kit_dir / "kit.json").write_text(json.dumps(kit_meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    deploy_res: Optional[Dict[str, Any]] = None
    if use_deploy:
        report("deploy", 88, f"Deploying {resolved_name}.xpm to MPC /media/okcomputer/FlowKits...")
        deploy_res = deploy_drum_kit_to_mpc(kit_dir, kit_meta, host=host)
    kit_meta["deploy"] = deploy_res

    report("done", 100, f"Ready: {resolved_name}.xpm ({lyria_count} Lyria + {16 - lyria_count} Synth hits)")
    emit_activity("flow", "drumkit_done", f"{resolved_name}.xpm ({lyria_count}/16 Lyria) @ {use_bpm} BPM {use_key}")
    return kit_meta


def _read_wav_frames_simple(wav_path: Path) -> Tuple[int, float]:
    with wave.open(str(wav_path), "rb") as wf:
        sr = wf.getframerate() or SAMPLE_RATE
        nf = wf.getnframes()
        return nf, round(nf / float(sr), 4)


def audition_kit_pad(
    kit_name: Optional[str] = None,
    pad: int = 1,
    trigger_mpc: bool = True,
    play_mac: bool = False,
    host: Optional[str] = None,
) -> Dict[str, Any]:
    """Audition a Drum Kit pad via MPC MIDI note injection and/or Mac audio preview."""
    pnum = max(1, min(16, int(pad)))
    kits = list_drum_kits()
    target_kit: Optional[Dict[str, Any]] = None
    if kit_name:
        target_kit = _read_kit_summary(KITS_DIR / kit_name)
    elif kits:
        target_kit = kits[0]

    wav_played = False
    wav_path = ""
    if target_kit:
        for p in target_kit.get("pads", []):
            if int(p["pad"]) == pnum:
                wav_path = str(p.get("wav_path", ""))
                break
        if play_mac and wav_path and Path(wav_path).is_file():
            afplay = shutil.which("afplay")
            if afplay:
                subprocess.Popen([afplay, wav_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                wav_played = True

    mpc_res: Optional[Dict[str, Any]] = None
    if trigger_mpc:
        try:
            client = MpcBridgeClient(host=host, source="flow_audition")
            mpc_res = client.trigger_pads([pnum], velocity=115, duration_ms=110, bank="A")
        except Exception as exc:
            mpc_res = {"error": str(exc)}

    return {
        "ok": True,
        "kit_name": target_kit.get("kit_name") if target_kit else None,
        "pad": pnum,
        "wav_played": wav_played,
        "wav_path": wav_path,
        "mpc": mpc_res,
    }
