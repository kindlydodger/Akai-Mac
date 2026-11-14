#!/usr/bin/env python3
"""Pure Python 3 stdio MCP Server for controlling Akai MPC Live physical hardware."""

from __future__ import annotations

import base64
import json
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List

from mpc_audio import analyze_audio, chop_audio, detect_backends, stretch_audio
from mpc_client import MpcBridgeClient, emit_activity
from mpc_library import fetch_status as library_fetch_status
from mpc_library import resolve_pending_host_decode, search_library
from mpc_library import sync_from_mpc as library_sync_from_mpc
from mpc_flow import (
    generate_sample_bank as flow_generate_bank,
    get_flow_status as flow_get_status,
    list_banks as flow_list_banks,
    save_config as flow_save_config,
    save_gemini_api_key as flow_save_api_key,
    select_bank as flow_select_bank,
)
from mpc_drumkit import generate_drum_kit as flow_generate_drum_kit, list_drum_kits as flow_list_drum_kits
from mpc_midi_pattern import BUILTIN_PRESETS, deploy_midi_to_mpc, write_midi_pattern
from mpc_webhook_server import (
    DEFAULT_BIND as WEBHOOK_BIND,
    DEFAULT_PORT as WEBHOOK_PORT,
    IacMidiRouter,
    get_webhook_health,
    normalize_midi_messages,
    send_test_webhook,
)


SERVER_INFO = {"name": "mpc-live-hardware", "version": "1.0.0"}
SUPPORTED_PROTOCOLS = ("2025-03-26", "2024-11-05")


def _json_text(obj: Any) -> List[Dict[str, Any]]:
    return [{"type": "text", "text": json.dumps(obj, indent=2, ensure_ascii=False)}]


def _build_tools(client: MpcBridgeClient) -> Dict[str, Dict[str, Any]]:
    def tool_status(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        return _json_text(client.status(check_screen=bool(args.get("check_screen", True))))

    def tool_list_controls(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        client.map.reload()
        return _json_text(client.map.summary())

    def tool_button(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        res = client.press_button(
            name=args["name"],
            action=args.get("action", "tap"),
            hold_ms=int(args.get("hold_ms", 60)),
            release_mode=args.get("release_mode", "90_00"),
        )
        return _json_text(res)

    def tool_combo(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        res = client.combo(
            modifier=args.get("modifier", "SHIFT"),
            target=args["target"],
            hold_ms=int(args.get("hold_ms", 60)),
            release_mode=args.get("release_mode", "90_00"),
        )
        return _json_text(res)

    def tool_pad(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        pads = args.get("pads") if "pads" in args else [args["pad"]]
        res = client.trigger_pads(
            pads=pads,
            velocity=int(args.get("velocity", 100)),
            duration_ms=int(args.get("duration_ms", 100)),
            aftertouch=args.get("aftertouch"),
            bank=args.get("bank"),
            gap_ms=int(args.get("gap_ms", 0)),
        )
        return _json_text(res)

    def tool_dial(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        res = client.turn_dial(
            steps=int(args.get("steps", 1)),
            press=bool(args.get("press", False)),
            step_delay_ms=int(args.get("step_delay_ms", 20)),
        )
        return _json_text(res)

    def tool_qlink(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        res = client.qlink(
            knob=int(args["knob"]),
            delta=args.get("delta"),
            value=args.get("value"),
            touch=args.get("touch"),
        )
        return _json_text(res)

    def tool_sequence(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        res = client.play_sequence(
            steps=list(args["steps"]),
            default_step_ms=int(args.get("default_step_ms", 125)),
        )
        return _json_text(res)

    def tool_raw_midi(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        res = client.raw_midi(
            hex_bytes=args["hex"],
            allow_unsafe=bool(args.get("allow_unsafe", False)),
        )
        return _json_text(res)

    def tool_events(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        res = client.events(
            since_seq=int(args.get("since_seq", 0)),
            sniff_leds=args.get("sniff_leds"),
            clear_first=bool(args.get("clear_first", False)),
            include_noise=bool(args.get("include_noise", False)),
        )
        return _json_text(res)

    def tool_learn(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        res = client.learn_control(
            name=args["name"],
            timeout_s=float(args.get("timeout_s", 10.0)),
        )
        return _json_text(res)

    def tool_screen(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        half = bool(args.get("half", True))
        save_to = Path(args["save_path"]) if args.get("save_path") else None
        png = client.screenshot(half=half, save_path=save_to)
        content: List[Dict[str, Any]] = [
            {"type": "image", "data": base64.b64encode(png).decode("ascii"), "mimeType": "image/png"},
            {"type": "text", "text": json.dumps({"bytes": len(png), "half": half, "saved": str(save_to) if save_to else None})},
        ]
        return content

    def tool_tap(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        pause_ms = int(args.get("pause_ms", 140))
        if "to_x" in args and "to_y" in args:
            return _json_text(client.drag(
                int(args["x"]), int(args["y"]), int(args["to_x"]), int(args["to_y"]),
                duration_ms=int(args.get("duration_ms", 200)),
            ))
        if args.get("target"):
            return _json_text(client.ui_tap(str(args["target"]), pause_ms=pause_ms))
        return _json_text(client.tap(int(args["x"]), int(args["y"]), pause_ms=pause_ms))

    def tool_ui_macro(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        macro = str(args["macro"])
        if macro == "clear_events":
            return _json_text(client.clear_sequence_events())
        if macro == "open_browser":
            return _json_text(client.open_browser(select_okcomputer=bool(args.get("okcomputer", True))))
        if macro == "step_seq":
            preset = args.get("preset")
            pads_map = BUILTIN_PRESETS[preset]["pads"] if preset else args["pads_map"]
            return _json_text(client.program_step_seq(
                pads_map=pads_map,
                start_pad=int(args.get("start_pad", 1)),
                bars=int(args.get("bars", 4)),
            ))
        raise ValueError(f"Unknown macro '{macro}'")

    def tool_midi_pattern(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        preset = args.get("preset")
        spec = BUILTIN_PRESETS[preset] if preset else args.get("spec")
        if not isinstance(spec, dict):
            raise ValueError("Provide either 'preset' or 'spec' object")
        safe_name = "".join(c if (c.isalnum() or c in "_-") else "_" for c in str(spec.get("name", "pattern")))
        out_path = Path(args["out_path"]) if args.get("out_path") else (Path(__file__).resolve().parent / "patterns" / f"{safe_name}.mid")
        meta = write_midi_pattern(spec, out_path, mpc_map=client.map)
        res: Dict[str, Any] = {"midi": meta}
        if args.get("deploy", False):
            remote_dir = str(args.get("remote_dir") or client.map.data.get("meta", {}).get("midi_patterns_dir", "/media/okcomputer/MidiPatterns"))
            res.update(deploy_midi_to_mpc(out_path, client.host, remote_dir=remote_dir))
        return _json_text(res)

    def tool_audio_backends(_args: Dict[str, Any]) -> List[Dict[str, Any]]:
        return _json_text(detect_backends())

    def tool_audio_analyze(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        res = analyze_audio(
            Path(args["path"]),
            beat_backend=str(args.get("beat_backend", "auto")),
            key_backend=str(args.get("key_backend", "ensemble")),
            min_bpm=float(args.get("min_bpm", 55.0)),
            max_bpm=float(args.get("max_bpm", 175.0)),
        )
        if not bool(args.get("include_arrays", True)):
            res.pop("beats_s", None)
            res.pop("onsets_s", None)
        return _json_text(res)

    def tool_audio_stretch(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        res = stretch_audio(
            input_path=Path(args["input_path"]),
            output_path=Path(args["output_path"]) if args.get("output_path") else None,
            backend=str(args.get("backend", "rubberband-r3")),
            tempo_mult=float(args["tempo_mult"]) if args.get("tempo_mult") is not None else None,
            source_bpm=float(args["source_bpm"]) if args.get("source_bpm") is not None else None,
            target_bpm=float(args["target_bpm"]) if args.get("target_bpm") is not None else None,
            pitch_semitones=float(args["pitch_semitones"]) if args.get("pitch_semitones") is not None else None,
            source_key=args.get("source_key"),
            target_key=args.get("target_key"),
            formant=bool(args.get("formant", False)),
            crisp=int(args["crisp"]) if args.get("crisp") is not None else None,
            sample_rate=int(args.get("sample_rate", 44100)),
            bit_depth=int(args.get("bit_depth", 16)),
            deploy=bool(args.get("deploy", False)),
            host=client.host,
            remote_dir=str(args.get("remote_dir") or "/media/okcomputer/MPCUTER_Audio"),
        )
        return _json_text(res)

    def tool_audio_chop(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        res = chop_audio(
            input_path=Path(args["input_path"]),
            out_dir=Path(args["out_dir"]) if args.get("out_dir") else None,
            mode=str(args.get("mode", "onsets")),
            backend=str(args.get("backend", "auto")),
            slices=int(args.get("slices", 16)),
            min_gap_ms=int(args.get("min_gap_ms", 90)),
            fade_ms=float(args.get("fade_ms", 3.0)),
            bit_depth=int(args.get("bit_depth", 16)),
            deploy=bool(args.get("deploy", False)),
            host=client.host,
            remote_dir=str(args.get("remote_dir") or "/media/okcomputer/MPCUTER_Audio"),
        )
        return _json_text(res)

    def tool_webhook_status(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        url = str(args.get("url") or f"http://{WEBHOOK_BIND}:{WEBHOOK_PORT}/health")
        health = get_webhook_health(url)
        try:
            endpoints = IacMidiRouter().list_endpoints()
        except Exception as exc:
            endpoints = {"error": str(exc)}
        return _json_text({"webhook": health, "coremidi": endpoints})

    def tool_webhook_send(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        url = str(args.get("url") or f"http://{WEBHOOK_BIND}:{WEBHOOK_PORT}/webhook")
        payload = dict(args.get("payload")) if isinstance(args.get("payload"), dict) else {}
        if args.get("message") is not None:
            payload["message"] = str(args["message"])
        if args.get("midi") is not None:
            payload["midi"] = args["midi"]
        elif any(k in args for k in ("note", "cc", "hex")):
            msg: Dict[str, Any] = {"channel": int(args.get("channel", 1))}
            if args.get("hex"):
                msg["hex"] = str(args["hex"])
            elif args.get("note") is not None:
                msg.update({
                    "type": "note_on",
                    "note": args["note"],
                    "velocity": int(args.get("velocity", 100)),
                    "duration_ms": float(args.get("duration_ms", 150.0)),
                })
            elif args.get("cc") is not None:
                msg.update({
                    "type": "cc",
                    "cc": int(args["cc"]),
                    "value": int(args.get("value", 64)),
                })
            payload["midi"] = [msg]
        return _json_text(send_test_webhook(url, payload))

    def tool_iac_midi_send(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        router = IacMidiRouter(dest_pattern=str(args.get("dest") or "IAC"))
        try:
            router.ensure_ready()
            events = normalize_midi_messages(args)
            if not events:
                raise ValueError("Geçerli MIDI verisi bulunamadı (note, cc, hex, bytes veya midi gönderin)")
            results = []
            for ev in events:
                res = router.send_bytes(ev["bytes"])
                results.append({"kind": ev["kind"], "hex": ev["hex"], **res})
            hex_summary = ", ".join(r["hex"] for r in results[:4])
            emit_activity("mcp", "iac_midi", hex_summary, target=f"iac:{router.destination_name or 'Bus1'}")
            return _json_text({"ok": True, "destination": router.destination_name, "sent": len(results), "results": results})
        finally:
            router.close()

    def tool_library_status(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        host = str(args.get("host") or client.host)
        if bool(args.get("sync", False)):
            library_sync_from_mpc(host=host, include_jsonl=bool(args.get("include_jsonl", True)))
        return _json_text(library_fetch_status(host=host))

    def tool_library_search(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        if bool(args.get("sync_first", False)):
            library_sync_from_mpc(host=str(args.get("host") or client.host), include_jsonl=True)
        res = search_library(
            query=args.get("query"),
            bpm=float(args["bpm"]) if args.get("bpm") is not None else None,
            bpm_tol=float(args.get("bpm_tol", 3.0)),
            half_double=bool(args.get("half_double", True)),
            key=args.get("key"),
            harmonic=bool(args.get("harmonic", True)),
            category=args.get("category"),
            drive=args.get("drive"),
            limit=int(args.get("limit", 30)),
        )
        return _json_text(res)

    def tool_library_resolve_pending(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        res = resolve_pending_host_decode(
            host=str(args.get("host") or client.host),
            oneshot_max_s=float(args.get("oneshot_max_s", 3.0)),
            limit=int(args.get("limit", 0)),
            sync_first=bool(args.get("sync_first", True)),
        )
        return _json_text(res)

    def tool_flow_generate(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        res = flow_generate_bank(
            prompt=str(args["prompt"]),
            slot=int(args.get("slot", 1)),
            bpm=float(args["bpm"]) if args.get("bpm") is not None else None,
            key=str(args["key"]) if args.get("key") else None,
            bars=int(args["bars"]) if args.get("bars") is not None else None,
            engine=str(args["engine"]) if args.get("engine") else None,
            model=str(args["model"]) if args.get("model") else None,
            slice_mode=str(args["slice_mode"]) if args.get("slice_mode") else None,
            deploy=bool(args.get("deploy", True)),
            host=str(args.get("host") or client.host),
        )
        return _json_text(res)

    def tool_flow_drumkit(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        res = flow_generate_drum_kit(
            prompt=str(args["prompt"]),
            kit_name=str(args["kit_name"]) if args.get("kit_name") else None,
            bpm=float(args["bpm"]) if args.get("bpm") is not None else None,
            key=str(args["key"]) if args.get("key") else None,
            engine=str(args["engine"]) if args.get("engine") else None,
            model=str(args["model"]) if args.get("model") else None,
            deploy=bool(args.get("deploy", True)),
            host=str(args.get("host") or client.host),
        )
        return _json_text(res)

    def tool_flow_banks(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        if args.get("select_slot") is not None:
            res = flow_select_bank(
                slot=int(args["select_slot"]),
                deploy=bool(args.get("deploy", True)),
                host=str(args.get("host") or client.host),
            )
            return _json_text(res)
        return _json_text({"ok": True, "banks": flow_list_banks()})

    def tool_flow_config(args: Dict[str, Any]) -> List[Dict[str, Any]]:
        updates: Dict[str, Any] = {}
        for k in ("engine", "model", "default_bpm", "default_key", "default_bars", "slice_mode", "match_bpm", "match_key", "auto_deploy", "mpc_host"):
            if k in args and args[k] is not None:
                updates[k] = args[k]
        if updates:
            flow_save_config(updates)
        if args.get("clear_api_key"):
            flow_save_api_key("")
        elif args.get("api_key"):
            flow_save_api_key(str(args["api_key"]))
        return _json_text(flow_get_status())

    specs = [
        (
            "mpc_status",
            "Check connection health to Akai MPC Live hardware bridge (:6731) and optional screen server (:6720).",
            {
                "type": "object",
                "properties": {"check_screen": {"type": "boolean", "default": True}},
                "additionalProperties": False,
            },
            tool_status,
        ),
        (
            "mpc_list_controls",
            "List all mapped MPC Live physical buttons, aliases, Shift combos, pads (1..16), Data Dial, Q-Links, and learned controls.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            tool_list_controls,
        ),
        (
            "mpc_button",
            "Press, hold, or release any physical MPC Live front-panel button (e.g. PLAY, STOP, REC, OVERDUB, MAIN, MENU, BROWSE, STEP_SEQ, TRACK_MIXER, PAD_MIXER, BANK_A..D, BANK_E..H, UNDO, REDO, MINUS, PLUS, UP, DOWN, LEFT, RIGHT, SHIFT).",
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Button or combo name, e.g. PLAY, STOP, MAIN, MENU, BANK_B, REDO."},
                    "action": {"type": "string", "enum": ["tap", "down", "hold", "up", "release"], "default": "tap"},
                    "hold_ms": {"type": "integer", "minimum": 10, "maximum": 10000, "default": 60},
                    "release_mode": {"type": "string", "enum": ["90_00", "80_40"], "default": "90_00"},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            tool_button,
        ),
        (
            "mpc_combo",
            "Hold a modifier button (usually SHIFT) while tapping a target button (e.g. modifier=SHIFT, target=BANK_A for Bank E, or target=UNDO for Redo).",
            {
                "type": "object",
                "properties": {
                    "modifier": {"type": "string", "default": "SHIFT"},
                    "target": {"type": "string"},
                    "hold_ms": {"type": "integer", "minimum": 20, "maximum": 5000, "default": 60},
                    "release_mode": {"type": "string", "enum": ["90_00", "80_40"], "default": "90_00"},
                },
                "required": ["target"],
                "additionalProperties": False,
            },
            tool_combo,
        ),
        (
            "mpc_pad",
            "Trigger one or more physical MPC Live pads (1..16) with configurable velocity, hold duration, optional polyphonic aftertouch pressure, and optional bank (A..H).",
            {
                "type": "object",
                "properties": {
                    "pad": {"type": "integer", "minimum": 1, "maximum": 16, "description": "Single pad number 1..16 (1=bottom-left, 16=top-right)."},
                    "pads": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 16}, "minItems": 1, "maxItems": 16},
                    "velocity": {"type": "integer", "minimum": 1, "maximum": 127, "default": 100},
                    "duration_ms": {"type": "integer", "minimum": 10, "maximum": 10000, "default": 100},
                    "aftertouch": {"type": "integer", "minimum": 0, "maximum": 127},
                    "bank": {"type": "string", "enum": ["A", "B", "C", "D", "E", "F", "G", "H"]},
                    "gap_ms": {"type": "integer", "minimum": 0, "maximum": 5000, "default": 0, "description": "0 plays pads together as a chord; >0 arpeggiates with gap_ms between hits."},
                },
                "additionalProperties": False,
            },
            tool_pad,
        ),
        (
            "mpc_dial",
            "Rotate the MPC Live Data Dial clockwise (+) or counter-clockwise (-) and optionally push/enter it.",
            {
                "type": "object",
                "properties": {
                    "steps": {"type": "integer", "minimum": -200, "maximum": 200, "default": 1},
                    "press": {"type": "boolean", "default": False},
                    "step_delay_ms": {"type": "integer", "minimum": 0, "maximum": 500, "default": 20},
                },
                "additionalProperties": False,
            },
            tool_dial,
        ),
        (
            "mpc_qlink",
            "Adjust Q-Link knob 1..4 (uses learned CC/touch map in mpc_live_map.json; teach with mpc_learn if unmapped).",
            {
                "type": "object",
                "properties": {
                    "knob": {"type": "integer", "minimum": 1, "maximum": 4},
                    "delta": {"type": "integer", "minimum": -127, "maximum": 127},
                    "value": {"type": "integer", "minimum": 0, "maximum": 127},
                    "touch": {"type": "boolean"},
                },
                "required": ["knob"],
                "additionalProperties": False,
            },
            tool_qlink,
        ),
        (
            "mpc_sequence",
            "Execute a timed sequence of pad hits, button presses, dial turns, combos, or rests.",
            {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "pad": {"type": "integer", "minimum": 1, "maximum": 16},
                                "pads": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 16}},
                                "velocity": {"type": "integer", "minimum": 1, "maximum": 127},
                                "duration_ms": {"type": "integer", "minimum": 10, "maximum": 5000},
                                "aftertouch": {"type": "integer", "minimum": 0, "maximum": 127},
                                "bank": {"type": "string"},
                                "button": {"type": "string"},
                                "action": {"type": "string"},
                                "hold_ms": {"type": "integer"},
                                "combo": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 2},
                                "dial": {"type": "integer"},
                                "press": {"type": "boolean"},
                                "rest_ms": {"type": "integer", "minimum": 0, "maximum": 10000},
                                "wait_ms": {"type": "integer", "minimum": 0, "maximum": 10000},
                            },
                        },
                        "minItems": 1,
                    },
                    "default_step_ms": {"type": "integer", "minimum": 0, "maximum": 10000, "default": 125},
                },
                "required": ["steps"],
                "additionalProperties": False,
            },
            tool_sequence,
        ),
        (
            "mpc_raw_midi",
            "Inject raw hex MIDI bytes directly into hw:1,0,1 (MPC Private input).",
            {
                "type": "object",
                "properties": {
                    "hex": {"type": "string", "description": "Hex MIDI bytes, e.g. '90 52 7F' or 'B0 64 01'."},
                    "allow_unsafe": {"type": "boolean", "default": False},
                },
                "required": ["hex"],
                "additionalProperties": False,
            },
            tool_raw_midi,
        ),
        (
            "mpc_events",
            "Read captured hardware MIDI events (RX from physical controls, plus optional TX LEDs when sniff_leds=true).",
            {
                "type": "object",
                "properties": {
                    "since_seq": {"type": "integer", "minimum": 0, "default": 0},
                    "sniff_leds": {"type": "boolean"},
                    "clear_first": {"type": "boolean", "default": False},
                    "include_noise": {"type": "boolean", "default": False},
                },
                "additionalProperties": False,
            },
            tool_events,
        ),
        (
            "mpc_learn",
            "Listen for the next physical button, pad, or knob event pressed/turned on the MPC Live hardware and save it under 'name' in mpc_live_map.json.",
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Control name to save, e.g. QLINK_1 or CUSTOM_BTN."},
                    "timeout_s": {"type": "number", "minimum": 1, "maximum": 30, "default": 10},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            tool_learn,
        ),
        (
            "mpc_screen",
            "Capture a live PNG screenshot from MPC Live (:6720/screen) to visually verify state.",
            {
                "type": "object",
                "properties": {
                    "half": {"type": "boolean", "default": True},
                    "save_path": {"type": "string"},
                },
                "additionalProperties": False,
            },
            tool_screen,
        ),
        (
            "mpc_tap",
            "Tap or drag on the MPC Live 1280x800 touchscreen via :6720, or click a calibrated named UI element (e.g. target='step_seq.tc_btn').",
            {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Named coordinate target from mpc_live_map ui (e.g. main.browser_icon, step_seq.tc_btn)."},
                    "x": {"type": "integer", "minimum": 0, "maximum": 1279},
                    "y": {"type": "integer", "minimum": 0, "maximum": 799},
                    "to_x": {"type": "integer", "minimum": 0, "maximum": 1279},
                    "to_y": {"type": "integer", "minimum": 0, "maximum": 799},
                    "duration_ms": {"type": "integer", "minimum": 20, "maximum": 3000, "default": 200},
                    "pause_ms": {"type": "integer", "minimum": 0, "maximum": 2000, "default": 140},
                },
                "additionalProperties": False,
            },
            tool_tap,
        ),
        (
            "mpc_ui_macro",
            "Execute calibrated MPC 3.x UI workflows (clear_events, open_browser, or batch step_seq programming).",
            {
                "type": "object",
                "properties": {
                    "macro": {"type": "string", "enum": ["clear_events", "open_browser", "step_seq"]},
                    "okcomputer": {"type": "boolean", "default": True},
                    "preset": {"type": "string", "enum": ["dj_premier_fm", "dilla_swung_boom"]},
                    "pads_map": {"type": "object"},
                    "start_pad": {"type": "integer", "minimum": 1, "maximum": 16, "default": 1},
                    "bars": {"type": "integer", "minimum": 1, "maximum": 16, "default": 4},
                },
                "required": ["macro"],
                "additionalProperties": False,
            },
            tool_ui_macro,
        ),
        (
            "mpc_midi_pattern",
            "Generate a crisp 960-PPQN Standard MIDI File (.mid) with MPC swing/micro-timing and optionally SCP-deploy it directly to /media/okcomputer/MidiPatterns.",
            {
                "type": "object",
                "properties": {
                    "preset": {"type": "string", "enum": ["dj_premier_fm", "dilla_swung_boom"]},
                    "spec": {"type": "object"},
                    "out_path": {"type": "string"},
                    "deploy": {"type": "boolean", "default": False},
                    "remote_dir": {"type": "string"},
                },
                "additionalProperties": False,
            },
            tool_midi_pattern,
        ),
        (
            "mpc_audio_backends",
            "List installed open-source audio DSP backends (Rubber Band R3/R2, aubio, libkeyfinder, SoundTouch, SoX, FFmpeg, librosa).",
            {"type": "object", "properties": {}, "additionalProperties": False},
            tool_audio_backends,
        ),
        (
            "mpc_audio_analyze",
            "Analyze BPM, beat grid, onset transients, musical key, and Camelot wheel code using ensemble MIR backends.",
            {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {"type": "string"},
                    "beat_backend": {"type": "string", "enum": ["auto", "librosa", "aubio", "soundstretch"], "default": "auto"},
                    "key_backend": {"type": "string", "enum": ["ensemble", "keyfinder", "librosa"], "default": "ensemble"},
                    "min_bpm": {"type": "number", "default": 55.0},
                    "max_bpm": {"type": "number", "default": 175.0},
                    "include_arrays": {"type": "boolean", "default": True},
                },
                "additionalProperties": False,
            },
            tool_audio_analyze,
        ),
        (
            "mpc_audio_stretch",
            "Time-stretch and/or pitch-shift an audio file to match BPM and/or musical key (Rubber Band R3/R2, SoundTouch, FFmpeg), outputting MPC-ready WAV and optionally SCP-deploying.",
            {
                "type": "object",
                "required": ["input_path"],
                "properties": {
                    "input_path": {"type": "string"},
                    "output_path": {"type": "string"},
                    "backend": {"type": "string", "enum": ["rubberband-r3", "rubberband", "soundstretch", "ffmpeg"], "default": "rubberband-r3"},
                    "tempo_mult": {"type": "number"},
                    "source_bpm": {"type": "number"},
                    "target_bpm": {"type": "number"},
                    "pitch_semitones": {"type": "number"},
                    "source_key": {"type": "string"},
                    "target_key": {"type": "string"},
                    "formant": {"type": "boolean", "default": False},
                    "crisp": {"type": "integer"},
                    "sample_rate": {"type": "integer", "default": 44100},
                    "bit_depth": {"type": "integer", "enum": [16, 24], "default": 16},
                    "deploy": {"type": "boolean", "default": False},
                    "remote_dir": {"type": "string"},
                },
                "additionalProperties": False,
            },
            tool_audio_stretch,
        ),
        (
            "mpc_audio_chop",
            "Chop a sample by onsets, beats, or equal grid slices with de-click fades, producing pad WAVs + manifest.json and optionally SCP-deploying to MPC.",
            {
                "type": "object",
                "required": ["input_path"],
                "properties": {
                    "input_path": {"type": "string"},
                    "out_dir": {"type": "string"},
                    "mode": {"type": "string", "enum": ["onsets", "beats", "grid"], "default": "onsets"},
                    "backend": {"type": "string", "enum": ["auto", "librosa", "aubio", "soundstretch"], "default": "auto"},
                    "slices": {"type": "integer", "default": 16},
                    "min_gap_ms": {"type": "integer", "default": 90},
                    "fade_ms": {"type": "number", "default": 3.0},
                    "bit_depth": {"type": "integer", "enum": [16, 24], "default": 16},
                    "deploy": {"type": "boolean", "default": False},
                    "remote_dir": {"type": "string"},
                },
                "additionalProperties": False,
            },
            tool_audio_chop,
        ),
        (
            "mpc_webhook_status",
            "Check local webhook server health (:6780), watched inbox stats, and macOS CoreMIDI IAC Driver Bus 1 ports.",
            {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "additionalProperties": False,
            },
            tool_webhook_status,
        ),
        (
            "mpc_webhook_send",
            "Send a message and/or MIDI payload to the local webhook server so it is archived to webhook_inbox, logged to AntiGravity activity feed, and routed to IAC Driver Bus 1.",
            {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "message": {"type": "string"},
                    "payload": {"type": "object"},
                    "midi": {},
                    "note": {},
                    "velocity": {"type": "integer", "default": 100},
                    "duration_ms": {"type": "number", "default": 150.0},
                    "cc": {"type": "integer"},
                    "value": {"type": "integer"},
                    "channel": {"type": "integer", "default": 1},
                    "hex": {"type": "string"},
                },
                "additionalProperties": False,
            },
            tool_webhook_send,
        ),
        (
            "mpc_iac_midi_send",
            "Send raw or structured MIDI messages directly to macOS CoreMIDI IAC Driver Bus 1 (or matching CoreMIDI destination).",
            {
                "type": "object",
                "properties": {
                    "dest": {"type": "string", "default": "IAC"},
                    "midi": {},
                    "note": {},
                    "velocity": {"type": "integer", "default": 100},
                    "cc": {"type": "integer"},
                    "value": {"type": "integer"},
                    "program": {"type": "integer"},
                    "channel": {"type": "integer", "default": 1},
                    "hex": {"type": "string"},
                    "bytes": {"type": "array", "items": {"type": "integer"}},
                },
                "additionalProperties": True,
            },
            tool_iac_midi_send,
        ),
        (
            "mpc_library_status",
            "Read live MPC multi-drive sample indexing progress, category/BPM/key breakdown stats, and systemd watcher status (optionally sync library JSONL/CSV to Mac).",
            {
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "sync": {"type": "boolean", "default": False},
                    "include_jsonl": {"type": "boolean", "default": True},
                },
                "additionalProperties": False,
            },
            tool_library_status,
        ),
        (
            "mpc_library_search",
            "Search the indexed MPC sample library by text, BPM (with half/double-time matching), Camelot harmonic key compatibility, category (one_shot, loop, phrase, track), or drive.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "bpm": {"type": "number"},
                    "bpm_tol": {"type": "number", "default": 3.0},
                    "half_double": {"type": "boolean", "default": True},
                    "key": {"type": "string", "description": "Key name or Camelot code, e.g. 'Am', 'F# minor', '8A'."},
                    "harmonic": {"type": "boolean", "default": True},
                    "category": {"type": "string", "enum": ["one_shot", "ultra_short", "loop", "phrase", "track"]},
                    "drive": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 30},
                    "sync_first": {"type": "boolean", "default": False},
                    "host": {"type": "string"},
                },
                "additionalProperties": False,
            },
            tool_library_search,
        ),
        (
            "mpc_library_resolve_pending",
            "Decode and analyze pending compressed samples (.flac/.ogg/.m4a) from MPC on the Mac host and push host_resolved.jsonl back to MPC.",
            {
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 0, "default": 0},
                    "oneshot_max_s": {"type": "number", "default": 3.0},
                    "sync_first": {"type": "boolean", "default": True},
                },
                "additionalProperties": False,
            },
            tool_library_resolve_pending,
        ),
        (
            "mpc_flow_generate",
            "Generate a musical loop from text prompt via Gemini Lyria 3 or offline SynthFlow, analyze BPM/Key, slice into 16 regions, and push directly into the FLOW AI sample banks on MPC Live.",
            {
                "type": "object",
                "required": ["prompt"],
                "properties": {
                    "prompt": {"type": "string"},
                    "slot": {"type": "integer", "minimum": 1, "maximum": 16, "default": 1},
                    "bpm": {"type": "number"},
                    "key": {"type": "string"},
                    "bars": {"type": "integer", "minimum": 1, "maximum": 16},
                    "engine": {"type": "string", "enum": ["auto", "gemini_lyria", "synth_flow"]},
                    "model": {"type": "string", "enum": ["lyria-realtime-exp", "lyria-3-clip-preview", "lyria-3.5"]},
                    "slice_mode": {"type": "string", "enum": ["onsets", "beats", "grid"]},
                    "deploy": {"type": "boolean", "default": True},
                    "host": {"type": "string"},
                },
                "additionalProperties": False,
            },
            tool_flow_generate,
        ),
        (
            "mpc_flow_drumkit",
            "Generate a ready-to-play 16-pad Akai MPC Live Drum Program (.xpm + 16 named WAV one-shots with hi-hat mute groups & pad colors) from a text prompt and deploy to /media/okcomputer/FlowKits.",
            {
                "type": "object",
                "required": ["prompt"],
                "properties": {
                    "prompt": {"type": "string"},
                    "kit_name": {"type": "string"},
                    "bpm": {"type": "number"},
                    "key": {"type": "string"},
                    "engine": {"type": "string", "enum": ["auto", "gemini_lyria", "synth_flow"]},
                    "model": {"type": "string"},
                    "deploy": {"type": "boolean", "default": True},
                    "host": {"type": "string"},
                },
                "additionalProperties": False,
            },
            tool_flow_drumkit,
        ),
        (
            "mpc_flow_banks",
            "List populated FLOW AI sample banks (1..16) or switch the active FLOW AI bank remotely on MPC Live.",
            {
                "type": "object",
                "properties": {
                    "select_slot": {"type": "integer", "minimum": 1, "maximum": 16},
                    "deploy": {"type": "boolean", "default": True},
                    "host": {"type": "string"},
                },
                "additionalProperties": False,
            },
            tool_flow_banks,
        ),
        (
            "mpc_flow_config",
            "Inspect or update Google Flow / FLOW AI bridge configuration (engine, model, default BPM/Key, Gemini API key).",
            {
                "type": "object",
                "properties": {
                    "engine": {"type": "string", "enum": ["auto", "gemini_lyria", "synth_flow", "flow_drop"]},
                    "model": {"type": "string"},
                    "api_key": {"type": "string"},
                    "clear_api_key": {"type": "boolean"},
                    "default_bpm": {"type": "number"},
                    "default_key": {"type": "string"},
                    "default_bars": {"type": "integer"},
                    "slice_mode": {"type": "string", "enum": ["onsets", "beats", "grid"]},
                    "match_bpm": {"type": "boolean"},
                    "match_key": {"type": "boolean"},
                    "auto_deploy": {"type": "boolean"},
                    "mpc_host": {"type": "string"},
                },
                "additionalProperties": False,
            },
            tool_flow_config,
        ),
    ]

    return {
        name: {"definition": {"name": name, "description": desc, "inputSchema": schema}, "handler": fn}
        for name, desc, schema, fn in specs
    }


def _write_msg(payload: Dict[str, Any]) -> None:
    line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
    sys.stdout.buffer.write(line.encode("utf-8"))
    sys.stdout.buffer.flush()


def _error_resp(req_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def main() -> int:
    client = MpcBridgeClient(source="mcp")
    tools = _build_tools(client)

    while True:
        raw_line = sys.stdin.buffer.readline()
        if not raw_line:
            break
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue

        req_id = None
        try:
            msg = json.loads(line)
            req_id = msg.get("id")
            method = msg.get("method")
            params = msg.get("params") or {}

            if method == "initialize":
                req_ver = params.get("protocolVersion")
                proto = req_ver if req_ver in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
                _write_msg({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": proto,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": SERVER_INFO,
                    },
                })
            elif method in ("notifications/initialized", "notifications/cancelled"):
                continue
            elif method == "ping":
                _write_msg({"jsonrpc": "2.0", "id": req_id, "result": {}})
            elif method == "tools/list":
                _write_msg({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"tools": [t["definition"] for t in tools.values()]},
                })
            elif method == "tools/call":
                tname = params.get("name")
                targs = params.get("arguments") or {}
                if tname not in tools:
                    if req_id is not None:
                        _write_msg(_error_resp(req_id, -32601, f"Unknown tool: {tname}"))
                    continue
                try:
                    content = tools[tname]["handler"](targs)
                    is_err = False
                except Exception as exc:
                    content = _json_text({"error": str(exc)})
                    is_err = True
                if req_id is not None:
                    _write_msg({"jsonrpc": "2.0", "id": req_id, "result": {"content": content, "isError": is_err}})
            elif req_id is not None:
                _write_msg(_error_resp(req_id, -32601, f"Method not found: {method}"))
        except Exception as exc:
            if req_id is not None:
                _write_msg(_error_resp(req_id, -32603, f"{exc}\n{traceback.format_exc()}"))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
