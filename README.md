# Universal Computer Use (U2C)

> **AI agents: read [README-For Agents.md](README-For%20Agents.md) first.** It contains the installation, environment configuration, verification and operating steps. Do not start desktop actions before completing that guide.

A local, screenshot-grounded Windows GUI runtime: an AI observes the real desktop as pixels and drives the mouse and keyboard. No model service, no telemetry, no network listener.

- Install: `py -3.14 install_u2c.py --root C:\Tools\u2c --bundle bundle`
- Verify: `C:\Tools\u2c\u2c-medium.cmd doctor --json`
- License: Apache-2.0 (see [LICENSE](LICENSE) and [NOTICE](NOTICE))

## Layout

| Path | Purpose |
|---|---|
| `README-For Agents.md` | Agent installation and operating guide (read this first) |
| `install_u2c.py` | Verify, install, apply patches and write the medium-integrity launcher |
| `bundle/` | Offline wheel, wheelhouse, patch overlay and manifest |