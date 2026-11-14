#!/usr/bin/env python3
"""Command-line interface for Akai MPC Live physical hardware control."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

from mpc_client import MpcBridgeClient
from mpc_midi_pattern import BUILTIN_PRESETS, deploy_midi_to_mpc, write_midi_pattern


def _maybe_dispatch_passthrough(argv: List[str]) -> Optional[int]:
    """Bypass parent argparse for `audio` / `webhook` subcommands so flags/--help pass through untouched."""
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("--host", "--port") and i + 1 < len(argv):
            if tok == "--host":
                os.environ["MPC_HOST"] = argv[i + 1]
            i += 2
            continue
        if tok.startswith("--host="):
            os.environ["MPC_HOST"] = tok.split("=", 1)[1]
            i += 1
            continue
        if tok.startswith("--port="):
            i += 1
            continue
        if tok == "audio":
            from mpc_audio import run_audio_cli

            return run_audio_cli(argv[i + 1 :])
        if tok == "webhook":
            from mpc_webhook_server import run_webhook_cli

            return run_webhook_cli(argv[i + 1 :])
        return None
    return None


def main() -> int:
    sub_rc = _maybe_dispatch_passthrough(sys.argv[1:])
    if sub_rc is not None:
        return sub_rc

    parser = argparse.ArgumentParser(description="Control Akai MPC Live hardware over libmpc_hw_bridge (:6731).")
    parser.add_argument("--host", default=None, help="MPC IP (default: env MPC_HOST or 10.101.132.233)")
    parser.add_argument("--port", type=int, default=None, help="Bridge TCP port (default: 6731)")

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_stat = sub.add_parser("status", help="Check bridge and screen health")
    p_stat.add_argument("--no-screen", action="store_true")

    sub.add_parser("list", help="List all mapped controls")

    p_btn = sub.add_parser("button", help="Press/hold/release a button")
    p_btn.add_argument("name")
    p_btn.add_argument("--action", default="tap", choices=["tap", "down", "hold", "up", "release"])
    p_btn.add_argument("--hold-ms", type=int, default=60)
    p_btn.add_argument("--release-mode", default="90_00", choices=["90_00", "80_40"])

    p_cmb = sub.add_parser("combo", help="Hold modifier + tap target")
    p_cmb.add_argument("modifier")
    p_cmb.add_argument("target")
    p_cmb.add_argument("--hold-ms", type=int, default=60)

    p_pad = sub.add_parser("pad", help="Trigger one or more pads (1..16)")
    p_pad.add_argument("pads", nargs="+", type=int)
    p_pad.add_argument("--vel", "--velocity", dest="velocity", type=int, default=100)
    p_pad.add_argument("--ms", "--duration-ms", dest="duration_ms", type=int, default=100)
    p_pad.add_argument("--at", "--aftertouch", dest="aftertouch", type=int, default=None)
    p_pad.add_argument("--bank", default=None)
    p_pad.add_argument("--gap-ms", type=int, default=0)

    p_dial = sub.add_parser("dial", help="Rotate and/or press the Data Dial")
    p_dial.add_argument("steps", type=int, nargs="?", default=1)
    p_dial.add_argument("--press", action="store_true")

    p_ql = sub.add_parser("qlink", help="Adjust Q-Link knob 1..4")
    p_ql.add_argument("knob", type=int)
    p_ql.add_argument("--delta", type=int, default=None)
    p_ql.add_argument("--value", type=int, default=None)

    p_seq = sub.add_parser("sequence", help="Run JSON step sequence from string or file")
    p_seq.add_argument("spec", help="JSON array string or path to .json file")
    p_seq.add_argument("--step-ms", type=int, default=125)

    p_raw = sub.add_parser("raw", help="Inject raw hex MIDI bytes")
    p_raw.add_argument("hex_bytes")
    p_raw.add_argument("--allow-unsafe", action="store_true")

    p_ev = sub.add_parser("events", help="Show captured hardware RX/TX events")
    p_ev.add_argument("--since", type=int, default=0)
    p_ev.add_argument("--sniff-leds", action="store_true")
    p_ev.add_argument("--no-noise", action="store_true")

    p_lrn = sub.add_parser("learn", help="Learn a physical control from hardware press")
    p_lrn.add_argument("name")
    p_lrn.add_argument("--timeout", type=float, default=10.0)

    p_scr = sub.add_parser("screen", help="Save current MPC screenshot")
    p_scr.add_argument("out", type=Path)
    p_scr.add_argument("--full", action="store_true")

    p_tap = sub.add_parser("tap", help="Tap 1280x800 touchscreen coords or named UI target")
    p_tap.add_argument("args", nargs="+", help="Either 'x y' or 'section.element' (e.g. step_seq.tc_btn)")
    p_tap.add_argument("--pause-ms", type=int, default=140)

    p_ui = sub.add_parser("ui", help="Run MPC 3.x UI macro (clear_events | open_browser | step_seq)")
    p_ui.add_argument("macro", choices=["clear_events", "open_browser", "step_seq"])
    p_ui.add_argument("--spec", default=None, help="JSON string, file path, or preset name for step_seq")
    p_ui.add_argument("--start-pad", type=int, default=1)
    p_ui.add_argument("--bars", type=int, default=4)

    p_mid = sub.add_parser("midi", help="Build a 960-PPQN .mid pattern and optionally deploy via SCP")
    p_mid.add_argument("--preset", choices=sorted(BUILTIN_PRESETS.keys()), default=None)
    p_mid.add_argument("--spec", default=None, help="JSON string or path to pattern .json")
    p_mid.add_argument("--out", type=Path, default=None, help="Output .mid path")
    p_mid.add_argument("--deploy", action="store_true", help="Upload .mid to MPC /media/okcomputer/MidiPatterns")
    p_mid.add_argument("--remote-dir", default="/media/okcomputer/MidiPatterns")

    p_mir = sub.add_parser("mirror", help="Start local Mac Studio Mirror web server")
    p_mir.add_argument("--bind", default="127.0.0.1")
    p_mir.add_argument("--port", type=int, default=6767)
    p_mir.add_argument("--open", action="store_true")

    p_mon = sub.add_parser("monitor", help="Launch live Mac Terminal Monitor (or --once snapshot)")
    p_mon.add_argument("--once", action="store_true", help="Print one snapshot and exit")
    p_mon.add_argument("--stream", action="store_true", help="Continuous ANSI log stream mode")
    p_mon.add_argument("--interval", type=float, default=1.0, help="Poll tick interval (s)")

    p_aud = sub.add_parser("audio", add_help=False, help="Beat/Key detection, time-stretch & sample chopping")
    p_aud.add_argument("audio_args", nargs=argparse.REMAINDER)

    p_wh = sub.add_parser("webhook", add_help=False, help="Local webhook server -> watched inbox -> IAC Driver Bus 1")
    p_wh.add_argument("webhook_args", nargs=argparse.REMAINDER)

    p_lib = sub.add_parser("library", add_help=False, help="MPC sample library status, sync, Camelot search & compressed resolver")
    p_lib.add_argument("library_args", nargs=argparse.REMAINDER)

    p_fl = sub.add_parser("flow", add_help=False, help="Google Flow / Gemini Lyria sample gen & FLOW AI control")
    p_fl.add_argument("flow_args", nargs=argparse.REMAINDER)

    args = parser.parse_args()
    client = MpcBridgeClient(host=args.host, port=args.port, source="cli")

    if args.cmd == "status":
        out = client.status(check_screen=not args.no_screen)
    elif args.cmd == "list":
        out = client.map.summary()
    elif args.cmd == "button":
        out = client.press_button(args.name, action=args.action, hold_ms=args.hold_ms, release_mode=args.release_mode)
    elif args.cmd == "combo":
        out = client.combo(args.modifier, args.target, hold_ms=args.hold_ms)
    elif args.cmd == "pad":
        out = client.trigger_pads(
            args.pads, velocity=args.velocity, duration_ms=args.duration_ms,
            aftertouch=args.aftertouch, bank=args.bank, gap_ms=args.gap_ms,
        )
    elif args.cmd == "dial":
        out = client.turn_dial(steps=args.steps, press=args.press)
    elif args.cmd == "qlink":
        out = client.qlink(args.knob, delta=args.delta, value=args.value)
    elif args.cmd == "sequence":
        p = Path(args.spec)
        steps = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else json.loads(args.spec)
        out = client.play_sequence(steps, default_step_ms=args.step_ms)
    elif args.cmd == "raw":
        out = client.raw_midi(args.hex_bytes, allow_unsafe=args.allow_unsafe)
    elif args.cmd == "events":
        out = client.events(
            since_seq=args.since,
            sniff_leds=True if args.sniff_leds else None,
            include_noise=not args.no_noise,
        )
    elif args.cmd == "learn":
        out = client.learn_control(args.name, timeout_s=args.timeout)
    elif args.cmd == "screen":
        data = client.screenshot(half=not args.full, save_path=args.out)
        out = {"saved": str(args.out), "bytes": len(data)}
    elif args.cmd == "tap":
        if len(args.args) == 1:
            out = client.ui_tap(args.args[0], pause_ms=args.pause_ms)
        elif len(args.args) == 2:
            out = client.tap(int(args.args[0]), int(args.args[1]), pause_ms=args.pause_ms)
        else:
            raise ValueError("Expected 'tap x y' or 'tap section.element'")
    elif args.cmd == "ui":
        if args.macro == "clear_events":
            out = client.clear_sequence_events()
        elif args.macro == "open_browser":
            out = client.open_browser()
        else:
            if args.spec in BUILTIN_PRESETS:
                spec_obj = BUILTIN_PRESETS[args.spec]
            elif args.spec:
                sp = Path(args.spec)
                spec_obj = json.loads(sp.read_text(encoding="utf-8")) if sp.is_file() else json.loads(args.spec)
            else:
                raise ValueError("--spec is required for ui step_seq")
            pads_map = spec_obj.get("pads", spec_obj)
            out = client.program_step_seq(pads_map, start_pad=args.start_pad, bars=args.bars)
    elif args.cmd == "midi":
        if args.preset:
            spec_obj = BUILTIN_PRESETS[args.preset]
        elif args.spec:
            sp = Path(args.spec)
            spec_obj = json.loads(sp.read_text(encoding="utf-8")) if sp.is_file() else json.loads(args.spec)
        else:
            raise ValueError("Provide --preset or --spec")
        default_name = "".join(c if (c.isalnum() or c in "_-") else "_" for c in str(spec_obj.get("name", "pattern")))
        out_path = args.out or (Path(__file__).resolve().parent / "patterns" / f"{default_name}.mid")
        meta = write_midi_pattern(spec_obj, out_path, mpc_map=client.map)
        out = {"midi": meta}
        if args.deploy:
            out.update(deploy_midi_to_mpc(out_path, client.host, remote_dir=args.remote_dir))
    elif args.cmd == "mirror":
        from mpc_mirror import MirrorServer
        import webbrowser

        srv = MirrorServer((args.bind, args.port), client)
        url = f"http://{args.bind}:{args.port}/"
        print(f"MPC Live Mac Mirror listening on {url} -> MPC {client.host}", flush=True)
        if args.open:
            webbrowser.open(url)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            srv.server_close()
        return 0
    elif args.cmd == "monitor":
        from mpc_monitor import run_monitor_cli

        mon_argv = ["--interval", str(args.interval)]
        if args.once:
            mon_argv.append("--once")
        elif args.stream:
            mon_argv.append("--stream")
        return run_monitor_cli(mon_argv)
    elif args.cmd == "audio":
        from mpc_audio import run_audio_cli

        return run_audio_cli(args.audio_args)
    elif args.cmd == "webhook":
        from mpc_webhook_server import run_webhook_cli

        return run_webhook_cli(args.webhook_args)
    elif args.cmd == "library":
        from mpc_library import run_library_cli

        return run_library_cli(args.library_args)
    elif args.cmd == "flow":
        from mpc_flow import run_flow_cli

        return run_flow_cli(args.flow_args)
    else:
        return 2

    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
