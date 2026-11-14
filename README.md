# Universal Computer Use (UCU)

> **UCU and U2C are the same project.** UCU is the public-facing name. The Python distribution, import package, CLI and Windows launcher retain the `ress-u2c` / `u2c` identifiers for compatibility; they do not refer to a separate implementation.

> **AI agents: read [README-For Agents.md](README-For%20Agents.md) first.** It contains installation, environment configuration, verification and operating steps. Do not start desktop actions before completing that guide.

A local, screenshot-grounded Windows GUI runtime: an AI observes the real desktop as pixels and drives the mouse and keyboard. No model service, no telemetry, no network listener.

- Requirements: Windows 10/11 x64 and **64-bit CPython 3.14** for the current offline bundle. The Python package metadata declares Python `>=3.11`, but the bundled offline wheelhouse currently provides a CPython 3.14 Pillow wheel only.
- Install: `py -3.14 install_u2c.py --root C:\Tools\u2c --bundle bundle`
- Verify: `C:\Tools\u2c\u2c-medium.cmd doctor --json`
- License: Apache-2.0 (see [LICENSE](LICENSE) and [NOTICE](NOTICE))

## Source code and release artifacts

The public release wheel at `bundle/assets/runtime/wheels/ress_u2c-0.1.0-py3-none-any.whl` contains the readable Python source package under `u2c/` (17 Python modules; not bytecode-only). The wheel is the artifact used by the verified offline installer.

To inspect the package source, extract the wheel:

```powershell
py -3.14 -m zipfile -e bundle/assets/runtime/wheels/ress_u2c-0.1.0-py3-none-any.whl .\u2c-wheel-source
```

The bundled Pillow wheel is platform- and interpreter-specific (`cp314-win_amd64`), which is why the current offline installer requires 64-bit CPython 3.14 even though the Python package itself declares a broader Python version range.

## Layout

| Path | Purpose |
|---|---|
| `README-For Agents.md` | Agent installation and operating guide (read this first) |
| `install_u2c.py` | Verify the offline bundle, install the wheel, write the medium-integrity launcher and run `doctor` |
| `bundle/assets/runtime/wheels/` | UCU runtime wheel, including its readable Python source package |
| `bundle/assets/runtime/wheelhouse/` | Offline dependencies and SHA-256 manifest |
| `website/` | Source for the official UCU landing page and Cloudflare Worker |

Official website: https://ucu.ress.ink
