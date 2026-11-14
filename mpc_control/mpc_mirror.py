#!/usr/bin/env python3
"""Local Mac Studio Mirror & Full-Hardware Controller Server for Akai MPC Live.

Combines MPC :6720 live MJPEG stream + touch forwarding with :6731 hardware
bridge controls (16 pads, transport, modes, data dial, Q-Links) and instant
.mid pattern building/deployment into a single multi-threaded local web UI.
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import threading
import time
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

from mpc_client import MpcBridgeClient
from mpc_midi_pattern import BUILTIN_PRESETS, deploy_midi_to_mpc, write_midi_pattern


HERE = Path(__file__).resolve().parent
UI_HTML_PATH = HERE / "mirror_ui.html"
PATTERNS_DIR = HERE / "patterns"
MAX_BODY_BYTES = 2 * 1024 * 1024

_JOB_LOCK = threading.Lock()
_FLOW_JOBS: Dict[str, Dict[str, Any]] = {}
_LAST_JOB_ID: Optional[str] = None
_JOB_SEQ = 0


def _import_flow_modules() -> tuple[Any, Any]:
    try:
        from . import mpc_drumkit, mpc_flow  # type: ignore
    except ImportError:
        import mpc_drumkit  # type: ignore
        import mpc_flow  # type: ignore
    return mpc_flow, mpc_drumkit


def start_flow_job(params: Dict[str, Any], default_host: str) -> Dict[str, Any]:
    global _JOB_SEQ, _LAST_JOB_ID
    mpc_flow, mpc_drumkit = _import_flow_modules()
    mode = str(params.get("mode") or "kit").strip().lower()
    prompt = str(params.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")

    with _JOB_LOCK:
        _JOB_SEQ += 1
        job_id = f"job_{int(time.time())}_{_JOB_SEQ}"
        job_state: Dict[str, Any] = {
            "ok": True,
            "job_id": job_id,
            "mode": mode,
            "prompt": prompt,
            "status": "running",
            "stage": "queued",
            "pct": 4,
            "message": f"Starting {mode} generation...",
            "created_at": round(time.time(), 2),
            "result": None,
            "error": None,
        }
        _FLOW_JOBS[job_id] = job_state
        _LAST_JOB_ID = job_id

    def _update(stage: str, pct: int, msg: str) -> None:
        with _JOB_LOCK:
            job_state["stage"] = stage
            job_state["pct"] = max(0, min(100, int(pct)))
            job_state["message"] = msg[:120]

    def _worker() -> None:
        try:
            bpm = float(params["bpm"]) if params.get("bpm") not in (None, "", 0) else None
            key = str(params["key"]).strip() if params.get("key") else None
            engine = str(params["engine"]).strip() if params.get("engine") else None
            model = str(params["model"]).strip() if params.get("model") else None
            deploy = bool(params.get("deploy", True))
            host = str(params.get("host") or default_host)
            if mode in ("kit", "drumkit", "xpm"):
                res = mpc_drumkit.generate_drum_kit(
                    prompt=prompt,
                    kit_name=str(params["kit_name"]).strip() if params.get("kit_name") else None,
                    bpm=bpm,
                    key=key,
                    engine=engine,
                    model=model,
                    deploy=deploy,
                    host=host,
                    progress_cb=_update,
                )
            else:
                _update("lyria", 25, "Streaming Lyria loop stems...")
                res = mpc_flow.generate_sample_bank(
                    prompt=prompt,
                    slot=int(params.get("slot", 1)),
                    bpm=bpm,
                    key=key,
                    bars=int(params.get("bars", 4)) if params.get("bars") not in (None, "") else 4,
                    engine=engine,
                    model=model,
                    slice_mode=str(params["slice_mode"]).strip() if params.get("slice_mode") else None,
                    deploy=deploy,
                    host=host,
                )
            with _JOB_LOCK:
                job_state["status"] = "done"
                job_state["stage"] = "done"
                job_state["pct"] = 100
                job_state["result"] = _compact_job_result(res, mode)
                if mode in ("kit", "drumkit", "xpm"):
                    job_state["message"] = f"KIT READY: {res.get('kit_name')}.xpm"
                else:
                    job_state["message"] = f"BANK {res.get('slot', 1):02d} READY"
        except Exception as exc:
            with _JOB_LOCK:
                job_state["status"] = "error"
                job_state["stage"] = "error"
                job_state["error"] = str(exc)[:220]
                job_state["message"] = f"ERROR: {str(exc)[:90]}"

    threading.Thread(target=_worker, daemon=True).start()
    return dict(job_state)


def _compact_job_result(res: Dict[str, Any], mode: str) -> Dict[str, Any]:
    if mode in ("kit", "drumkit", "xpm"):
        remote_xpm = ((res.get("deploy") or {}).get("remote_xpm") or res.get("xpm_path") or "")
        return {
            "mode": "kit",
            "kit_name": res.get("kit_name", ""),
            "bpm": res.get("bpm", 92.0),
            "key": res.get("key", "C minor"),
            "lyria_hits": res.get("lyria_hits", 0),
            "xpm_path": remote_xpm,
            "pads": [
                {
                    "pad": p["pad"],
                    "pad_id": p["pad_id"],
                    "short": p["short"],
                    "role": p["role"],
                    "source": p["source"],
                    "note": p["midi_note"],
                }
                for p in (res.get("pads") or [])
            ],
        }
    meta = res.get("meta") or {}
    return {
        "mode": "loop",
        "slot": res.get("slot", 1),
        "bpm": meta.get("bpm", 92.0),
        "key": meta.get("key", "C minor"),
        "label": meta.get("label", ""),
    }


def get_flow_job(job_id: Optional[str] = None) -> Dict[str, Any]:
    with _JOB_LOCK:
        target_id = job_id or _LAST_JOB_ID
        if not target_id or target_id not in _FLOW_JOBS:
            _, mpc_drumkit = _import_flow_modules()
            kits = mpc_drumkit.list_drum_kits()
            latest = _compact_job_result(kits[0], "kit") if kits else None
            return {"ok": True, "status": "idle", "pct": 0, "message": "READY", "latest_kit": latest}
        return dict(_FLOW_JOBS[target_id])


class MirrorServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], client: MpcBridgeClient) -> None:
        self.mpc_client = client
        super().__init__(server_address, MirrorRequestHandler)


class MirrorRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Keep console clean; surface only errors if needed.
        return

    @property
    def client(self) -> MpcBridgeClient:
        return self.server.mpc_client  # type: ignore[attr-defined]

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Dict[str, Any]:
        raw_len = self.headers.get("Content-Length", "0") or "0"
        try:
            ln = int(raw_len)
        except ValueError as exc:
            raise ValueError(f"Invalid Content-Length: {raw_len}") from exc
        if ln < 0 or ln > MAX_BODY_BYTES:
            raise ValueError(f"Payload too large ({ln} bytes, max {MAX_BODY_BYTES})")
        if ln == 0:
            return {}
        raw = self.rfile.read(ln)
        obj = json.loads(raw.decode("utf-8"))
        if not isinstance(obj, dict):
            raise ValueError("Expected JSON object body")
        return obj

    def _proxy_remote_simple(self, path_with_query: str) -> None:
        last_exc: Optional[Exception] = None
        for attempt in range(2):
            conn: Optional[http.client.HTTPConnection] = None
            try:
                target = self.client.resolve_host(force_refresh=(attempt == 1))
                conn = http.client.HTTPConnection(target, self.client.remote_port, timeout=4.0)
                conn.request("GET", path_with_query)
                resp = conn.getresponse()
                data = resp.read()
                ctype = resp.getheader("Content-Type") or "text/plain; charset=utf-8"
                self.send_response(resp.status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            except Exception as exc:
                last_exc = exc
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
        self._send_json({"ok": False, "error": str(last_exc)}, status=502)

    def _proxy_mjpeg_stream(self, path_with_query: str) -> None:
        conn: Optional[http.client.HTTPConnection] = None
        try:
            target = self.client.resolve_host()
            try:
                conn = http.client.HTTPConnection(target, self.client.remote_port, timeout=10.0)
                conn.request("GET", path_with_query)
                resp = conn.getresponse()
            except (http.client.HTTPException, OSError):
                if conn is not None:
                    conn.close()
                target = self.client.resolve_host(force_refresh=True)
                conn = http.client.HTTPConnection(target, self.client.remote_port, timeout=10.0)
                conn.request("GET", path_with_query)
                resp = conn.getresponse()
            ctype = resp.getheader("Content-Type") or "multipart/x-mixed-replace; boundary=frame"
            self.send_response(resp.status)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            while True:
                chunk = resp.read1(8192) if hasattr(resp, "read1") else resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        p = parsed.path

        if p in ("/", "/index.html"):
            if not UI_HTML_PATH.is_file():
                self.send_error(HTTPStatus.NOT_FOUND, "mirror_ui.html missing")
                return
            html = UI_HTML_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(html)
            return

        if p == "/stream":
            q = f"?{parsed.query}" if parsed.query else ""
            self._proxy_mjpeg_stream(f"/stream{q}")
            return

        if p in ("/info", "/tap", "/down", "/move", "/up", "/screen"):
            q = f"?{parsed.query}" if parsed.query else ""
            self._proxy_remote_simple(f"{p}{q}")
            return

        if p == "/api/status":
            try:
                bridge = self.client.status(check_screen=False)
                screen_info = self.client.screen_info()
                qlinks = self.client.map.data.get("qlinks", {}).get("knobs", {})
                self._send_json({
                    "ok": True,
                    "bridge": bridge,
                    "screen": screen_info,
                    "qlinks": qlinks,
                    "learned": self.client.map.learned,
                    "presets": sorted(BUILTIN_PRESETS.keys()),
                })
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=502)
            return

        if p == "/api/flow/status":
            try:
                from . import mpc_flow
            except ImportError:
                import mpc_flow  # type: ignore

            try:
                self._send_json(mpc_flow.get_flow_status())
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=500)
            return

        if p == "/api/flow/banks":
            try:
                from . import mpc_flow
            except ImportError:
                import mpc_flow  # type: ignore

            try:
                self._send_json({"ok": True, "banks": mpc_flow.list_banks()})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=500)
            return

        if p == "/api/flow/kits":
            try:
                _, mpc_drumkit = _import_flow_modules()
                kits = mpc_drumkit.list_drum_kits()
                self._send_json({
                    "ok": True,
                    "kits": [_compact_job_result(k, "kit") for k in kits[:12]],
                    "presets": list(mpc_drumkit.DRUM_PRESETS),
                })
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=500)
            return

        if p == "/api/flow/job":
            try:
                qs = urllib.parse.parse_qs(parsed.query or "")
                jid = (qs.get("id") or [None])[0]
                self._send_json(get_flow_job(jid))
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=500)
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        p = urllib.parse.urlsplit(self.path).path
        try:
            body = self._read_json_body()
            if p == "/api/button":
                res = self.client.press_button(
                    name=str(body["name"]),
                    action=str(body.get("action", "tap")),
                    hold_ms=int(body.get("hold_ms", 60)),
                    release_mode=str(body.get("release_mode", "90_00")),
                )
            elif p == "/api/combo":
                res = self.client.combo(
                    modifier=str(body.get("modifier", "SHIFT")),
                    target=str(body["target"]),
                    hold_ms=int(body.get("hold_ms", 60)),
                )
            elif p == "/api/pad":
                if "pads" in body:
                    raw_pads = body["pads"]
                    pads = list(raw_pads) if isinstance(raw_pads, (list, tuple)) else [raw_pads]
                elif "pad" in body:
                    pads = [body["pad"]]
                else:
                    raise ValueError("Provide 'pad' or 'pads'")
                res = self.client.trigger_pads(
                    pads=pads,
                    velocity=int(body.get("velocity", 110)),
                    duration_ms=int(body.get("duration_ms", 90)),
                    aftertouch=body.get("aftertouch"),
                    bank=body.get("bank"),
                )
            elif p == "/api/dial":
                res = self.client.turn_dial(
                    steps=int(body.get("steps", 1)),
                    press=bool(body.get("press", False)),
                )
            elif p == "/api/qlink":
                res = self.client.qlink(
                    knob=int(body["knob"]),
                    delta=body.get("delta"),
                    value=body.get("value"),
                    touch=body.get("touch"),
                )
            elif p == "/api/learn":
                res = self.client.learn_control(
                    name=str(body["name"]),
                    timeout_s=float(body.get("timeout_s", 10.0)),
                )
            elif p == "/api/macro":
                act = str(body.get("action", ""))
                if act == "clear_events":
                    res = self.client.clear_sequence_events()
                elif act == "open_browser":
                    res = self.client.open_browser(select_okcomputer=bool(body.get("okcomputer", True)))
                else:
                    raise ValueError(f"Unknown macro '{act}'")
            elif p == "/api/midi/deploy":
                preset_name = body.get("preset")
                spec = BUILTIN_PRESETS[preset_name] if preset_name else body.get("spec")
                if not isinstance(spec, dict):
                    raise ValueError("Provide either valid 'preset' name or 'spec' object")
                safe_name = "".join(c if (c.isalnum() or c in "_-") else "_" for c in str(spec.get("name", "pattern")))
                local_file = PATTERNS_DIR / f"{safe_name}.mid"
                meta = write_midi_pattern(spec, local_file, mpc_map=self.client.map)
                remote_dir = str(
                    body.get("remote_dir")
                    or self.client.map.data.get("meta", {}).get("midi_patterns_dir", "/media/okcomputer/MidiPatterns")
                )
                deploy_info = {}
                if body.get("deploy", True):
                    deploy_info = deploy_midi_to_mpc(local_file, self.client.host, remote_dir=remote_dir)
                res = {"midi": meta, **deploy_info}
            elif p in ("/api/flow/generate", "/api/flow/select", "/api/flow/config", "/api/flow/drumkit", "/api/flow/job", "/api/flow/audition"):
                mpc_flow, mpc_drumkit = _import_flow_modules()

                if p == "/api/flow/generate":
                    prompt = str(body.get("prompt") or "").strip()
                    if not prompt:
                        self._send_json({"ok": False, "error": "prompt is required"}, status=400)
                        return
                    res = mpc_flow.generate_sample_bank(
                        prompt=prompt,
                        slot=int(body.get("slot", 1)),
                        bpm=float(body["bpm"]) if body.get("bpm") not in (None, "") else None,
                        key=str(body["key"]).strip() if body.get("key") else None,
                        bars=int(body["bars"]) if body.get("bars") not in (None, "") else None,
                        engine=str(body["engine"]).strip() if body.get("engine") else None,
                        model=str(body["model"]).strip() if body.get("model") else None,
                        slice_mode=str(body["slice_mode"]).strip() if body.get("slice_mode") else None,
                        deploy=bool(body.get("deploy", True)),
                        host=str(body.get("host") or self.client.host),
                    )
                elif p == "/api/flow/drumkit":
                    prompt = str(body.get("prompt") or "").strip()
                    if not prompt:
                        self._send_json({"ok": False, "error": "prompt is required"}, status=400)
                        return
                    if bool(body.get("async", False)):
                        res = start_flow_job({**body, "mode": "kit"}, self.client.host)
                    else:
                        res = mpc_drumkit.generate_drum_kit(
                            prompt=prompt,
                            kit_name=str(body["kit_name"]).strip() if body.get("kit_name") else None,
                            bpm=float(body["bpm"]) if body.get("bpm") not in (None, "") else None,
                            key=str(body["key"]).strip() if body.get("key") else None,
                            engine=str(body["engine"]).strip() if body.get("engine") else None,
                            model=str(body["model"]).strip() if body.get("model") else None,
                            deploy=bool(body.get("deploy", True)),
                            host=str(body.get("host") or self.client.host),
                        )
                elif p == "/api/flow/job":
                    res = start_flow_job(body, self.client.host)
                elif p == "/api/flow/audition":
                    res = mpc_drumkit.audition_kit_pad(
                        kit_name=str(body["kit_name"]).strip() if body.get("kit_name") else None,
                        pad=int(body.get("pad", 1)),
                        trigger_mpc=bool(body.get("trigger_mpc", True)),
                        play_mac=bool(body.get("play_mac", False)),
                        host=str(body.get("host") or self.client.host),
                    )
                elif p == "/api/flow/select":
                    slot = int(body.get("slot", 1))
                    res = mpc_flow.select_bank(
                        slot=slot,
                        deploy=bool(body.get("deploy", True)),
                        host=str(body.get("host") or self.client.host),
                    )
                else:
                    updates = {k: v for k, v in body.items() if k not in ("api_key", "clear_api_key")}
                    if updates:
                        mpc_flow.save_config(updates)
                    if body.get("clear_api_key"):
                        mpc_flow.save_gemini_api_key("")
                    elif body.get("api_key"):
                        mpc_flow.save_gemini_api_key(str(body["api_key"]))
                    res = mpc_flow.get_flow_status()
            else:
                self._send_json({"ok": False, "error": f"Unknown endpoint {p}"}, status=404)
                return

            self._send_json({"ok": True, **res})
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local Mac Studio Mirror UI for Akai MPC Live.")
    parser.add_argument("--mpc-host", default=None, help="MPC IP (default: mpc_live_map default_host)")
    parser.add_argument("--bind", default="127.0.0.1", help="Local bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=6767, help="Local mirror web port (default: 6767)")
    parser.add_argument("--open", action="store_true", help="Open mirror URL automatically in default browser")
    args = parser.parse_args()

    client = MpcBridgeClient(host=args.mpc_host, source="mirror")
    srv = MirrorServer((args.bind, args.port), client)
    url = f"http://{args.bind}:{args.port}/"
    print(f"MPC Live Mac Mirror listening on {url} -> MPC {client.host} (:6720 screen, :{client.port} bridge)", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping MPC Mirror.", flush=True)
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
