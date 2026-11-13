"""Wait for observed pixel quiescence without claiming semantic GUI success."""
from __future__ import annotations

import math
import time

from .errors import U2CError


def _changed(comparison):
    if isinstance(comparison, bool):
        return comparison
    if isinstance(comparison, dict) and isinstance(comparison.get("changed"), bool):
        return comparison["changed"]
    raise U2CError("INVALID_REQUEST", "Frame comparator must return a boolean or a dictionary with boolean changed")


def wait_for_stable_frame(capture, compare, checkpoint, *, timeout=1.0, interval=.04, stable_for=.12,
                          retry_capture=None, allow_unstable=False):
    """Return ``(last_frame, capture_count, stable)`` after a quiet interval.

    ``capture()`` must obtain a fresh real screenshot. ``compare(previous,
    current)`` must compare exact pixels and return a changed boolean, or the
    dictionary produced by ``compare_frames``. Captures are not substituted with
    cached images. Every observed change restarts the continuous quiet interval.
    If ``retry_capture(error)`` accepts a capture error, allow one retry within
    the same budget. A missed sample also resets the quiet interval.

    With ``allow_unstable``, exhausting the deadline returns the last real
    capture with ``stable=False`` instead of raising TIMEOUT. A blinking caret
    or a video surface would otherwise fail an otherwise successful click; the
    host still reviews the returned PNG and the receipt records the instability.

    This establishes only that sampled pixels remained unchanged for the
    requested interval. It does not establish task completion or guarantee that
    an application has no future asynchronous update; the host must inspect the
    returned PNG. A cursor blink/animation can legitimately exhaust the deadline.
    """
    for name, value in (("timeout", timeout), ("interval", interval), ("stable_for", stable_for)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise U2CError("INVALID_REQUEST", f"{name} must be a positive finite number")
    if type(allow_unstable) is not bool:
        raise U2CError("INVALID_REQUEST", "allow_unstable must be a boolean")
    if not all(callable(function) for function in (capture, compare, checkpoint)):
        raise U2CError("INVALID_REQUEST", "Capture, compare and checkpoint must be callable")
    if retry_capture is not None and not callable(retry_capture):
        raise U2CError("INVALID_REQUEST", "Capture retry policy must be callable")

    started = time.perf_counter()
    deadline = started + timeout
    capture_count = 0

    def check_deadline():
        checkpoint()
        now = time.perf_counter()
        if now >= deadline:
            raise U2CError("TIMEOUT", "Screen pixels did not settle within the verification budget",
                           timeout=timeout, stable_for=stable_for, capture_count=capture_count,
                           elapsed=now - started)
        return now

    previous = None
    stable_since = None
    next_capture = started
    try:
        while True:
            now = check_deadline()
            # Sleep in short increments even if a caller chooses a long sample
            # interval. A stop/focus/permission checkpoint stays responsive.
            while now < next_capture:
                time.sleep(min(.04, next_capture - now))
                now = check_deadline()
            try:
                current = capture()
            except U2CError as exc:
                if retry_capture is None:
                    raise
                checkpoint()
                now = time.perf_counter()
                next_capture = now + max(interval, stable_for)
                if next_capture >= deadline or not retry_capture(exc):
                    raise
                retry_capture = None
                stable_since = None
                continue
            capture_count += 1
            check_deadline()
            changed = stable_since is None or _changed(compare(previous, current))
            now = check_deadline()
            if changed:
                stable_since = now
            previous = current
            if now - stable_since >= stable_for:
                return current, capture_count, True
            next_capture = min(now + interval, deadline)
    except U2CError as exc:
        if allow_unstable and exc.code == "TIMEOUT" and previous is not None:
            return previous, capture_count, False
        raise
