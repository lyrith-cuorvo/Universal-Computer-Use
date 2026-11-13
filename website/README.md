# UCU Website

Official site for **RESS — Universal Computer Use**.

- Primary hostname: https://ucu.ress.ink
- Cloudflare Worker: `ress-ucu-site`
- This directory includes the standalone landing page (`index.html`) and the Worker entrypoint (`worker.js`).
- Deployment is to a Cloudflare Worker with `worker.js` as the main module and `index.html` as an imported text module.
- No analytics, cookies, remote desktop access or model API calls are implemented by this website.
- Language selector: English / 简体中文.
- This website is a **development preview**, not a released installable UCU runtime.

The execution architecture is conceptual and does not claim the UCU application is ready for production.
