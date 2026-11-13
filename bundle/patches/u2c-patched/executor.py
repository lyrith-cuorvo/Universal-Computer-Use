"""Validated, frame-bound execution. This module never interprets screen content."""
from __future__ import annotations

import hashlib
import math
import time
from pathlib import Path

from .capture import WINDOW_CLIENT_SOURCE, capture_frame, compare_frames, guard_settings, map_point
from .errors import U2CError
from .guard import Guard
from .protocol import validate_request
from .retention import prune_owned_frames
from .desktop_lock import desktop_lock
from .verifier import wait_for_stable_frame


INPUT_ACTIONS = frozenset({"click", "double_click", "move", "drag", "scroll", "type", "key", "hotkey"})


def _window_binding(frame):
    if frame and frame.get("pixel_source") == WINDOW_CLIENT_SOURCE:
        return frame.get("window_hwnd", frame.get("foreground_hwnd")), frame.get("window_pid")
    return None


class Runtime:
    def __init__(self, store, driver):
        self.store = store
        self.driver = driver

    def observe(self, session_id=None, *, mode="safe", monitor=None, region=None, scale=1.0, frame_ttl=30,
                max_actions=None, window_client=None, window_pid=None, guard=None):
        with desktop_lock():
            return self._observe(session_id, mode=mode, monitor=monitor, region=region, scale=scale,
                                 frame_ttl=frame_ttl, max_actions=max_actions, window_client=window_client,
                                 window_pid=window_pid, guard=guard)

    def _observe(self, session_id=None, *, mode="safe", monitor=None, region=None, scale=1.0, frame_ttl=30,
                 max_actions=None, window_client=None, window_pid=None, guard=None):
        session = self.store.get(session_id) if session_id else self.store.create(mode=mode, frame_ttl=frame_ttl, max_actions=max_actions)
        latest = self.store.latest_frame_metadata(session_id) if session_id else None
        binding = _window_binding(latest)
        if binding is not None:
            hwnd, pid = binding
            if (window_client not in (None, hwnd) or window_pid not in (None, pid)
                    or monitor is not None or region is not None):
                raise U2CError("INVALID_REQUEST", "Start a new session to change the window capture target")
            window_client, window_pid = binding
        self.driver.assert_interactive()
        started = time.perf_counter()
        frame = capture_frame(
            self.driver, Path(self.store.root) / "frames" / session["session_id"],
            monitor=monitor, region=region, scale=scale, window_client=window_client, window_pid=window_pid,
            guard=guard,
        )
        frame = self.store.publish_frame(session["session_id"], frame)
        prune_owned_frames(self.store.root, session["session_id"], preserve=frame["path"])
        return {"status": "ok", "session_id": session["session_id"],
                "mode": session["mode"], "frame": frame,
                "metrics": {"screenshots": 1, "capture_ms": round((time.perf_counter()-started)*1000, 3)},
                "visual_verification": "required_by_host_model"}

    def wait_foreground(self, session_id, *, timeout=15):
        """Wait passively on the window already bound to a session."""
        if type(timeout) not in (int, float) or not 0 <= timeout <= 120 or not math.isfinite(timeout):
            raise U2CError("INVALID_REQUEST", "Foreground wait timeout must be between 0 and 120 seconds")
        binding = _window_binding(self.store.latest_frame_metadata(session_id))
        if binding is None or not all(type(value) is int and value > 0 for value in binding):
            raise U2CError("INVALID_REQUEST", "Foreground waiting requires a bound window-client session")
        hwnd, pid = binding
        result = {"session_id": session_id, "window_hwnd": hwnd, "window_pid": pid}
        started = time.monotonic()
        deadline = started + timeout
        while True:
            self.driver.assert_interactive()
            focus = self.driver.foreground()
            if self.driver.window_owner(hwnd) != pid:
                raise U2CError("WINDOW_UNAVAILABLE", "The bound window belongs to a different process")
            now = time.monotonic()
            if focus == hwnd and self.driver.foreground() == hwnd:
                return {**result, "status": "ok", "foreground": True,
                        "wait_ms": round((now - started) * 1000, 3)}
            if now >= deadline:
                error = U2CError("FOCUS_LOST", "The bound window did not become the foreground window in time")
                return {**result, "status": "blocked", "foreground": False, "error": error.as_dict(),
                        "wait_ms": round((now - started) * 1000, 3)}
            time.sleep(min(.15, deadline - now))
            if _window_binding(self.store.latest_frame_metadata(session_id)) != binding:
                raise U2CError("STALE_FRAME", "The window binding changed during the wait")

    def act(self, payload, *, activate_target=False):
        with desktop_lock():
            return self._act(payload, activate_target=activate_target)

    def _act(self, payload, *, activate_target=False):
        # Validate before constructing driver actions or claiming the frame.
        sid = payload.get("session_id") if isinstance(payload, dict) else None
        if not sid:
            raise U2CError("INVALID_REQUEST", "session_id is required")
        session = self.store.get(sid)
        request = validate_request(payload, mode=session["mode"], max_actions=session.get("max_actions"))
        frame = self.store.frame(sid, request["frame_id"])
        self.driver.assert_interactive()
        if frame.get("sha256"):
            try:
                actual_hash = hashlib.sha256(Path(frame["path"]).read_bytes()).hexdigest()
            except OSError as exc:
                raise U2CError("STALE_FRAME", "Observed screenshot is no longer readable") from exc
            if actual_hash != frame["sha256"]:
                raise U2CError("STALE_FRAME", "Observed screenshot has been modified; observe again")
        binding = _window_binding(frame)
        window_mode = binding is not None
        if activate_target and not window_mode:
            raise U2CError("INVALID_REQUEST", "--activate-target requires a window-client frame")
        focus = binding[0] if window_mode else frame.get("foreground_hwnd")
        expected_focus = int(focus or 0)
        foreground_matches = expected_focus and self.driver.foreground() == expected_focus
        needs_activation = (activate_target and expected_focus and not foreground_matches and
                            any(action["type"] in INPUT_ACTIONS for action in request["actions"]))
        if not foreground_matches and not needs_activation:
            raise U2CError("FOCUS_LOST", "Foreground differs from the observed frame; observe again")
        if window_mode and any(action["type"] == "scroll" and "x" not in action for action in request["actions"]):
            raise U2CError("INVALID_REQUEST", "Window-client scroll requires explicit coordinates")
        timeout = request.get("timeout", 15.0)
        token = self.store.claim(sid, request["frame_id"], timeout=timeout)
        started = time.perf_counter()
        deadline = time.monotonic() + timeout
        receipts = []
        screenshots = 0
        result_frame = None
        error = None
        stopped_for_change = False
        stop_reason = None
        baseline_path = frame["path"]
        capture_scope = ({"window_client": expected_focus, "window_pid": frame.get("window_pid")}
                         if window_mode else {"region": frame.get("bounds")})
        single_window_click = window_mode and len(request["actions"]) == 1 and request["actions"][0]["type"] == "click"
        click_point = None
        guard = guard_settings(frame.get("guard"))
        channel_tolerance = guard["channel_tolerance"]
        budget = guard["changed_ratio_budget"]
        pixel_guard = "strict"

        def recapture():
            nonlocal screenshots
            captured = capture_frame(self.driver, Path(self.store.root) / "frames" / sid,
                                     scale=frame.get("scale", 1.0), **capture_scope)
            screenshots += 1
            return captured

        def check_lease():
            self.store.check(sid, token)
            if time.monotonic() > deadline:
                raise U2CError("TIMEOUT", "Action batch exceeded its time budget")
            self.driver.assert_interactive()

        def checkpoint():
            check_lease()
            if self.driver.foreground() != expected_focus:
                raise U2CError("FOCUS_LOST", "Foreground changed during batch; observe before continuing")
            if window_mode and not single_window_click:
                self.driver.assert_window_input(expected_focus, frame.get("window_pid"), frame["bounds"])

        def click_checkpoint():
            checkpoint()
            self.driver.assert_window_input(expected_focus, frame["window_pid"], frame["bounds"], point=click_point)

        try:
            input_checkpoint = checkpoint
            if single_window_click:
                click = request["actions"][0]
                click_point = map_point(frame, click["x"], click["y"])
                input_checkpoint = click_checkpoint
            if needs_activation:
                check_lease()
                Guard(self.store).check(request)
                self.driver.activate_window_input(expected_focus, frame["window_pid"], frame["bounds"], check_lease)
            input_checkpoint()
            # The saved PNG hash proves file integrity, not current UI state.
            # Compare fresh screen pixels before sending the first input. An
            # ``auto`` session relaxes the exact-pixel rule only when two fresh
            # captures prove the observed scope is continuously animating (for
            # example Wallpaper Engine); a real, settled change still requires a
            # new observation. ``relaxed`` checks geometry/focus only.
            result_frame, pixel_guard = self._preflight(frame, recapture, checkpoint, guard)
            if not needs_activation:
                Guard(self.store).check(request)
            for index, action in enumerate(request["actions"]):
                input_checkpoint()
                at = time.perf_counter()
                try:
                    self._perform(action, frame, input_checkpoint, verify_click_position=single_window_click)
                except U2CError as exc:
                    receipts.append({"index": index, "type": action["type"], "status": "failed",
                                     "may_have_partially_executed": True, "error": exc.as_dict(),
                                     "duration_ms": round((time.perf_counter()-at)*1000, 3)})
                    raise
                receipts.append({"index": index, "type": action["type"], "status": "executed",
                                 "duration_ms": round((time.perf_counter()-at)*1000, 3)})
                # Every GUI input gets a fresh real image. Safe and Fast never skip
                # focus/page checks to obtain a flattering benchmark number.
                if action["type"] != "finish":
                    checkpoint()
                    retried_capture = False
                    if pixel_guard == "exact":
                        def retry_render_failure(exc):
                            nonlocal retried_capture
                            retried_capture = exc.code == "CAPTURE_FAILED" and exc.details.get("retryable") is True
                            if retried_capture:
                                receipts[-1]["post_capture_retried"] = True
                            return retried_capture

                        result_frame, _, stable = wait_for_stable_frame(
                            recapture,
                            lambda a, b: compare_frames(a["path"], b["path"]), checkpoint,
                            retry_capture=retry_render_failure if window_mode and action["type"] in INPUT_ACTIONS else None,
                            allow_unstable=True,
                        )
                        checkpoint()
                        if action["type"] != "observe":
                            change = compare_frames(baseline_path, result_frame["path"])
                            receipts[-1]["image_change"] = change
                            if not stable:
                                receipts[-1]["pixel_stability"] = "unstable (caret/video within budget)"
                                baseline_path = result_frame["path"]
                                continue
                            # A change or missed render requires fresh visual review
                            # before continuing, even if the recovered pixels match.
                            if (self._changed(change) or retried_capture) and index + 1 < len(request["actions"]):
                                stopped_for_change = True
                                stop_reason = "pixel_change"
                                break
                    else:
                        # An animating scope never reaches pixel quiescence.
                        # Capture once so the host still reviews real pixels,
                        # and do not stop the batch on animation-only deltas.
                        result_frame = recapture()
                        checkpoint()
                        if action["type"] != "observe":
                            receipts[-1]["image_change"] = compare_frames(
                                baseline_path, result_frame["path"], channel_tolerance=channel_tolerance)
                            receipts[-1]["pixel_stability"] = f"single-capture ({pixel_guard} guard)"
                    baseline_path = result_frame["path"]
                if action["type"] == "finish":
                    self.store.finish(sid)
                    break
            if result_frame is None and request.get("post_observe", True) and not any(a["type"] == "finish" for a in request["actions"]):
                checkpoint()
                result_frame = recapture()
        except U2CError as exc:
            # A click that opens or focuses another window moves the foreground
            # after the input already landed. That is a normal "observe again"
            # outcome, not an input failure, so it must not be reported as an
            # error next to a successful action.
            if exc.code == "FOCUS_LOST" and any(r["status"] == "executed" for r in receipts):
                stopped_for_change = True
                stop_reason = "focus_changed_after_input"
                try:
                    result_frame = recapture()
                except U2CError:
                    result_frame = None
            else:
                error = exc.as_dict()
                stop_reason = exc.code
        except KeyboardInterrupt:
            error = U2CError("STOP_REQUESTED", "Interrupted by user").as_dict()
            stop_reason = "stop_requested"
        except Exception as exc:
            error = U2CError("INPUT_FAILED", "Unexpected driver failure", exception=type(exc).__name__).as_dict()
            stop_reason = "input_failed"
        finally:
            try:
                self.driver.release_all()
            except Exception as exc:
                error = U2CError("PERMISSION_DENIED", "Input cleanup failed; manually release keys", exception=type(exc).__name__, original_error=error).as_dict()
            if window_mode and error:
                result_frame = None
            published_frame = self.store.release(sid, token, frame=result_frame)
        if published_frame is not None:
            result_frame = published_frame
            prune_owned_frames(self.store.root, sid, preserve=result_frame["path"])
        completed = sum(r["status"] == "executed" for r in receipts)
        status = "partial" if error and receipts else "error" if error else "observe_required" if stopped_for_change else "ok"
        receipt = {
            "status": status, "session_id": sid, "request_frame_id": request["frame_id"],
            "actions": receipts, "completed_actions": completed,
            "remaining_actions": len(request["actions"]) - len(receipts),
            "frame": result_frame, "error": error,
            "frame_published": published_frame is not None,
            "pixel_guard": pixel_guard,
            "stop_reason": stop_reason,
            "metrics": {"screenshots": screenshots, "actions_requested": len(request["actions"]),
                        "actions_completed": completed, "runtime_ms": round((time.perf_counter()-started)*1000, 3)},
            "visual_verification": "required_by_host_model",
        }
        self.store.record(sid, receipt)
        return receipt

    @staticmethod
    def _changed(change):
        if isinstance(change, dict):
            if "changed" in change:
                return bool(change["changed"])
            return float(change.get("changed_ratio", change.get("difference_ratio", 1))) > 0
        return bool(change)

    def _preflight(self, frame, recapture, checkpoint, guard):
        """Verify that the observed frame is still current before input.

        Returns ``(fresh_frame, guard_state)``. ``guard_state`` is ``exact`` when
        pixels still match the observation, ``animated`` when two fresh captures
        proved the scope is continuously animating (Wallpaper Engine, video), or
        ``relaxed`` when the session asked for geometry/focus checks only.
        A change that has settled raises STALE_FRAME so the host re-observes.
        """
        tolerance = guard["channel_tolerance"]
        budget = guard["changed_ratio_budget"]
        current = recapture()
        checkpoint()
        if frame.get("monitors") != current.get("monitors"):
            raise U2CError("STALE_FRAME", "Display topology or DPI changed; observe again")
        change = compare_frames(frame["path"], current["path"], channel_tolerance=tolerance)
        if not change["changed"] or change["changed_ratio"] <= budget:
            return current, "exact"
        if guard["mode"] == "strict":
            raise U2CError("STALE_FRAME",
                           "Screen pixels changed since observation; inspect the new frame before retrying",
                           change=change)
        if guard["mode"] == "relaxed":
            return current, "relaxed"
        second = recapture()
        checkpoint()
        animation = compare_frames(current["path"], second["path"], channel_tolerance=tolerance)
        if not animation["changed"] or animation["changed_ratio"] <= budget:
            raise U2CError("STALE_FRAME",
                           "Screen pixels changed and then settled; inspect the new frame before retrying",
                           change=change, animation=animation)
        return second, "animated"

    def _perform(self, action, frame, checkpoint, *, verify_click_position=False):
        kind = action["type"]
        if kind in ("click", "double_click", "move", "drag"):
            x, y = map_point(frame, action["x"], action["y"])
        if kind in ("click", "double_click"):
            options = {"verify_position": True} if verify_click_position else {}
            self.driver.click(x, y, button=action.get("button", "left"),
                              count=2 if kind == "double_click" else 1, checkpoint=checkpoint, **options)
        elif kind == "move":
            self.driver.move(x, y, duration=action.get("duration", 0), checkpoint=checkpoint)
        elif kind == "drag":
            tx, ty = map_point(frame, action["to_x"], action["to_y"])
            self.driver.drag(x, y, tx, ty, button=action.get("button", "left"),
                             duration=action.get("duration", .3), checkpoint=checkpoint)
        elif kind == "scroll":
            pos = map_point(frame, action["x"], action["y"]) if "x" in action else (None, None)
            self.driver.scroll(action["amount"], x=pos[0], y=pos[1], checkpoint=checkpoint)
        elif kind == "type":
            if "method" in action:
                self.driver.type_text(action["text"], checkpoint=checkpoint, method=action["method"])
            else:
                self.driver.type_text(action["text"], checkpoint=checkpoint)
        elif kind == "key":
            self.driver.key(action["key"], checkpoint=checkpoint)
        elif kind == "hotkey":
            self.driver.hotkey(action["keys"], checkpoint=checkpoint)
        elif kind == "wait":
            until = time.monotonic() + action["seconds"]
            while time.monotonic() < until:
                checkpoint()
                time.sleep(min(.02, max(0, until-time.monotonic())))
        elif kind not in ("observe", "finish"):
            raise U2CError("INVALID_REQUEST", "Unknown action")
