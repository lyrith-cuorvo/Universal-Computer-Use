#!/usr/bin/env python3
"""Agent-facing U2C installer.

Reads the verified bundle manifest, installs the wheel into a private venv,
applies the development patch overlay, writes a medium-integrity launcher and
runs ``doctor``. Nothing is installed globally, PATH is not modified and no
directory is deleted.

Usage:
    python install_u2c.py --root C:\\Tools\\u2c [--bundle DIR] [--state DIR]

Exit codes: 0 ready, 2 installed but input blocked by integrity, 1 failure.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(command, **kwargs):
    return subprocess.run(command, check=True, capture_output=True, text=True, **kwargs)


def line_ending_hint(path):
    """Explain the class of failure caused by git rewriting bundle text files."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return ""
    if b"\r\n" in data:
        return (" (CRLF line endings detected: git converted this file. Re-clone with "
                "`git -c core.autocrlf=false clone ...`, or keep the repository's .gitattributes)")
    return ""


def fail(message):
    print(json.dumps({"status": "FAIL", "error": message}, indent=2))
    raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, help="absolute runtime root")
    parser.add_argument("--bundle", default=str(Path(__file__).resolve().parent),
                        help="directory containing assets/runtime and patches/")
    parser.add_argument("--state", default=None, help="U2C state directory")
    args = parser.parse_args()

    if sys.platform != "win32":
        fail("U2C requires Windows 10/11.")
    if sys.version_info < (3, 11):
        fail("Python 3.11+ is required.")

    root = Path(args.root).resolve()
    bundle = Path(args.bundle).resolve()
    assets = bundle / "assets" / "runtime"
    manifest_path = assets / "manifest.json"
    if not manifest_path.is_file():
        fail(f"missing runtime manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "u2c.skill-runtime.v0":
        fail("unknown runtime manifest schema")

    wheel = assets / manifest["wheel"]
    if sha256(wheel) != manifest["wheel_sha256"].lower():
        fail("WHEEL_HASH_MISMATCH" + line_ending_hint(wheel))

    wheelhouse = assets / manifest["wheelhouse"]
    sums = wheelhouse / "SHA256SUMS"
    if sha256(sums) != manifest["wheelhouse_manifest_sha256"].lower():
        fail("WHEELHOUSE_MANIFEST_HASH_MISMATCH" + line_ending_hint(sums))
    listed = {}
    for line in sums.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, name = line.split("  ", 1)
        listed[name] = digest.lower()
    for dependency in wheelhouse.glob("*.whl"):
        expected = listed.get(dependency.name)
        if expected is None or sha256(dependency) != expected:
            fail(f"DEPENDENCY_HASH_MISMATCH: {dependency.name}")

    version = manifest["version"]
    install_root = root / "versions" / f"{version}-{manifest['wheel_sha256'][:12]}"
    python = install_root / "venv" / "Scripts" / "python.exe"
    cli = install_root / "venv" / "Scripts" / "u2c.exe"
    marker = install_root / "u2c-install.json"

    changed = False
    if marker.is_file():
        recorded = json.loads(marker.read_text(encoding="utf-8"))
        if recorded.get("sha256") != manifest["wheel_sha256"]:
            fail("existing installation marker mismatch; target retained")
        run([str(python), "-c",
             "import importlib.metadata, u2c; "
             f"assert importlib.metadata.version('ress-u2c') == '{version}'"])
    else:
        install_root.mkdir(parents=True, exist_ok=True)
        run([sys.executable, "-m", "venv", str(install_root / "venv")])
        run([str(python), "-m", "pip", "--isolated", "--disable-pip-version-check",
             "install", "--only-binary=:all:", "--no-index",
             "--find-links", str(wheelhouse), str(wheel)])
        marker.write_text(json.dumps({"version": version, "sha256": manifest["wheel_sha256"]}),
                          encoding="utf-8")
        changed = True

    # Development overlay: keep the patched runtime that the bug-fix work produced.
    site_packages = install_root / "venv" / "Lib" / "site-packages"
    patched = bundle / "patches" / "u2c-patched"
    if not patched.is_dir():
        patched = Path(__file__).resolve().parent / "patches" / "u2c-patched"
    patched_applied = False
    if patched.is_dir():
        target = site_packages / "u2c"
        if not target.is_dir():
            fail(f"installed package not found: {target}")
        for source in patched.rglob("*.py"):
            destination = target / source.relative_to(patched)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
        patched_applied = True

    state = Path(args.state).resolve() if args.state else root / "state"
    state.mkdir(parents=True, exist_ok=True)
    launcher = root / "u2c-medium.cmd"
    launcher.write_text(
        "@echo off\r\n"
        "setlocal\r\n"
        f'set "PYTHONPATH={site_packages}"\r\n'
        f'set "U2C_STATE_DIR={state}"\r\n'
        f'"{sys.executable}" -m u2c %*\r\n'
        "exit /b %ERRORLEVEL%\r\n",
        encoding="ascii",
    )

    doctor = json.loads(run(["cmd", "/c", str(launcher), "doctor", "--json"]).stdout)
    version_line = run(["cmd", "/c", str(launcher), "--version"]).stdout.strip()
    capabilities = doctor.get("capabilities", {})
    integrity = doctor.get("driver", {}).get("integrity", {}).get("level", "unknown")
    input_ok = bool(capabilities.get("input_integrity_ok"))
    warnings = []
    if "input_integrity_ok" not in capabilities:
        warnings.append("runtime does not report integrity; the patched overlay is missing")
    elif not input_ok:
        warnings.append(
            f"launcher ran at '{integrity}' integrity; desktop input needs medium integrity. "
            "Run it from an unsandboxed host shell or a normal terminal.")
    if not patched_applied:
        warnings.append("patched overlay not found; installed the unpatched bundle")

    summary = {
        "status": "READY" if input_ok else "INSTALLED_INPUT_BLOCKED",
        "changed": changed,
        "version": version_line.replace("U2C ", ""),
        "cli": str(cli),
        "launcher": str(launcher),
        "python": str(python),
        "state_dir": str(state),
        "integrity": integrity,
        "input_integrity_ok": input_ok,
        "patched": patched_applied,
        "warnings": warnings,
    }
    print(json.dumps(summary, indent=2))
    return 0 if input_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
