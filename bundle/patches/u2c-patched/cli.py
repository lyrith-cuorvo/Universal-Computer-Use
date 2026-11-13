"""U2C process-per-command CLI; all session state lives outside this process."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import sqlite3
import sys
import time

from . import __version__
from .benchmark import smoke
from .drivers.windows import WindowsDriver
from .errors import U2CError
from .executor import Runtime
from .guard import request_digest
from .protocol import validate_request
from .session import SessionStore


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise U2CError("INVALID_REQUEST", message)


def parser():
    result = Parser(prog="u2c", description="Local screenshot-grounded computer use; no model service")
    result.add_argument("--version", action="version", version=f"U2C {__version__}")
    commands = result.add_subparsers(dest="command", required=True, parser_class=Parser)
    evaluation = commands.add_parser("evaluation", help="Prepare or check an independent AI feedback package; no GUI actions")
    evaluations = evaluation.add_subparsers(dest="evaluation_command", required=True, parser_class=Parser)
    for name in ("init", "check"):
        cmd = evaluations.add_parser(name)
        cmd.add_argument("--directory", required=True)
        cmd.add_argument("--json", action="store_true")
        if name == "init":
            cmd.add_argument("--host-label", default="anonymous-host")
            cmd.add_argument("--model-label", default="anonymous-model")
    for name in ("doctor", "observe", "wait-foreground", "act", "stop", "benchmark", "approve", "verify"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--json", action="store_true", help="Emit a machine-readable JSON object")
        if name in ("observe", "wait-foreground", "stop", "verify"):
            cmd.add_argument("--session", required=name != "observe")
        if name == "observe":
            cmd.add_argument("--mode", choices=("safe", "fast"), default="safe")
            cmd.add_argument("--monitor")
            capture_scope = cmd.add_mutually_exclusive_group()
            capture_scope.add_argument("--region", help="Physical left,top,width,height; negative origins allowed")
            capture_scope.add_argument("--window-client", type=int, metavar="HWND",
                                       help="Render only this window's client area")
            cmd.add_argument("--window-pid", type=int, metavar="PID",
                             help="Resolve a unique window for this PID, or check ownership of the supplied HWND")
            cmd.add_argument("--scale", type=float, default=1.0)
            cmd.add_argument("--frame-ttl", type=float, default=30, help="New-session frame lifetime, 0.1-300 seconds; default 30")
            cmd.add_argument("--max-actions", type=int, help="New-session batch limit, 1-64")
            cmd.add_argument("--guard", choices=("strict", "auto", "relaxed"), default="auto",
                             help="Pre-input pixel guard: strict exact pixels, auto relax only proven animation, relaxed geometry/focus only")
        if name == "wait-foreground":
            cmd.add_argument("--timeout", type=float, default=15, metavar="SECONDS",
                             help="Passive wait for the bound window, 0-120 seconds; 0 checks immediately")
        if name in ("act", "approve"):
            cmd.add_argument("--request", required=True)
        if name == "act":
            cmd.add_argument("--activate-target", action="store_true",
                             help="Request foreground focus for the bound window before input")
        if name == "approve":
            cmd.add_argument("--reason", required=True)
        if name == "benchmark":
            cmd.add_argument("--suite", choices=("smoke",), default="smoke")
            cmd.add_argument("--iterations", type=int, default=10)
            cmd.add_argument("--mode", choices=("safe", "fast"), default="safe")
            cmd.add_argument("--region", help="Restrict the capture-only smoke test to a physical rectangle")
        if name == "verify":
            cmd.add_argument("--frame", required=True)
            cmd.add_argument("--outcome", choices=("pass", "fail"), required=True)
            cmd.add_argument("--note", required=True)
    return result


def read_request(path):
    try:
        request_path = Path(path)
        if request_path.stat().st_size > 1_048_576:
            raise U2CError("INVALID_REQUEST", "Request exceeds 1 MiB")
        data = json.loads(request_path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise U2CError("INVALID_REQUEST", "Request must be a JSON object")
        return data
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise U2CError("INVALID_REQUEST", "Cannot read JSON request", reason=str(exc)) from exc
    except (ValueError, RecursionError) as exc:
        # Python applies independent integer-conversion and parser nesting
        # limits even to files below our byte limit. Keep those failures in UAP.
        raise U2CError("INVALID_REQUEST", "JSON request exceeds parser numeric or nesting limits",
                       exception=type(exc).__name__) from exc


def doctor(driver, store):
    diagnosis = driver.doctor()
    interactive = bool(diagnosis.get("interactive"))
    integrity = diagnosis.get("integrity") or {}
    input_ok = None if "input_integrity_ok" not in integrity else bool(integrity["input_integrity_ok"])
    capabilities = {"local_execution": True, "screenshot": interactive,
                    "native_input_preflight": interactive,
                    "input_integrity_ok": input_ok,
                    "model_vision": "unverified", "full_visual_e2e": "unverified"}
    note = "A desktop preflight is not evidence that a model viewed a screenshot or that input succeeded."
    if input_ok is False:
        note = ("This process runs below medium integrity: UIPI silently drops synthetic mouse and keyboard "
                "input aimed at normal desktop windows. Run U2C unsandboxed at medium integrity.")
    return {
        "status": "ok" if interactive else "blocked", "version": __version__,
        "python": platform.python_version(), "state_directory": str(store.root),
        "driver": diagnosis,
        "capabilities": capabilities,
        "telemetry": False, "public_control_server": False,
        "note": note,
    }


def run(args):
    if args.command == "evaluation":
        from .evaluation import check_run, init_run
        if args.evaluation_command == "init":
            return init_run(args.directory, host_label=args.host_label, model_label=args.model_label)
        return check_run(args.directory)
    store = SessionStore()
    if args.command == "stop":
        store.stop(args.session)
        release_result = "not_attempted"
        release_error = None
        try:
            emergency_driver = WindowsDriver()
            emergency_driver.emergency_release()
            release_result = "attempted"
        except (U2CError, OSError) as exc:
            release_result = "blocked"
            release_error = exc.as_dict() if isinstance(exc, U2CError) else str(exc)
        return {"status": "ok", "session_id": args.session, "stop_requested": True,
                "emergency_release": release_result, "release_error": release_error,
                "note": "Active executor stops at its next checkpoint; emergency modifier/button keyups are best effort. Privileged or blocked native calls cannot be preempted."}
    if args.command == "verify":
        frame = store.frame(args.session, args.frame)
        record = {"kind": "host_visual_assertion", "frame_id": args.frame,
                  "outcome": args.outcome, "note": args.note[:2000], "recorded_at": time.time(),
                  "verification_source": "host_reported_not_independently_attested"}
        store.record(args.session, record)
        return {"status": "ok", "session_id": args.session, "verification": record,
                "screenshot_sha256": frame.get("sha256"),
                "note": "This records the host's assertion; it does not independently prove image reading."}
    if args.command == "approve":
        request = read_request(args.request)
        session = store.get(request.get("session_id"))
        request = validate_request(request, mode=session["mode"], max_actions=session.get("max_actions"))
        store.frame(request["session_id"], request["frame_id"])
        if not sys.stdin.isatty():
            raise U2CError("PERMISSION_DENIED", "Approval requires a human interactive terminal; agents must not self-authorize")
        digest = request_digest(request)
        print("Review this exact validated request and its screenshot before authorizing:", file=sys.stderr)
        print(json.dumps(request, ensure_ascii=False, indent=2), file=sys.stderr)
        frame = store.frame(request["session_id"], request["frame_id"])
        print(f"Screenshot: {frame.get('path')}  SHA256: {frame.get('sha256')}", file=sys.stderr)
        print(f"Approve risk={request.get('risk', 'none')} for request SHA256 {digest}?", file=sys.stderr)
        print("Only continue with the user's explicit authorization. Type APPROVE:", file=sys.stderr)
        if input().strip() != "APPROVE":
            raise U2CError("PERMISSION_DENIED", "Approval declined")
        approval = store.approve(request["session_id"], digest, args.reason)
        return {"status": "ok", "session_id": request["session_id"], **approval}
    driver = WindowsDriver()
    if args.command == "doctor":
        return doctor(driver, store)
    runtime = Runtime(store, driver)
    if args.command == "observe":
        region = None
        if args.region:
            try:
                left, top, width, height = map(int, args.region.split(","))
                region = {"left": left, "top": top, "width": width, "height": height}
            except ValueError as exc:
                raise U2CError("INVALID_REQUEST", "region must be left,top,width,height") from exc
        return runtime.observe(args.session, mode=args.mode, monitor=args.monitor, region=region, scale=args.scale,
                               frame_ttl=args.frame_ttl, max_actions=args.max_actions,
                               window_client=args.window_client, window_pid=args.window_pid,
                               guard={"mode": args.guard})
    if args.command == "wait-foreground":
        return runtime.wait_foreground(args.session, timeout=args.timeout)
    if args.command == "act":
        return runtime.act(read_request(args.request), activate_target=args.activate_target)
    if args.command == "benchmark":
        region = None
        if args.region:
            try:
                left, top, width, height = map(int, args.region.split(","))
                region = {"left": left, "top": top, "width": width, "height": height}
            except ValueError as exc:
                raise U2CError("INVALID_REQUEST", "region must be left,top,width,height") from exc
        return smoke(runtime, args.iterations, region=region, mode=args.mode)
    raise U2CError("INVALID_REQUEST", "Unknown command")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parser().parse_args(argv)
        result = run(args)
        result.setdefault("protocol", "u2c.receipt.v0")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("status") in ("error", "partial", "FAIL"):
            return 1
        if result.get("status") in ("blocked", "PARTIAL"):
            return 3
        return 0
    except U2CError as exc:
        print(json.dumps({"protocol": "u2c.receipt.v0", "status": "error", "error": exc.as_dict()}, ensure_ascii=False, indent=2))
        return 3 if exc.code in ("NO_INTERACTIVE_DESKTOP", "PERMISSION_DENIED") else 1
    except KeyboardInterrupt:
        print(json.dumps({"protocol": "u2c.receipt.v0", "status": "error",
                          "error": {"code": "STOP_REQUESTED", "message": "Interrupted by user", "details": {}}}))
        return 130
    except sqlite3.Error as exc:
        # A damaged, locked or inaccessible state database must not be silently
        # replaced. Preserve the original evidence and emit a sanitized error.
        error = U2CError("STATE_STORE_ERROR", "Local session storage failed; no automatic repair was attempted",
                         exception=type(exc).__name__)
        print(json.dumps({"protocol": "u2c.receipt.v0", "status": "error", "error": error.as_dict()}, ensure_ascii=False, indent=2))
        return 1
    except OSError as exc:
        print(json.dumps({"protocol": "u2c.receipt.v0", "status": "error",
                          "error": {"code": "PERMISSION_DENIED", "message": str(exc), "details": {}}}))
        return 3
