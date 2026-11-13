import html from "./index.html";
const favicon = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#071116"/><circle cx="32" cy="32" r="23" fill="none" stroke="#79c8be" stroke-width="2"/><ellipse cx="32" cy="32" rx="12" ry="23" transform="rotate(42 32 32)" fill="none" stroke="#9cf5da" stroke-width="2.4"/><ellipse cx="32" cy="32" rx="12" ry="23" transform="rotate(-42 32 32)" fill="none" stroke="#9cf5da" stroke-width="2.4"/><circle cx="32" cy="32" r="4" fill="#9cf5da"/></svg>';
const headers = { "X-Content-Type-Options":"nosniff", "Referrer-Policy":"strict-origin-when-cross-origin", "X-Frame-Options":"DENY", "Permissions-Policy":"camera=(), microphone=(), geolocation=(), payment=()", "Content-Security-Policy":"default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'; upgrade-insecure-requests" };
export default {
  async fetch(request) {
    if (request.method !== "GET" && request.method !== "HEAD") return new Response("Method Not Allowed",{status:405,headers:{Allow:"GET, HEAD"}});
    const u=new URL(request.url);
    if (u.pathname==="/favicon.svg") return new Response(request.method==="HEAD"?null:favicon,{headers:{"content-type":"image/svg+xml; charset=utf-8","cache-control":"public, max-age=86400",...headers}});
    if(u.pathname!=="/" && u.pathname!=="/index.html")return new Response("Not Found",{status:404,headers});
    return new Response(request.method==="HEAD"?null:html,{headers:{"content-type":"text/html; charset=utf-8","cache-control":"public, max-age=300",...headers}});
  }
};