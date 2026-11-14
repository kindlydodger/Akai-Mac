#!/usr/bin/env python3
"""Offline & protocol verification tests for mpc_control upgrades."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mpc_client import MpcMap
from mpc_midi_pattern import BUILTIN_PRESETS, build_smf_bytes, parse_smf_summary, write_midi_pattern


HERE = Path(__file__).resolve().parent


class TestMpcControl(unittest.TestCase):
    def test_ui_map_bounds(self) -> None:
        m = MpcMap()
        self.assertIn("ui", m.data)
        for sec, item in [
            ("main", "browser_icon"),
            ("main", "seq_edit_pencil"),
            ("main", "track_edit_pencil"),
            ("browser", "up_folder"),
            ("browser", "places_tab"),
            ("browser", "okcomputer_place"),
            ("browser", "open_or_load"),
            ("step_seq", "tc_btn"),
            ("step_seq", "bar_minus"),
            ("step_seq", "bar_plus"),
            ("step_seq", "pad_minus"),
            ("step_seq", "pad_plus"),
            ("tc_modal", "swing_field"),
            ("tc_modal", "do_it_btn"),
            ("seq_edit_modal", "clear_events_icon"),
            ("seq_edit_modal", "confirm_clear_events_btn"),
        ]:
            x, y = m.resolve_ui(f"{sec}.{item}")
            self.assertTrue(0 <= x < 1280, f"{sec}.{item} x={x} out of range")
            self.assertTrue(0 <= y < 800, f"{sec}.{item} y={y} out of range")

    def test_midi_presets_roundtrip(self) -> None:
        m = MpcMap()
        smf_p, meta_p = build_smf_bytes(BUILTIN_PRESETS["dj_premier_fm"], mpc_map=m)
        parsed_p = parse_smf_summary(smf_p)
        self.assertEqual(parsed_p["ppqn"], 960)
        self.assertEqual(parsed_p["notes_on"], parsed_p["notes_off"])
        self.assertGreater(parsed_p["notes_on"], 90)
        self.assertEqual(parsed_p["total_ticks"], 4 * 4 * 960)
        self.assertEqual(parsed_p["tempos_us"][0], round(60_000_000 / 90.0))

        with tempfile.TemporaryDirectory() as td:
            out_mid = Path(td) / "dilla.mid"
            meta_d = write_midi_pattern(BUILTIN_PRESETS["dilla_swung_boom"], out_mid, mpc_map=m)
            parsed_d = parse_smf_summary(out_mid.read_bytes())
            self.assertEqual(parsed_d["notes_on"], parsed_d["notes_off"])
            self.assertEqual(meta_d["note_count"], parsed_d["notes_on"])
            self.assertEqual(parsed_d["total_ticks"], 4 * 4 * 960)

    def test_mcp_server_initialize_and_tools(self) -> None:
        reqs = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n"
        )
        proc = subprocess.run(
            [sys.executable, str(HERE / "mpc_mcp_server.py")],
            input=reqs,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        lines = [json.loads(line) for line in proc.stdout.strip().splitlines() if line.strip()]
        self.assertEqual(len(lines), 2)
        tool_names = {t["name"] for t in lines[1]["result"]["tools"]}
        self.assertTrue(
            {
                "mpc_status",
                "mpc_button",
                "mpc_pad",
                "mpc_tap",
                "mpc_ui_macro",
                "mpc_midi_pattern",
                "mpc_audio_backends",
                "mpc_audio_analyze",
                "mpc_audio_stretch",
                "mpc_audio_chop",
                "mpc_webhook_status",
                "mpc_webhook_send",
                "mpc_iac_midi_send",
            }.issubset(tool_names)
        )

    def test_activity_feed_and_monitor_probes(self) -> None:
        import os
        import socket
        from mpc_client import emit_activity
        from mpc_monitor import (
            MonitorEngine,
            add_target,
            build_target_entry,
            load_targets,
            remove_target,
            set_target_enabled,
        )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            act_log = root / "activity_feed.jsonl"
            transcript = root / "transcript.jsonl"
            watch_dir = root / "watch"
            watch_dir.mkdir()
            targets_json = root / "targets.json"

            old_env = os.environ.get("MPC_ACTIVITY_LOG")
            os.environ["MPC_ACTIVITY_LOG"] = str(act_log)
            try:
                emit_activity("mcp", "trigger_pads", "[1, 2] vel=110", target="mpc:test")
                emit_activity("cli", "button", "PLAY_START (tap, 0x50)", target="mpc:test")
            finally:
                if old_env is None:
                    os.environ.pop("MPC_ACTIVITY_LOG", None)
                else:
                    os.environ["MPC_ACTIVITY_LOG"] = old_env

            transcript.write_text(
                json.dumps({"step_index": 1, "type": "USER_INPUT", "content": "beat cal"}) + "\n"
                + json.dumps({
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "tool_calls": [{"name": "mpc_pad", "args": {"pads": [1]}}],
                }) + "\n",
                encoding="utf-8",
            )
            (watch_dir / "beat1.mid").write_bytes(b"MThd")

            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.bind(("127.0.0.1", 0))
            srv.listen(2)
            port = srv.getsockname()[1]

            try:
                add_target(build_target_entry("act", "log_tail", str(act_log), parser="jsonl_activity"), targets_json)
                add_target(build_target_entry("tr", "log_tail", str(transcript), parser="antigravity_transcript"), targets_json)
                add_target(build_target_entry("dir", "dir_watch", str(watch_dir)), targets_json)
                add_target(build_target_entry("sock", "tcp", f"127.0.0.1:{port}"), targets_json)

                self.assertEqual(len(load_targets(targets_json)), 4)
                self.assertTrue(set_target_enabled("dir", False, targets_json))
                self.assertFalse(next(t for t in load_targets(targets_json) if t["id"] == "dir")["enabled"])
                self.assertTrue(set_target_enabled("dir", True, targets_json))

                eng = MonitorEngine(targets_path=targets_json)
                eng.probe_once_all()
                rows, evs = eng.snapshot()
                by_id = {r["id"]: r for r in rows}
                self.assertEqual(by_id["act"]["status"], "OK")
                self.assertEqual(by_id["tr"]["status"], "OK")
                self.assertEqual(by_id["dir"]["status"], "OK")
                self.assertEqual(by_id["sock"]["status"], "OK")

                actions = {e["action"] for e in evs}
                self.assertIn("trigger_pads", actions)
                self.assertIn("tools#2", actions)

                # Verify incremental tailing & new file detection on 2nd probe
                (watch_dir / "beat2.mid").write_bytes(b"MThd\x00\x00")
                eng.probe_once_all()
                _, evs2 = eng.snapshot()
                self.assertTrue(any(e["action"] == "created" and "beat2.mid" in e["detail"] for e in evs2))

                proc = subprocess.run(
                    [sys.executable, str(HERE / "mpc_monitor.py"), "--targets", str(targets_json), "--once", "--json"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=True,
                )
                cli_out = json.loads(proc.stdout)
                self.assertEqual(len(cli_out["targets"]), 4)
                removed, remaining = remove_target("sock", targets_json)
                self.assertTrue(removed)
                self.assertEqual(len(remaining), 3)
            finally:
                srv.close()

    def test_desktop_app_bundle_and_tui_pty(self) -> None:
        import os
        import plistlib
        import pty
        import select
        import time
        from build_desktop_app import build_desktop_app, render_mpc_icon_png

        png = render_mpc_icon_png(128)
        self.assertTrue(png.startswith(b"\x89PNG"))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app_path = build_desktop_app(root / "MPC Live Monitor.app")
            plist = plistlib.loads((app_path / "Contents" / "Info.plist").read_bytes())
            self.assertEqual(plist["CFBundleName"], "MPC Live Monitor")
            icns_data = (app_path / "Contents" / "Resources" / "AppIcon.icns").read_bytes()
            self.assertTrue(icns_data.startswith(b"icns"))
            launcher = (app_path / "Contents" / "MacOS" / "MPC Live Monitor")
            self.assertTrue(os.access(launcher, os.X_OK))
            self.assertIn("mpc_monitor.py", launcher.read_text(encoding="utf-8"))

            # Verify curses 256-color MPC palette TUI renders & quits cleanly over PTY
            import fcntl
            import struct
            import termios

            tjson = root / "tui_targets.json"
            tjson.write_text('{"version": 1, "targets": []}', encoding="utf-8")
            pid, fd = pty.fork()
            if pid == 0:
                os.environ["TERM"] = "xterm-256color"
                os.execvp(sys.executable, [sys.executable, str(HERE / "mpc_monitor.py"), "--targets", str(tjson)])
            try:
                fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 110, 0, 0))
                out_chunks = bytearray()
                sent_q = False
                exit_code = None
                deadline = time.monotonic() + 4.0
                while time.monotonic() < deadline:
                    r, _, _ = select.select([fd], [], [], 0.1)
                    if r:
                        try:
                            out_chunks.extend(os.read(fd, 4096))
                        except OSError:
                            pass
                    if b"MPC LIVE" in out_chunks and not sent_q:
                        os.write(fd, b"q")
                        sent_q = True
                    wpid, status = os.waitpid(pid, os.WNOHANG)
                    if wpid:
                        exit_code = os.waitstatus_to_exitcode(status)
                        break
                if exit_code is None:
                    os.kill(pid, 9)
                    os.waitpid(pid, 0)
                    self.fail("curses TUI did not exit cleanly within 4s")
                self.assertEqual(exit_code, 0)
                self.assertIn(b"MPC LIVE", out_chunks)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def test_audio_beat_key_stretch_chop(self) -> None:
        import math
        import os
        import struct
        import wave
        from mpc_audio import (
            analyze_audio,
            chop_audio,
            detect_backends,
            detect_beat,
            detect_key,
            semitones_between_keys,
            stretch_audio,
        )

        backends = detect_backends()
        for tool_name in ("rubberband_r3", "rubberband", "aubio", "keyfinder_cli", "soundstretch", "sox", "ffmpeg", "audio_python"):
            self.assertTrue(backends["tools"].get(tool_name), f"Backend tool missing: {tool_name}")
        for mod_name in ("librosa", "soundfile", "scipy", "numpy", "pyrubberband", "numba"):
            self.assertIn(mod_name, backends["python_mir"], f"Python MIR lib missing: {mod_name}")

        self.assertEqual(semitones_between_keys("F minor", "Gm"), 2)
        self.assertEqual(semitones_between_keys("4A", "6A"), 2)

        sr = 44100
        dur_s = 8.0 * (60.0 / 90.0)  # 8 beats @ 90 BPM = 5.3333s
        total_frames = int(sr * dur_s)
        beat_step = int(sr * (60.0 / 90.0))
        notes_hz = (174.614, 207.652, 261.626, 349.228, 415.305)  # F3, Ab3, C4, F4, Ab4 (F minor)
        buf = bytearray()
        two_pi = 2.0 * math.pi
        for i in range(total_frames):
            t = i / sr
            in_beat = (i % beat_step) / sr
            # Sharp percussive transient at every quarter-note beat + sustained F minor harmonic bed
            env = math.exp(-in_beat * 28.0)
            kick = 0.52 * env * math.sin(two_pi * (55.0 + 90.0 * math.exp(-in_beat * 40.0)) * in_beat)
            chord = sum(0.08 * math.sin(two_pi * f * t) for f in notes_hz)
            sample = max(-0.98, min(0.98, kick + chord))
            buf.extend(struct.pack("<h", int(sample * 32767)))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wav_path = root / "loop_90bpm_fmin.wav"
            with wave.open(str(wav_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sr)
                wf.writeframes(bytes(buf))

            old_env = os.environ.get("MPC_ACTIVITY_LOG")
            os.environ["MPC_ACTIVITY_LOG"] = str(root / "activity.jsonl")
            try:
                an = analyze_audio(wav_path, beat_backend="auto", key_backend="ensemble")
                self.assertGreaterEqual(an["bpm"], 85.0)
                self.assertLessEqual(an["bpm"], 95.0)
                self.assertEqual(an["key"], "F minor")
                self.assertEqual(an["camelot"], "4A")

                b_aub = detect_beat(wav_path, backend="aubio")
                b_lib = detect_beat(wav_path, backend="librosa")
                self.assertGreaterEqual(b_aub["bpm"], 85.0)
                self.assertLessEqual(b_aub["bpm"], 95.0)
                self.assertGreaterEqual(b_lib["bpm"], 85.0)
                self.assertLessEqual(b_lib["bpm"], 95.0)

                k_kf = detect_key(wav_path, backend="keyfinder")
                k_lib = detect_key(wav_path, backend="librosa")
                self.assertEqual(k_kf["key"], "F minor")
                self.assertEqual(k_lib["key"], "F minor")

                out_rb = root / "stretched_100.wav"
                st_rb = stretch_audio(
                    wav_path,
                    output_path=out_rb,
                    backend="rubberband-r3",
                    source_bpm=90.0,
                    target_bpm=100.0,
                    sample_rate=44100,
                    bit_depth=16,
                )
                self.assertTrue(out_rb.is_file())
                expected_dur = st_rb["input_duration_s"] * 0.90
                self.assertAlmostEqual(st_rb["output_duration_s"], expected_dur, delta=0.15)

                out_st = root / "shifted_gm.wav"
                st_key = stretch_audio(
                    wav_path,
                    output_path=out_st,
                    backend="soundstretch",
                    source_key="F minor",
                    target_key="Gm",
                )
                self.assertTrue(out_st.is_file())
                self.assertEqual(st_key["pitch_semitones"], 2.0)

                chop_dir = root / "pads"
                ch = chop_audio(wav_path, out_dir=chop_dir, mode="beats", slices=4, bit_depth=16)
                self.assertEqual(ch["count"], 4)
                self.assertTrue(Path(ch["manifest"]).is_file())
                for s in ch["slices"]:
                    self.assertTrue(Path(s["file"]).is_file())
            finally:
                if old_env is None:
                    os.environ.pop("MPC_ACTIVITY_LOG", None)
                else:
                    os.environ["MPC_ACTIVITY_LOG"] = old_env

    def test_webhook_inbox_and_iac_midi_routing(self) -> None:
        import os
        import threading
        import time
        from mpc_monitor import MonitorEngine, add_target, build_target_entry
        from mpc_webhook_server import (
            DEFAULT_IAC_BIN,
            IacMidiRouter,
            WebhookService,
            make_http_server,
            normalize_midi_messages,
            send_test_webhook,
        )

        # 1. Normalization unit checks across MIDI event types
        norm = normalize_midi_messages(
            {
                "midi": [
                    {"type": "note_on", "note": "C4", "velocity": 110, "channel": 1, "duration_ms": 80},
                    {"type": "cc", "cc": 74, "value": 99, "channel": 2},
                    {"type": "program_change", "program": 5, "channel": 3},
                    {"type": "pitch_bend", "value": 0, "channel": 1},
                    {"type": "clock"},
                    "90 40 7F",
                ]
            }
        )
        self.assertEqual([e["bytes"] for e in norm], [
            [0x90, 60, 110],
            [0xB1, 74, 99],
            [0xC2, 5],
            [0xE0, 0, 0],
            [0xF8],
            [0x90, 64, 127],
        ])

        self.assertTrue(DEFAULT_IAC_BIN.is_file(), "mpc_iac_midi binary missing")

        # 2. Live HTTP webhook -> watched folder -> macOS CoreMIDI IAC Driver Bus 1 loopback + monitor probe
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inbox = root / "webhook_inbox"
            act_log = root / "activity_feed.jsonl"
            targets_json = root / "targets.json"

            old_env = os.environ.get("MPC_ACTIVITY_LOG")
            os.environ["MPC_ACTIVITY_LOG"] = str(act_log)
            router = IacMidiRouter(dest_pattern="IAC")
            srv = None
            th = None
            try:
                router.ensure_ready()
                svc = WebhookService(inbox_dir=inbox, router=router)
                srv = make_http_server(svc, bind="127.0.0.1", port=0)
                port = srv.server_address[1]
                th = threading.Thread(target=srv.serve_forever, daemon=True)
                th.start()

                add_target(build_target_entry("wh-box", "dir_watch", str(inbox)), targets_json)
                eng = MonitorEngine(targets_path=targets_json)
                eng.probe_once_all()  # Initial empty baseline

                listener = subprocess.Popen(
                    [str(DEFAULT_IAC_BIN), "--dest", "IAC", "--listen-ms", "2500", "--count", "2"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                time.sleep(0.25)

                resp = send_test_webhook(
                    f"http://127.0.0.1:{port}/webhook",
                    {
                        "message": "AntiGravity Live Loopback Verification",
                        "midi": [
                            {"type": "note_on", "note": "C4", "velocity": 110, "channel": 1},
                            {"type": "cc", "cc": 74, "value": 99, "channel": 1},
                        ],
                    },
                )
                out_str, err_str = listener.communicate(timeout=4)
                self.assertEqual(listener.returncode, 0, err_str)
                captured = json.loads(out_str.strip())

                self.assertTrue(resp["ok"])
                self.assertEqual(resp["midi_routed"], 2)
                saved = Path(resp["saved_file"])
                self.assertTrue(saved.is_file())
                saved_obj = json.loads(saved.read_text(encoding="utf-8"))
                self.assertEqual(saved_obj["payload"]["message"], "AntiGravity Live Loopback Verification")

                captured_bytes = [p["bytes"] for p in captured["packets"]]
                self.assertIn([144, 60, 110], captured_bytes)
                self.assertIn([176, 74, 99], captured_bytes)

                feed_lines = act_log.read_text(encoding="utf-8").splitlines()
                self.assertTrue(any("AntiGravity Live Loopback Verification" in ln and "IAC[" in ln for ln in feed_lines))

                eng.probe_once_all()
                rows, evs = eng.snapshot()
                self.assertEqual(rows[0]["status"], "OK")
                self.assertTrue(any(e["action"] == "created" and saved.name in e["detail"] for e in evs))
            finally:
                if srv is not None:
                    srv.shutdown()
                    srv.server_close()
                if th is not None:
                    th.join(timeout=1.5)
                router.close()
                if old_env is None:
                    os.environ.pop("MPC_ACTIVITY_LOG", None)
                else:
                    os.environ["MPC_ACTIVITY_LOG"] = old_env


class TestAuditRegressions(unittest.TestCase):
    def test_monitor_pad_note_lookup_and_tcp_validation(self) -> None:
        import mpc_monitor

        lookup = mpc_monitor._build_pad_note_lookup(MpcMap())
        self.assertEqual(lookup.get(37), "1")
        self.assertEqual(lookup.get(36), "2")
        self.assertEqual(len(lookup), 16)

        with self.assertRaises(ValueError):
            mpc_monitor.build_target_entry("t_bad1", "tcp", "badport")
        with self.assertRaises(ValueError):
            mpc_monitor.build_target_entry("t_bad2", "tcp", "127.0.0.1:notaport")
        ok = mpc_monitor.build_target_entry("t_ok", "tcp", "127.0.0.1:6731")
        self.assertEqual(ok["config"]["host"], "127.0.0.1")
        self.assertEqual(ok["config"]["port"], 6731)

    def test_monitor_engine_stop_nonblocking(self) -> None:
        import threading
        import time
        import mpc_monitor

        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "targets.json"
            cfg.write_text(json.dumps({
                "targets": [
                    {
                        "id": "slow_probe",
                        "name": "Slow Probe",
                        "kind": "tcp",
                        "config": {"host": "127.0.0.1", "port": 6731, "timeout_s": 2.0},
                        "interval_s": 0.1,
                        "enabled": True,
                    }
                ]
            }), encoding="utf-8")
            started = threading.Event()
            release = threading.Event()
            engine = mpc_monitor.MonitorEngine(targets_path=cfg)

            def _slow_probe(_target: dict) -> None:
                started.set()
                release.wait(timeout=4.0)

            engine._run_single_probe = _slow_probe  # type: ignore[method-assign]
            try:
                engine.start()
                self.assertTrue(started.wait(timeout=1.5))
                t0 = time.monotonic()
                engine.stop()
                elapsed = time.monotonic() - t0
                self.assertLess(elapsed, 0.9)
            finally:
                release.set()

    def test_mirror_read_json_body_limits(self) -> None:
        import io
        import mpc_mirror

        def _make_handler(content_length: str, body: bytes) -> mpc_mirror.MirrorRequestHandler:
            h = mpc_mirror.MirrorRequestHandler.__new__(mpc_mirror.MirrorRequestHandler)
            h.headers = {"Content-Length": content_length}  # type: ignore[assignment]
            h.rfile = io.BytesIO(body)
            return h

        with self.assertRaises(ValueError):
            _make_handler("not-an-int", b"{}")._read_json_body()
        with self.assertRaises(ValueError):
            _make_handler("-5", b"{}")._read_json_body()
        with self.assertRaises(ValueError):
            _make_handler(str(mpc_mirror.MAX_BODY_BYTES + 1), b"{}")._read_json_body()
        with self.assertRaises(ValueError):
            _make_handler("7", b"[1,2,3]")._read_json_body()
        self.assertEqual(_make_handler("11", b'{"ok":true}')._read_json_body(), {"ok": True})

    def test_webhook_sysex_and_float_pitch_bend(self) -> None:
        import mpc_webhook_server

        events = mpc_webhook_server.normalize_midi_messages([
            {"type": "sysex", "data": "47 7F 4A"},
            {"type": "sysex", "hex": "F0 47 7F F7"},
            {"type": "pitch_bend", "value": 0.0},
            {"type": "pitch_bend", "value": 0},
            {"note": "C4", "velocity": None},
        ])
        self.assertEqual(events[0]["kind"], "sysex")
        self.assertEqual(events[0]["bytes"], [0xF0, 0x47, 0x7F, 0x4A, 0xF7])
        self.assertEqual(events[1]["bytes"], [0xF0, 0x47, 0x7F, 0xF7])
        self.assertEqual(events[2]["bytes"], [0xE0, 0x00, 0x40])
        self.assertEqual(events[3]["bytes"], [0xE0, 0x00, 0x00])
        self.assertEqual(events[4]["kind"], "note_on")
        self.assertEqual(events[4]["bytes"], [0x90, 60, 100])

    def test_audio_probe_meta_and_single_pass_analyze(self) -> None:
        import math
        import struct
        import wave
        from unittest.mock import patch
        import mpc_audio

        sr = 44100
        dur_s = 2.5
        n_frames = int(sr * dur_s)
        samples = bytearray()
        # 120 BPM click pulses + C4/E4/G4 triad for reliable offline MIR verification.
        beat_period = int(sr * 0.5)
        freqs = (261.63, 329.63, 392.00)
        for i in range(n_frames):
            t = i / sr
            in_beat = i % beat_period
            env = math.exp(-in_beat / (sr * 0.035))
            chord = sum(math.sin(2.0 * math.pi * f * t) for f in freqs) / len(freqs)
            click = math.sin(2.0 * math.pi * 1000.0 * t) * math.exp(-in_beat / (sr * 0.008))
            val = max(-0.95, min(0.95, 0.55 * env * chord + 0.35 * click))
            samples.extend(struct.pack("<h", int(val * 32767)))

        with tempfile.TemporaryDirectory() as td:
            wav_path = Path(td) / "test_c_maj_120.wav"
            with wave.open(str(wav_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sr)
                wf.writeframes(bytes(samples))

            with patch.object(mpc_audio, "_which", return_value=None):
                meta = mpc_audio._probe_audio_meta(wav_path)
            self.assertEqual(meta["sample_rate"], sr)
            self.assertEqual(meta["channels"], 1)
            self.assertEqual(meta["duration_s"], 2.5)
            self.assertEqual(meta["bits_per_sample"], 16)
            self.assertEqual(meta["codec"], "pcm_s16le")

            seen_ops: list[str] = []
            orig_worker = mpc_audio._call_librosa_worker

            def _spy_worker(payload: dict, timeout_s: float = 90.0) -> dict:
                prefetch = mpc_audio._MIR_PREFETCH.get()
                op = payload.get("op")
                if not (
                    prefetch is not None
                    and op in ("beat", "key")
                    and (str(payload.get("wav_path")), str(op)) in prefetch
                ):
                    seen_ops.append(str(op))
                return orig_worker(payload, timeout_s=timeout_s)

            with patch.object(mpc_audio, "_call_librosa_worker", side_effect=_spy_worker):
                res = mpc_audio.analyze_audio(wav_path, beat_backend="auto", key_backend="ensemble")
            self.assertEqual(seen_ops, ["analyze"])
            self.assertIsNotNone(res.get("bpm"))
            self.assertGreater(float(res["bpm"]), 0.0)
            self.assertTrue(bool(res.get("key")))

    def test_mpc_sample_indexer_scan_and_watch_classification(self) -> None:
        import math
        import signal
        import struct
        import subprocess
        import time
        import wave

        mac_bin = Path(__file__).resolve().parent / "bin" / "mpc_sample_indexer_mac"
        self.assertTrue(mac_bin.is_file(), f"Missing binary: {mac_bin}")

        def _write_synth_wav(path: Path, dur_s: float, bpm: float | None, freqs: tuple[float, ...]) -> None:
            sr = 44100
            n_frames = int(sr * dur_s)
            period = int(sr * (60.0 / bpm)) if bpm else n_frames + 1
            buf = bytearray()
            for i in range(n_frames):
                t = i / sr
                in_beat = (i % period) if bpm else i
                env = math.exp(-in_beat / (sr * (0.045 if bpm else 0.08)))
                chord = sum(math.sin(2.0 * math.pi * f * t) for f in freqs) / len(freqs)
                click = math.sin(2.0 * math.pi * 1100.0 * t) * math.exp(-in_beat / (sr * 0.008))
                val = max(-0.95, min(0.95, 0.55 * env * chord + 0.35 * click))
                buf.extend(struct.pack("<h", int(val * 32767)))
            with wave.open(str(path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sr)
                wf.writeframes(bytes(buf))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            drive = root / "testdrive"
            drive.mkdir()
            out_dir = root / "out"
            mirror_dir = root / "mirror"

            _write_synth_wav(drive / "tiny_tick.wav", 0.08, None, (220.0,))
            _write_synth_wav(drive / "drums_kick.wav", 0.45, None, (110.0,))
            _write_synth_wav(drive / "synth_loop_120.wav", 6.0, 120.0, (261.63, 329.63, 392.00))

            proc = subprocess.run(
                [
                    str(mac_bin),
                    "scan",
                    "--roots",
                    str(drive),
                    "--out-dir",
                    str(out_dir),
                    "--mirror-dir",
                    str(mirror_dir),
                    "--oneshot-max-s",
                    "3.0",
                    "--jobs",
                    "2",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            stats = json.loads((out_dir / "stats.json").read_text(encoding="utf-8"))
            self.assertEqual(stats["total_samples"], 3)
            self.assertEqual(stats["errors"], 0)
            self.assertTrue((mirror_dir / "stats.json").is_file())
            self.assertTrue((mirror_dir / "library.csv").is_file())

            by_name = {
                Path(json.loads(line)["rel_path"]).name: json.loads(line)
                for line in (out_dir / "library.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            }
            self.assertEqual(by_name["tiny_tick.wav"]["category"], "ultra_short")
            self.assertIsNone(by_name["tiny_tick.wav"]["bpm"])
            self.assertEqual(by_name["drums_kick.wav"]["category"], "one_shot")
            self.assertIsNone(by_name["drums_kick.wav"]["bpm"])
            self.assertEqual(by_name["synth_loop_120.wav"]["category"], "loop")
            self.assertIsNotNone(by_name["synth_loop_120.wav"]["bpm"])
            self.assertGreaterEqual(float(by_name["synth_loop_120.wav"]["bpm"]), 116.0)
            self.assertLessEqual(float(by_name["synth_loop_120.wav"]["bpm"]), 124.0)
            self.assertTrue(bool(by_name["synth_loop_120.wav"]["key"]))

            watch_proc = subprocess.Popen(
                [
                    str(mac_bin),
                    "watch",
                    "--roots",
                    str(drive),
                    "--out-dir",
                    str(out_dir),
                    "--mirror-dir",
                    str(mirror_dir),
                    "--interval",
                    "2",
                    "--jobs",
                    "2",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                time.sleep(0.8)
                _write_synth_wav(drive / "added_pad_am.wav", 4.5, 100.0, (220.0, 261.63, 329.63))
                deadline = time.time() + 8.0
                events_file = out_dir / "events.jsonl"
                found_new = False
                while time.time() < deadline:
                    if events_file.is_file() and "added_pad_am.wav" in events_file.read_text(encoding="utf-8"):
                        found_new = True
                        break
                    time.sleep(0.25)
                self.assertTrue(found_new, "Watcher did not detect added_pad_am.wav within 8s")
            finally:
                watch_proc.send_signal(signal.SIGTERM)
                watch_proc.wait(timeout=5)

    def test_mpc_library_search_camelot_and_host_resolved(self) -> None:
        import mpc_library
        import mpc_mcp_server
        from mpc_client import MpcBridgeClient

        self.assertEqual(mpc_library.classify_sample_category("kick_01.wav", 0.5), "one_shot")
        self.assertEqual(mpc_library.classify_sample_category("Pad-F#4.WAV", 5.0), "one_shot")
        self.assertEqual(mpc_library.classify_sample_category("break_loop.wav", 8.0), "loop")
        self.assertEqual(mpc_library.classify_sample_category("tiny.wav", 0.1), "ultra_short")

        compat = mpc_library.camelot_compatible("Am")
        self.assertEqual(compat, ["8A", "8B", "7A", "9A"])

        with tempfile.TemporaryDirectory() as td:
            tdir = Path(td)
            lib_path = tdir / "library.jsonl"
            host_path = tdir / "host_resolved.jsonl"
            lib_rows = [
                {
                    "drive": "okcomputer",
                    "rel_path": "Loops/Soul_120_Am.wav",
                    "full_path": "/media/okcomputer/Loops/Soul_120_Am.wav",
                    "category": "loop",
                    "duration_s": 8.0,
                    "bpm": 120.0,
                    "bpm_conf": 0.95,
                    "key": "A minor",
                    "short_key": "Am",
                    "camelot": "8A",
                    "key_conf": 0.9,
                    "status": "ok",
                },
                {
                    "drive": "okcomputer",
                    "rel_path": "Loops/HalfTime_60_C.wav",
                    "full_path": "/media/okcomputer/Loops/HalfTime_60_C.wav",
                    "category": "loop",
                    "duration_s": 8.0,
                    "bpm": 60.0,
                    "bpm_conf": 0.9,
                    "key": "C major",
                    "short_key": "C",
                    "camelot": "8B",
                    "key_conf": 0.85,
                    "status": "ok",
                },
                {
                    "drive": "az01-internal-sd",
                    "rel_path": "Stems/Pad_Flac.flac",
                    "full_path": "/media/az01-internal-sd/Stems/Pad_Flac.flac",
                    "category": "unknown",
                    "duration_s": 0.0,
                    "bpm": None,
                    "key": None,
                    "camelot": None,
                    "status": "pending_host_decode",
                },
            ]
            host_rows = [
                {
                    "drive": "az01-internal-sd",
                    "rel_path": "Stems/Pad_Flac.flac",
                    "full_path": "/media/az01-internal-sd/Stems/Pad_Flac.flac",
                    "category": "loop",
                    "duration_s": 12.0,
                    "bpm": 121.0,
                    "bpm_conf": 0.88,
                    "key": "D minor",
                    "short_key": "Dm",
                    "camelot": "7A",
                    "key_conf": 0.81,
                    "status": "ok",
                    "analyzer": "mac_host_audio",
                }
            ]
            lib_path.write_text("\n".join(json.dumps(r) for r in lib_rows) + "\n", encoding="utf-8")
            host_path.write_text("\n".join(json.dumps(r) for r in host_rows) + "\n", encoding="utf-8")

            entries = mpc_library.load_entries(jsonl_path=lib_path, host_resolved_path=host_path)
            self.assertEqual(len(entries), 3)
            flac = [e for e in entries if e["rel_path"] == "Stems/Pad_Flac.flac"][0]
            self.assertEqual(flac["status"], "ok")
            self.assertEqual(flac["camelot"], "7A")

            res = mpc_library.search_library(entries, bpm=120.0, bpm_tol=2.0, key="Am", harmonic=True)
            self.assertEqual(res["matched"], 3)
            exact_only = mpc_library.search_library(entries, bpm=120.0, bpm_tol=2.0, half_double=False, key="Am", harmonic=False)
            self.assertEqual(exact_only["matched"], 1)
            self.assertEqual(exact_only["results"][0]["rel_path"], "Loops/Soul_120_Am.wav")

        mcp_tools = mpc_mcp_server._build_tools(MpcBridgeClient(host="127.0.0.1", port=6731))
        for tname in ("mpc_library_status", "mpc_library_search", "mpc_library_resolve_pending"):
            self.assertIn(tname, mcp_tools)

    def test_mpc_flow_synth_lyria_mock_mirror_webhook_and_mcp(self) -> None:
        import base64
        import http.client
        import threading
        from unittest import mock
        import mpc_flow
        import mpc_mirror
        import mpc_mcp_server
        import mpc_webhook_server
        from mpc_client import MpcBridgeClient

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old_paths = (mpc_flow.CONFIG_PATH, mpc_flow.SECRETS_PATH, mpc_flow.BANKS_DIR, mpc_flow.SEQ_STATE_PATH)
            mpc_flow.CONFIG_PATH = root / "flow_config.json"
            mpc_flow.SECRETS_PATH = root / ".flow_secrets.json"
            mpc_flow.BANKS_DIR = root / "flow_banks"
            mpc_flow.SEQ_STATE_PATH = mpc_flow.BANKS_DIR / "trigger_seq.json"
            try:
                # 1) Offline SynthFlow generation + 16-slice packaging
                gen = mpc_flow.generate_sample_bank(
                    prompt="dark trap 808 bells 140 bpm",
                    slot=2,
                    bpm=140.0,
                    key="F minor",
                    bars=2,
                    engine="synth_flow",
                    deploy=False,
                )
                self.assertTrue(gen["ok"])
                self.assertEqual(gen["slot"], 2)
                self.assertEqual(len(gen["meta"]["slices"]), 16)
                self.assertTrue(Path(gen["wav"]).is_file())

                # 2) Mocked Gemini Lyria 3 HTTP response parsing & packaging
                lyria_src = root / "lyria_fixture.wav"
                mpc_flow.synthesize_flow_wav("ambient rhodes", lyria_src, bpm=90.0, key="C minor", bars=2)
                b64_wav = base64.b64encode(lyria_src.read_bytes()).decode("ascii")
                fake_resp = mock.MagicMock()
                fake_resp.read.return_value = json.dumps({
                    "steps": [
                        {
                            "type": "model_output",
                            "content": [
                                {"type": "text", "text": "[Instrumental ambient Rhodes loop]"},
                                {"type": "audio", "mime_type": "audio/wav", "data": b64_wav},
                            ],
                        }
                    ]
                }).encode("utf-8")
                fake_resp.__enter__.return_value = fake_resp
                fake_resp.__exit__.return_value = False

                mpc_flow.save_gemini_api_key("AIzaTestKey123456")
                with mock.patch("urllib.request.urlopen", return_value=fake_resp):
                    lres = mpc_flow.generate_sample_bank(
                        prompt="warm jazz piano",
                        slot=4,
                        bpm=90.0,
                        bars=2,
                        engine="gemini_lyria",
                        model="lyria-3-clip-preview",
                        deploy=False,
                    )
                self.assertEqual(lres["meta"]["engine"], "gemini_lyria")
                self.assertEqual(len(lres["meta"]["slices"]), 16)

                # 3) Webhook flow_generate integration
                wh = mpc_webhook_server.WebhookService(
                    inbox_dir=root / "wh_inbox",
                    router=mpc_webhook_server.IacMidiRouter(),
                )
                wh_res = wh.handle_webhook(
                    "/webhook",
                    {},
                    json.dumps({
                        "action": "flow_generate",
                        "prompt": "house bass groove",
                        "slot": 5,
                        "bpm": 124,
                        "bars": 2,
                        "engine": "synth_flow",
                        "deploy": False,
                    }).encode("utf-8"),
                )
                self.assertTrue(wh_res["ok"])
                self.assertEqual(wh_res["flow"]["slot"], 5)

                # 4) Web Mirror HTTP /api/flow/* integration
                client = MpcBridgeClient(host="127.0.0.1", port=6731)
                srv = mpc_mirror.MirrorServer(("127.0.0.1", 0), client)
                port = srv.server_address[1]
                t = threading.Thread(target=srv.serve_forever, daemon=True)
                t.start()
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                    try:
                        conn.request("GET", "/api/flow/status")
                        st = json.loads(conn.getresponse().read().decode("utf-8"))
                        self.assertTrue(st["ok"])
                        self.assertGreaterEqual(st["populated_banks"], 3)

                        conn.request("POST", "/api/flow/select", body=json.dumps({"slot": 2, "deploy": False}), headers={"Content-Type": "application/json"})
                        sel = json.loads(conn.getresponse().read().decode("utf-8"))
                        self.assertEqual(sel["slot"], 2)
                    finally:
                        conn.close()
                finally:
                    wh.router.close()
                    srv.shutdown()
                    srv.server_close()

                # 5) MCP Flow tools integration
                tools = mpc_mcp_server._build_tools(client)
                for name in ("mpc_flow_generate", "mpc_flow_banks", "mpc_flow_config"):
                    self.assertIn(name, tools)
                mcp_out = json.loads(tools["mpc_flow_banks"]["handler"]({})[0]["text"])
                self.assertTrue(mcp_out["ok"])
                self.assertEqual(len(mcp_out["banks"]), 16)
            finally:
                mpc_flow.CONFIG_PATH, mpc_flow.SECRETS_PATH, mpc_flow.BANKS_DIR, mpc_flow.SEQ_STATE_PATH = old_paths

    def test_mpc_flow_lyria_realtime_and_429_fallback(self) -> None:
        import io
        import math
        import struct
        import subprocess
        import urllib.error
        from unittest import mock
        import mpc_flow

        self.assertEqual(mpc_flow._key_to_lyria_scale("C minor"), "E_FLAT_MAJOR_C_MINOR")
        self.assertEqual(mpc_flow._key_to_lyria_scale("A minor"), "C_MAJOR_A_MINOR")
        self.assertEqual(mpc_flow._key_to_lyria_scale("F# minor"), "A_MAJOR_G_FLAT_MINOR")
        self.assertEqual(mpc_flow._key_to_lyria_scale("F major"), "F_MAJOR_D_MINOR")

        def _fake_pcm_stereo_48k(dur_s: float = 5.0) -> bytes:
            sr = 48000
            quiet_n = int(0.30 * sr)
            active_n = int(dur_s * sr)
            buf = bytearray((quiet_n + active_n) * 4)
            # Fill active portion with crisp 220 Hz sine wave
            for i in range(active_n):
                val = int(12000 * math.sin(2.0 * math.pi * 220.0 * i / sr))
                off = (quiet_n + i) * 4
                struct.pack_into("<hh", buf, off, val, val)
            return bytes(buf)

        real_subprocess_run = subprocess.run

        def _mock_node_run(cmd, **kwargs):
            if str(mpc_flow.LYRIA_REALTIME_SCRIPT) not in [str(a) for a in cmd]:
                return real_subprocess_run(cmd, **kwargs)
            # API key must travel via stdin JSON, never in process argv
            self.assertTrue(all("AIzaFakeKey" not in str(a) for a in cmd))
            payload = json.loads(kwargs.get("input") or "{}")
            self.assertEqual(payload.get("api_key"), "AIzaFakeKey")
            self.assertEqual(payload.get("scale"), "E_FLAT_MAJOR_C_MINOR")
            out_pcm = Path(payload["out_pcm"])
            out_pcm.write_bytes(_fake_pcm_stereo_48k(4.5))
            res = mock.MagicMock()
            res.returncode = 0
            res.stdout = json.dumps({
                "ok": True,
                "chunks": 3,
                "bytes": out_pcm.stat().st_size,
                "sample_rate": 48000,
                "channels": 2,
                "mime_type": "audio/l16;rate=48000;channels=2",
            })
            res.stderr = ""
            return res

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old_paths = (mpc_flow.CONFIG_PATH, mpc_flow.SECRETS_PATH, mpc_flow.BANKS_DIR, mpc_flow.SEQ_STATE_PATH)
            mpc_flow.CONFIG_PATH = root / "flow_config.json"
            mpc_flow.SECRETS_PATH = root / ".flow_secrets.json"
            mpc_flow.BANKS_DIR = root / "flow_banks"
            mpc_flow.SEQ_STATE_PATH = mpc_flow.BANKS_DIR / "trigger_seq.json"
            try:
                mpc_flow.save_gemini_api_key("AIzaFakeKey")
                # 1) Direct default lyria-realtime-exp call via mocked Node WebSocket helper
                with mock.patch("mpc_flow._find_node_binary", return_value="/opt/homebrew/bin/node"), \
                     mock.patch("subprocess.run", side_effect=_mock_node_run):
                    res = mpc_flow.generate_sample_bank(
                        prompt="soulful dusty piano loop",
                        slot=3,
                        bpm=120.0,
                        key="C minor",
                        bars=2,
                        engine="gemini_lyria",
                        deploy=False,
                    )
                self.assertEqual(res["lyria_model"], "lyria-realtime-exp")
                self.assertEqual(res["meta"]["engine"], "gemini_lyria")
                self.assertEqual(len(res["meta"]["slices"]), 16)
                self.assertTrue(Path(res["wav"]).is_file())

                # 2) REST 429 Free Tier limit: 0 -> automatic fallback to lyria-realtime-exp
                err_429 = urllib.error.HTTPError(
                    url=mpc_flow.GEMINI_INTERACTIONS_URL,
                    code=429,
                    msg="Too Many Requests",
                    hdrs=None,
                    fp=io.BytesIO(b'{"error":{"message":"Quota exceeded: Free Tier limit: 0"}}'),
                )
                with mock.patch("urllib.request.urlopen", side_effect=err_429), \
                     mock.patch("mpc_flow._find_node_binary", return_value="/opt/homebrew/bin/node"), \
                     mock.patch("subprocess.run", side_effect=_mock_node_run):
                    fb = mpc_flow.generate_sample_bank(
                        prompt="chill rhodes",
                        slot=6,
                        bpm=120.0,
                        key="C minor",
                        bars=2,
                        engine="gemini_lyria",
                        model="lyria-3-clip-preview",
                        deploy=False,
                    )
                self.assertEqual(fb["lyria_model"], "lyria-realtime-exp")
                self.assertEqual(len(fb["meta"]["slices"]), 16)
            finally:
                mpc_flow.CONFIG_PATH, mpc_flow.SECRETS_PATH, mpc_flow.BANKS_DIR, mpc_flow.SEQ_STATE_PATH = old_paths

    def test_mpc_drumkit_xpm_and_mirror_endpoints(self) -> None:
        import threading
        import time
        import urllib.request
        import wave
        import xml.etree.ElementTree as ET
        from unittest import mock
        import mpc_drumkit
        import mpc_flow
        import mpc_mcp_server
        import mpc_mirror

        self.assertEqual(
            mpc_drumkit.sanitize_kit_name("bana 90s boom bap tarzi drum kit olustur"),
            "90s_Boom_Bap",
        )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old_flow_paths = (mpc_flow.CONFIG_PATH, mpc_flow.SECRETS_PATH, mpc_flow.BANKS_DIR, mpc_flow.SEQ_STATE_PATH)
            old_kits_dir = mpc_drumkit.KITS_DIR
            mpc_flow.CONFIG_PATH = root / "flow_config.json"
            mpc_flow.SECRETS_PATH = root / ".flow_secrets.json"
            mpc_flow.BANKS_DIR = root / "flow_banks"
            mpc_flow.SEQ_STATE_PATH = mpc_flow.BANKS_DIR / "trigger_seq.json"
            mpc_drumkit.KITS_DIR = root / "flow_kits"
            try:
                res = mpc_drumkit.generate_drum_kit(
                    prompt="dark trap 808 punchy kick sharp hats",
                    bpm=140.0,
                    key="F min",
                    engine="synth_flow",
                    deploy=False,
                )
                self.assertTrue(res["ok"])
                xpm_path = Path(res["xpm_path"])
                self.assertTrue(xpm_path.is_file())
                self.assertEqual(len(res["pads"]), 16)
                for p in res["pads"]:
                    wp = Path(p["wav_path"])
                    self.assertTrue(wp.is_file())
                    with wave.open(str(wp), "rb") as wf:
                        self.assertEqual(wf.getnchannels(), 2)
                        self.assertEqual(wf.getframerate(), mpc_drumkit.SAMPLE_RATE)
                        self.assertGreater(wf.getnframes(), 100)

                tree = ET.fromstring(xpm_path.read_text(encoding="utf-8"))
                self.assertEqual(tree.findtext("./Version/File_Version"), "1.7")
                prog = tree.find("./Program")
                self.assertIsNotNone(prog)
                self.assertEqual(prog.get("type"), "Drum")
                insts = prog.findall("./Instruments/Instrument")
                self.assertEqual(len(insts), 128)
                for idx, inst in enumerate(insts[:16], start=1):
                    self.assertEqual(inst.get("number"), str(idx))
                    self.assertEqual(inst.findtext("./OneShot"), "True")
                    layer = inst.find("./Layers/Layer[@number='1']")
                    self.assertIsNotNone(layer)
                    sname = layer.findtext("./SampleName") or ""
                    self.assertTrue(sname)
                    self.assertFalse(sname.endswith(".wav"))
                    self.assertEqual(layer.findtext("./SampleFile") or "", "")
                    self.assertEqual(layer.findtext("./SliceIndex"), "128")
                self.assertEqual(insts[16].findtext("./Layers/Layer[@number='1']/SampleName") or "", "")
                self.assertEqual(insts[2].findtext("./MuteGroup"), "1")
                self.assertEqual(insts[3].findtext("./MuteGroup"), "1")

                pad_notes = prog.findall("./PadNoteMap/PadNote")
                self.assertEqual(len(pad_notes), 128)
                for idx, pn in enumerate(pad_notes[:16], start=1):
                    self.assertEqual(pn.get("number"), str(idx))
                    self.assertEqual(pn.findtext("./Note"), str(mpc_drumkit.MPC_BANK_A_NOTES[idx - 1]))

                kits = mpc_drumkit.list_drum_kits()
                self.assertEqual(len(kits), 1)
                self.assertEqual(kits[0]["kit_name"], res["kit_name"])

                # Mirror HTTP /api/flow/drumkit, /api/flow/kits, /api/flow/job
                mock_client = mock.MagicMock()
                mock_client.host = "127.0.0.1"
                mock_client.port = 6731
                mock_client.map = MpcMap()
                tools = mpc_mcp_server._build_tools(mock_client)
                self.assertIn("mpc_flow_drumkit", tools)

                srv = mpc_mirror.MirrorServer(("127.0.0.1", 0), mock_client)
                port = srv.server_address[1]
                t = threading.Thread(target=srv.serve_forever, daemon=True)
                t.start()
                try:
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/flow/drumkit",
                        data=json.dumps({"prompt": "90s boom bap", "engine": "synth_flow", "deploy": False}).encode(),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    sync_resp = json.loads(urllib.request.urlopen(req, timeout=5).read().decode())
                    self.assertTrue(sync_resp["ok"])
                    self.assertEqual(len(sync_resp["pads"]), 16)

                    kits_resp = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/flow/kits", timeout=5).read().decode())
                    self.assertTrue(kits_resp["ok"])
                    self.assertGreaterEqual(len(kits_resp["kits"]), 2)

                    jreq = urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/flow/job",
                        data=json.dumps({"mode": "kit", "prompt": "house 909 kit", "engine": "synth_flow", "deploy": False}).encode(),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    jinit = json.loads(urllib.request.urlopen(jreq, timeout=5).read().decode())
                    self.assertTrue(jinit["ok"])
                    jid = jinit["job_id"]
                    final_job = {}
                    for _ in range(40):
                        final_job = json.loads(
                            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/flow/job?id={jid}", timeout=5).read().decode()
                        )
                        if final_job.get("status") in ("done", "error"):
                            break
                        time.sleep(0.1)
                    self.assertEqual(final_job.get("status"), "done")
                    self.assertEqual(final_job["result"]["mode"], "kit")
                    self.assertEqual(len(final_job["result"]["pads"]), 16)
                finally:
                    srv.shutdown()
                    srv.server_close()
            finally:
                mpc_drumkit.KITS_DIR = old_kits_dir
                mpc_flow.CONFIG_PATH, mpc_flow.SECRETS_PATH, mpc_flow.BANKS_DIR, mpc_flow.SEQ_STATE_PATH = old_flow_paths

    def test_mpc_host_resolution_and_cache_refresh(self) -> None:
        from unittest import mock
        from mpc_client import MpcBridgeClient, invalidate_mpc_host_cache, resolve_mpc_host

        invalidate_mpc_host_cache()
        self.assertEqual(resolve_mpc_host("127.0.0.1"), "127.0.0.1")

        calls = []

        def fake_getaddrinfo(host: str, port: object, family: int = 0, type_: int = 0) -> list[tuple[object, ...]]:
            calls.append((host, family))
            ip = "10.104.58.233" if len(calls) >= 2 else "10.101.132.233"
            return [(2, 1, 6, "", (ip, 0))]

        with mock.patch("mpc_client.socket.getaddrinfo", side_effect=fake_getaddrinfo):
            self.assertEqual(resolve_mpc_host("mpc-live.local"), "10.101.132.233")
            # Second call within TTL hits cache without another DNS lookup.
            self.assertEqual(resolve_mpc_host("mpc-live.local"), "10.101.132.233")
            self.assertEqual(len(calls), 1)
            # Forced refresh re-resolves updated IP cleanly.
            self.assertEqual(resolve_mpc_host("mpc-live.local", force_refresh=True), "10.104.58.233")
            self.assertEqual(len(calls), 2)

            client = MpcBridgeClient()
            self.assertEqual(client.host, "mpc-live.local")
            self.assertEqual(client.resolve_host(), "10.104.58.233")

    def test_mpc_system_probe_and_sysinfo_parser(self) -> None:
        from unittest import mock
        from mpc_monitor import _parse_mpc_sysinfo, _probe_mpc_system

        sample_out = """==IP==
wlan0 10.104.58.233/24
==CPU1==
cpu  1000 0 500 8500 0 0 0 0 0 0
==CPU2==
cpu  1020 0 505 8575 0 0 0 0 0 0
==MEM==
MemTotal: 2048000
MemAvailable: 1024000
==LOAD==
0.55 0.72 0.80 1/200 1234
56606.96 178912.03
==TEMP==
53333
==DF==
Filesystem Size Used Available Capacity Mounted on
/dev/root 401.3M 319.9M 77.4M 81% /
/dev/mmcblk0p7 3.1G 2.1G 802.1M 73% /data
/dev/mmcblk1p1 14.4G 6.3G 8.1G 44% /sdcard
/dev/sda1 223.6G 125.9G 97.7G 56% /media/okcomputer
"""
        metrics, prev_cpu = _parse_mpc_sysinfo(sample_out)
        self.assertEqual(metrics["ip"], "10.104.58.233")
        self.assertEqual(metrics["iface"], "wlan0")
        self.assertEqual(metrics["cpu_pct"], 25.0)
        self.assertEqual(metrics["ram_pct"], 50.0)
        self.assertEqual(metrics["temp_c"], 53.3)
        self.assertEqual([d["label"] for d in metrics["disks"]], ["okcomputer", "sdcard", "data", "root"])
        self.assertEqual([d["pct"] for d in metrics["disks"]], [56.0, 44.0, 73.0, 81.0])

        # Subsequent delta tick using previous CPU tuple without CPU2
        next_out = sample_out.replace(
            "==CPU1==\ncpu  1000 0 500 8500 0 0 0 0 0 0\n==CPU2==\ncpu  1020 0 505 8575 0 0 0 0 0 0\n",
            "==CPU1==\ncpu  1030 0 515 8655 0 0 0 0 0 0\n",
        )
        m2, _ = _parse_mpc_sysinfo(next_out, prev_cpu=prev_cpu)
        self.assertEqual(m2["cpu_pct"], 20.0)

        state: dict = {}
        fake_proc = mock.Mock(returncode=0, stdout=sample_out, stderr="")
        with mock.patch("mpc_monitor.subprocess.run", return_value=fake_proc):
            st, summary, _ = _probe_mpc_system({"id": "mpc-system", "config": {"host": "root@mpc-live.local"}}, state)
        self.assertEqual(st, "OK")
        self.assertIn("10.104.58.233", summary)
        self.assertIn("CPU 25.0%", summary)
        self.assertIn("ok:56%", summary)
        self.assertEqual(state["metrics"]["ip"], "10.104.58.233")

    def test_mpc_gtop_widgets_and_pty_view(self) -> None:
        import fcntl
        import os
        import pty
        import select
        import struct
        import termios
        import time
        from unittest import mock
        from mpc_monitor import (
            _human_bytes,
            _parse_mpc_sysinfo,
            _probe_mpc_system,
            _render_braille_chart,
            _render_braille_donut,
            _render_sparkline,
            _sort_processes,
        )

        self.assertEqual(_human_bytes(0), "0 B")
        self.assertEqual(_human_bytes(1536), "1.5 KiB")
        self.assertEqual(_human_bytes(1000, si=True), "1.0 KB")

        rich_out = """==IP==
wlan0 10.104.58.233/24
==CPU1==
cpu  1000 0 500 8500 0 0 0 0 0 0
cpu0 250 0 150 2100 0 0 0 0 0 0
cpu1 250 0 50 2200 0 0 0 0 0 0
cpu2 300 0 100 2100 0 0 0 0 0 0
cpu3 200 0 200 2100 0 0 0 0 0 0
==NET1==
Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 1000 10 0 0 0 0 0 0 1000 10 0 0 0 0 0 0
 wlan0: 100000 500 0 0 0 0 0 0 50000 200 0 0 0 0 0 0
==MEM==
MemTotal: 2048000
MemAvailable: 1024000
SwapTotal: 512000
SwapFree: 384000
==LOAD==
0.92 0.84 0.82 1/200 1234
1000.00 3500.00
==UP2==
1000.50 3502.00
==CPU2==
cpu  1020 0 505 8575 0 0 0 0 0 0
cpu0 260 0 155 2110 0 0 0 0 0 0
cpu1 252 0 50 2223 0 0 0 0 0 0
cpu2 304 0 101 2120 0 0 0 0 0 0
cpu3 204 0 200 2121 0 0 0 0 0 0
==NET2==
    lo: 1000 10 0 0 0 0 0 0 1000 10 0 0 0 0 0 0
 wlan0: 102048 504 0 0 0 0 0 0 54096 204 0 0 0 0 0 0
==TEMP==
53333
==DF==
Filesystem Size Used Available Capacity Mounted on
/dev/root 401.3M 319.9M 77.4M 81% /
/dev/mmcblk0p7 3.1G 273.7M 2.6G 9% /data
/dev/mmcblk1p1 14.4G 6.3G 8.1G 44% /sdcard
/dev/sda1 223.6G 125.9G 97.7G 56% /media/okcomputer
==TOP==
25032     1 root     S     694m  34%   8% {MPC 3 Main} /usr/bin/MPC |RSS=710196
25141 25055 root     SN    596m  29%   1% ./filebrowser -c /data/mpc-addins/filebrowser/config.yaml |RSS=56424
  344     1 root     S     212m  10%   4% /usr/bin/DeviceControlServer.bin |RSS=204800
   99     2 root     SW       0   0%   2% [irq/56-ehci_hcd] |RSS=0
28726 28716 root     R     3028   0%   2% top -b -n 1 |RSS=2060
"""
        m, _ = _parse_mpc_sysinfo(rich_out)
        self.assertEqual(len(m["per_core"]), 4)
        self.assertGreater(m["per_core"][0], 40.0)
        self.assertEqual(m["swap_pct"], 25.0)
        self.assertEqual(m["net"]["iface"], "wlan0")
        self.assertEqual(m["net"]["rx_sec"], 4096.0)
        self.assertEqual(m["net"]["tx_sec"], 8192.0)

        procs = m["processes"]
        self.assertEqual([p["pid"] for p in procs], [25032, 344, 99, 25141])  # filtered out top -b -n 1, sorted by %CPU desc
        self.assertEqual(procs[0]["command"], "MPC 3 Main")
        self.assertEqual(procs[2]["command"], "[irq/56-ehci_hcd]")
        self.assertLess(procs[3]["mem_pct"], 5.0)  # RSS 56MB/2GB ~2.8%, not bloated 29% VSZ

        by_mem = _sort_processes(procs, sort_key="mem", reverse=False)
        self.assertEqual([p["pid"] for p in by_mem], [25032, 344, 25141, 99])
        by_pid_asc = _sort_processes(procs, sort_key="pid", reverse=True)
        self.assertEqual([p["pid"] for p in by_pid_asc], [99, 344, 25032, 25141])

        state: dict = {}
        fake_proc = mock.Mock(returncode=0, stdout=rich_out, stderr="")
        with mock.patch("mpc_monitor.subprocess.run", return_value=fake_proc):
            _probe_mpc_system({"id": "mpc-system", "config": {"host": "root@mpc-live.local"}}, state)
            _probe_mpc_system({"id": "mpc-system", "config": {"host": "root@mpc-live.local"}}, state)
        hist = state["metrics"]["history"]
        self.assertEqual(len(hist["cpu_total"]), 2)
        self.assertEqual(len(hist["cpu_cores"]), 4)
        self.assertEqual(len(hist["net_rx_sec"]), 2)

        braille = _render_braille_chart([[10, 40, 80], [20, 60, 30]], width=12, height=4, max_val=100.0)
        self.assertEqual(len(braille), 4)
        self.assertEqual(len(braille[0]), 12)
        self.assertTrue(any(cell[1] >= 0 and ord(cell[0]) >= 0x2800 for row in braille for cell in row))

        donut = _render_braille_donut(50.0, width=8, height=4)
        self.assertEqual(len(donut), 4)
        self.assertTrue(any(cell[1] == 1 for row in donut for cell in row))
        self.assertTrue(any(cell[1] == 0 for row in donut for cell in row))

        spark = _render_sparkline([0, 25, 50, 100], width=6)
        self.assertEqual(len(spark), 6)
        self.assertTrue(spark.endswith("█"))

        # Verify curses --dash view renders & handles sort keys (c, m, p), view switch (1), and quit (q)
        with tempfile.TemporaryDirectory() as td:
            tjson = Path(td) / "dash_targets.json"
            tjson.write_text('{"version": 1, "targets": []}', encoding="utf-8")
            pid, fd = pty.fork()
            if pid == 0:
                os.environ["TERM"] = "xterm-256color"
                os.execvp(sys.executable, [sys.executable, str(HERE / "mpc_monitor.py"), "--dash", "--targets", str(tjson)])
            try:
                fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 136, 0, 0))
                out = bytearray()
                step = 0
                exit_code = None
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    r, _, _ = select.select([fd], [], [], 0.1)
                    if r:
                        try:
                            out.extend(os.read(fd, 4096))
                        except OSError:
                            pass
                    if step == 0 and b"DASH" in out:
                        os.write(fd, b"cmp1")
                        step = 1
                    elif step == 1:
                        time.sleep(0.1)
                        os.write(fd, b"q")
                        step = 2
                    wpid, status = os.waitpid(pid, os.WNOHANG)
                    if wpid:
                        exit_code = os.waitstatus_to_exitcode(status)
                        break
                if exit_code is None:
                    os.kill(pid, 9)
                    os.waitpid(pid, 0)
                    self.fail("curses --dash TUI did not exit cleanly within 5s")
                self.assertEqual(exit_code, 0)
                self.assertIn(b"MPC LIVE", out)
                self.assertIn(b"DASH", out)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass


class TestBootAnim(unittest.TestCase):
    def test_boot_anim_build_and_render(self) -> None:
        boot_dir = HERE / "boot_anim"
        script = boot_dir / "build_and_deploy_boot_anim.sh"
        self.assertTrue(script.is_file(), f"missing build script: {script}")

        res = subprocess.run(
            ["bash", str(script), "--build-only"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(res.returncode, 0, msg=res.stdout + "\n" + res.stderr)

        build_dir = boot_dir / "build"
        host_bin = build_dir / "mpc_boot_anim_host"
        arm_bin = build_dir / "mpc_boot_anim"
        arm_so = build_dir / "libmpc_boot_splash.so"
        frame0 = build_dir / "boot_ascii_frame0.bgra"
        for p in (host_bin, arm_bin, arm_so, frame0):
            self.assertTrue(p.is_file(), f"missing build artifact: {p}")

        raw = frame0.read_bytes()
        self.assertEqual(len(raw), 800 * 1280 * 4)
        # Top-left logical corner maps to physical (0, 1279): pitch-black opaque ARGB (0xFF000000 LE)
        self.assertEqual(raw[0:4], b"\x00\x00\x00\xff")
        # Non-black bright glyphs present in ACV8 Frame 0
        nonblack = sum(1 for i in range(0, len(raw), 16) if raw[i] or raw[i + 1] or raw[i + 2])
        self.assertGreater(nonblack, 50)

        # ANSI preview at 68% shows toothy grin glyphs and real progress bar
        p68 = subprocess.run(
            [str(host_bin), "--ansi", "--progress", "0.68"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(p68.returncode, 0)
        self.assertIn("68%", p68.stdout)
        self.assertIn("V", p68.stdout)
        self.assertIn("^", p68.stdout)
        self.assertIn("MPC LIVE // HAKAI OS", p68.stdout)

        # ANSI preview at 100% shows finale state + celebratory beatmaker splash
        p100 = subprocess.run(
            [str(host_bin), "--ansi", "--progress", "1.00"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(p100.returncode, 0)
        self.assertIn("100%", p100.stdout)
        self.assertIn("MPC LIVE READY", p100.stdout)
        self.assertIn("BEAT MACHINE ARMED -- DROP THE NEEDLE!", p100.stdout)

        # Integration configs in place
        launch_txt = (HERE / "launch-mpc.sh").read_text(encoding="utf-8")
        self.assertIn("libmpc_boot_splash.so", launch_txt)
        self.assertIn("mpc_boot_anim", launch_txt)
        art_hdr = (boot_dir / "mpc_boot_ascii_art.h").read_text(encoding="utf-8")
        self.assertNotIn("ASCII BOOT SEQUENCE", art_hdr)
        self.assertTrue((boot_dir / "mpc-boot-anim.service").is_file())
        self.assertTrue((boot_dir / "10-boot-anim.conf").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
