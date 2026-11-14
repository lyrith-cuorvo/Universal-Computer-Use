# Universal Computer Use (U2C)

> **AI agents: read [README-For Agents.md](README-For%20Agents.md) first.** It contains the installation, environment configuration, verification and operating steps. Do not start desktop actions before completing that guide.

A local, screenshot-grounded Windows GUI runtime: an AI observes the real desktop as pixels and drives the mouse and keyboard. No model service, no telemetry, no network listener.

- Requirements: Windows 10/11 x64 and **64-bit CPython 3.14** (the bundled offline Pillow wheel is `cp314-win_amd64`; Python 3.11–3.13 are not supported by this bundle).
- Install: `py -3.14 install_u2c.py --root C:\Tools\u2c --bundle bundle`
- Verify: `C:\Tools\u2c\u2c-medium.cmd doctor --json`
- License: Apache-2.0 (see [LICENSE](LICENSE) and [NOTICE](NOTICE))

## Layout

| Path | Purpose |
|---|---|
| `README-For Agents.md` | Agent installation and operating guide (read this first) |
| `install_u2c.py` | Verify the offline bundle, install the wheel, write the medium-integrity launcher and run `doctor` |
| `bundle/` | Verified runtime wheel, offline dependency wheelhouse and manifest |