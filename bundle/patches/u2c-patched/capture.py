"""Real-frame metadata, physical coordinates and conservative pixel comparison."""
from __future__ import annotations

import hashlib
import math
import time
import uuid
from pathlib import Path

from PIL import Image, ImageChops

from .drivers.windows import virtual_bounds
from .errors import U2CError

DESKTOP_SOURCE = "win32_gdi_screen"
WINDOW_CLIENT_SOURCE = "win32_window_client"
GUARD_MODES = ("strict", "auto", "relaxed")
DEFAULT_GUARD = {"mode": "auto", "channel_tolerance": 24, "changed_ratio_budget": 0.0}


def guard_settings(guard=None):
    """Normalize the pixel-guard profile recorded with a frame.

    ``strict`` requires exact pixels, ``auto`` relaxes only when the observed
    scope is proven to be continuously animating, and ``relaxed`` checks only
    window geometry, occlusion and foreground focus.
    """
    settings = dict(DEFAULT_GUARD)
    explicit_tolerance = isinstance(guard, dict) and "channel_tolerance" in guard
    if guard is not None:
        if not isinstance(guard, dict):
            raise U2CError("INVALID_REQUEST", "guard must be an object")
        unknown = set(guard) - set(DEFAULT_GUARD)
        if unknown:
            raise U2CError("INVALID_REQUEST", "Unknown guard fields", fields=sorted(map(str, unknown)))
        settings.update(guard)
    if settings["mode"] not in GUARD_MODES:
        raise U2CError("INVALID_REQUEST", "guard mode must be strict, auto or relaxed")
    if settings["mode"] == "strict" and not explicit_tolerance:
        # strict means exact RGB equality, independent of the shared default.
        settings["channel_tolerance"] = 0
    tolerance = settings["channel_tolerance"]
    if isinstance(tolerance, bool) or not isinstance(tolerance, int) or not 0 <= tolerance <= 255:
        raise U2CError("INVALID_REQUEST", "channel_tolerance must be an integer in [0, 255]")
    budget = settings["changed_ratio_budget"]
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not math.isfinite(budget) or not 0 <= budget <= 1:
        raise U2CError("INVALID_REQUEST", "changed_ratio_budget must be a finite number in [0, 1]")
    settings["changed_ratio_budget"] = float(budget)
    return settings


def _region(region):
    if isinstance(region, dict) and set(region) == {"left", "top", "width", "height"}:
        result = dict(region)
    elif isinstance(region, (list, tuple)) and len(region) == 4:
        result = dict(zip(("left", "top", "width", "height"), region))
    else:
        raise U2CError("OUT_OF_BOUNDS", "Region requires physical left, top, width and height")
    if any(isinstance(v, bool) or not isinstance(v, int) for v in result.values()):
        raise U2CError("OUT_OF_BOUNDS", "Physical region values must be integers")
    if result["width"] < 1 or result["height"] < 1:
        raise U2CError("OUT_OF_BOUNDS", "Physical region must have positive dimensions")
    return result


def _contains(outer, inner):
    return (outer["left"] <= inner["left"] and outer["top"] <= inner["top"] and
            inner["left"] + inner["width"] <= outer["left"] + outer["width"] and
            inner["top"] + inner["height"] <= outer["top"] + outer["height"])


def _overlaps(first, second):
    return (max(first["left"], second["left"]) < min(first["left"] + first["width"], second["left"] + second["width"])
            and max(first["top"], second["top"]) < min(first["top"] + first["height"], second["top"] + second["height"]))


def capture_frame(driver, output_dir, *, monitor=None, region=None, scale=1.0, window_client=None, window_pid=None,
                  guard=None):
    """Capture PNG pixels through the native driver, retaining unscaled bounds.

    `region` is an absolute physical desktop rectangle, even with `monitor`.
    Resizing changes image dimensions only; normalized coordinates continue to
    map onto the original physical rectangle. DPI factors must never be applied
    twice to the physical bounds.
    """
    settings = guard_settings(guard)
    if isinstance(scale, bool) or not isinstance(scale, (float, int)) or not math.isfinite(scale) or not 0.05 <= scale <= 1:
        raise U2CError("INVALID_REQUEST", "Screenshot scale must be between 0.05 and 1")
    if window_client is not None and (type(window_client) is not int or not 0 < window_client < 2**64):
        raise U2CError("INVALID_REQUEST", "Window client requires a positive HWND")
    if window_pid is not None and (type(window_pid) is not int or not 0 < window_pid < 2**32):
        raise U2CError("INVALID_REQUEST", "Window PID must be a positive Windows process ID")
    if (window_client is not None or window_pid is not None) and (monitor is not None or region is not None):
        raise U2CError("INVALID_REQUEST", "Window capture cannot be combined with a monitor or region")
    before = driver.assert_interactive()
    if window_client is None and window_pid is not None:
        window_client = driver.resolve_window_client(window_pid)
    monitors = before["monitors"]
    selected = virtual_bounds(monitors)
    if monitor is not None:
        matches = [m for m in monitors if str(m["id"]) == str(monitor) or m.get("device") == monitor]
        if len(matches) != 1:
            raise U2CError("OUT_OF_BOUNDS", "Requested monitor was not found", monitor=monitor)
        selected = {k: matches[0][k] for k in ("left", "top", "width", "height")}
    if window_client is not None:
        client_bounds, pid = driver.window_client(window_client)
        if window_pid is not None and pid != window_pid:
            raise U2CError("WINDOW_UNAVAILABLE", "The selected handle now belongs to a different process")
        bounds = _region(client_bounds)
        # A bound HWND renders independently of the desktop. Keep its full
        # client coordinates, with off-display input points rejected by map_point.
        if not any(_overlaps(bounds, display) for display in monitors):
            raise U2CError("OUT_OF_BOUNDS", "Window client does not intersect a physical display")
    else:
        bounds = _region(region) if region is not None else selected
        if not _contains(selected, bounds):
            raise U2CError("OUT_OF_BOUNDS", "Capture region is outside the selected physical desktop")
    if bounds["width"] * bounds["height"] > 64_000_000:
        raise U2CError("OUT_OF_BOUNDS", "Screenshot exceeds the 64 million pixel budget")
    started_at = time.time()
    if window_client is None:
        image = driver.capture_pixels(bounds)
        source = {"pixel_source": DESKTOP_SOURCE}
    else:
        image = driver.capture_window_pixels(window_client, bounds, pid)
        source = {"pixel_source": WINDOW_CLIENT_SOURCE, "window_hwnd": window_client, "window_pid": pid}
    if image.size != (bounds["width"], bounds["height"]):
        raise U2CError("CAPTURE_FAILED", "Native capture returned unexpected dimensions")
    after = driver.assert_interactive()
    if before["monitors"] != after["monitors"]:
        raise U2CError("STALE_FRAME", "Display configuration changed during capture; observe again")
    if window_client is None and before.get("foreground_hwnd") != after.get("foreground_hwnd"):
        raise U2CError("FOCUS_LOST", "Foreground changed during capture; observe again")
    if scale != 1:
        image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                             Image.Resampling.LANCZOS)
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    frame_id = str(uuid.uuid4())
    path = output / f"{frame_id}.png"
    image.save(path, format="PNG")
    return {"frame_id": frame_id, "created_at": started_at, "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bounds": dict(bounds),
            "image": {"width": image.width, "height": image.height}, "scale": float(scale),
            "monitors": monitors, "foreground_hwnd": after.get("foreground_hwnd", 0),
            "thread_desktop": after.get("thread_desktop"), "input_desktop": after.get("input_desktop"),
            "coordinate_space": "normalized_1000", "guard": settings, **source}


def map_point(frame, x, y):
    """Normalized endpoints map to inclusive physical pixels, including crops."""
    for value in (x, y):
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= 1000:
            raise U2CError("OUT_OF_BOUNDS", "Normalized coordinates must be finite numbers from 0 to 1000")
    bounds = _region(frame.get("bounds"))
    point = (bounds["left"] + round(x * (bounds["width"] - 1) / 1000),
             bounds["top"] + round(y * (bounds["height"] - 1) / 1000))
    if frame.get("monitors") and not any(
        m["left"] <= point[0] < m["left"] + m["width"] and
        m["top"] <= point[1] < m["top"] + m["height"] for m in frame["monitors"]
    ):
        raise U2CError("OUT_OF_BOUNDS", "Normalized point maps into a gap outside all physical monitors")
    return point


def compare_frames(path_a, path_b, channel_tolerance=0):
    """Measure RGB pixel changes above a per-channel noise floor.

    ``channel_tolerance`` is a per-channel absolute delta in 0-255. A pixel
    counts as changed only when at least one channel differs by more than that
    value. The default zero keeps the original exact-pixel contract; a positive
    tolerance is useful for animated wallpaper and video surfaces where exact
    equality is unattainable but the UI structure the model read is unchanged.
    The result never claims semantic vision.
    """
    if isinstance(channel_tolerance, bool) or not isinstance(channel_tolerance, int) or not 0 <= channel_tolerance <= 255:
        raise U2CError("INVALID_REQUEST", "channel_tolerance must be an integer in [0, 255]")
    with Image.open(path_a) as source_a, Image.open(path_b) as source_b:
        a, b = source_a.convert("RGB"), source_b.convert("RGB")
        if a.size != b.size:
            return {"changed": True, "changed_ratio": 1.0, "difference_ratio": 1.0,
                    "dimensions_changed": True}
        diff = ImageChops.difference(a, b)
        # Per-pixel OR across channels, without discarding small changes during
        # greyscale conversion; even a one-channel +1 invalidates a stable frame.
        red, green, blue = diff.split()
        mask = ImageChops.lighter(ImageChops.lighter(red, green), blue)
        if channel_tolerance:
            table = bytes(0 if value <= channel_tolerance else 255 for value in range(256))
            mask = mask.point(table)
        histogram = mask.histogram()
        changed = a.width * a.height - histogram[0]
        ratio = changed / (a.width * a.height)
        return {"changed": changed > 0, "changed_pixels": changed, "changed_ratio": ratio,
                "difference_ratio": ratio, "dimensions_changed": False,
                "channel_tolerance": channel_tolerance}
