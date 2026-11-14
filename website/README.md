# UCU Website

Official website for **RESS — Universal Computer Use (UCU)**.

- Primary hostname: https://ucu.ress.ink
- Cloudflare Worker: `ress-ucu-site`
- `index.html` and `worker.js` in this directory are synchronized with the deployed landing page and Worker entrypoint.
- Deployment uses `worker.js` as the Worker module and imports `index.html` as a text module.
- The page links directly to the public source repository: https://github.com/lyrith-cuorvo/Universal-Computer-Use
- UCU and U2C are the same project. The Python distribution/package/CLI retain the `ress-u2c` and `u2c` identifiers.
- The public release wheel includes the readable Python runtime source package; the landing page is informational and does not itself execute desktop-control actions.
- No analytics, cookies, remote desktop access or model API calls are implemented by this website.
- Language selector: English / 简体中文.
