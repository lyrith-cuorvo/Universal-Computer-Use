# Universal Computer Use (U2C)

> **给 AI Agent：请先阅读 [README-For Agents.md](README-For%20Agents.md)，按其中的步骤完成安装、环境配置与验证，再开始操作桌面。**
>
> **AI agents: read [README-For Agents.md](README-For%20Agents.md) first. It contains the installation, configuration and verification steps.**

本仓库是一个本地、基于真实截图的 Windows GUI 运行时：AI 通过截图观察桌面，用鼠标和键盘完成任务，不依赖模型服务，也不开网络端口。

- 安装：`py -3.14 install_u2c.py --root C:\Tools\u2c --bundle bundle`
- 自检：`C:\Tools\u2c\u2c-medium.cmd doctor --json`
- 许可证：Apache-2.0（见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)）

## 目录

| 路径 | 说明 |
|---|---|
| `README-For Agents.md` | Agent 安装/操作指南（先读这个） |
| `install_u2c.py` | 校验 + 安装 + 打补丁 + 生成 Medium 启动器 |
| `bundle/` | 离线 wheel、wheelhouse、补丁与清单 |