/* Public demo console: the same console, talking to the visitor's private demo copy under /demo/api. */
(() => {
  "use strict";
  window.LUMEN_DEMO = { signup: "/signup" };       // the console offers sign-up as the next step
  const fix = u => (typeof u === "string" && u.startsWith("/api/")) ? "/demo" + u : u;
  const realFetch = window.fetch.bind(window);
  let reloading = false;
  const mayReload = () => {          // at most once a minute, so a browser that drops cookies cannot loop
    try {
      const t = Number(sessionStorage.getItem("lumenDemoReload") || 0);
      if (Date.now() - t < 60000) return false;
      sessionStorage.setItem("lumenDemoReload", String(Date.now()));
      return true;
    } catch { return false; }
  };
  window.fetch = async (u, o) => {
    const r = await realFetch(fix(u), o);
    if (r.status === 409 && !reloading) {          // the visitor's demo copy expired: open a fresh one
      try {
        const d = await r.clone().json();
        if (d.reload) {
          reloading = true;
          if (mayReload()) location.href = d.reload;
          else { const n = document.querySelector(".cloud-plan"); if (n) n.textContent = "Demo paused: allow cookies for this site, then reload"; }
        }
      } catch { /* */ }
    }
    return r;
  };
  const ES = window.EventSource;
  if (ES) {
    const Demo = function (u, o) { return new ES(fix(u), o); };
    Demo.prototype = ES.prototype;
    window.EventSource = Demo;
  }
  try { if (!(localStorage.getItem("project") || "").startsWith("PRJ-SUPPORT")) localStorage.setItem("project", "PRJ-SUPPORT"); } catch { /* */ }
  const bar = document.createElement("div");
  bar.className = "cloud-acct";
  bar.innerHTML = '<span class="cloud-plan">Live demo with sample data</span><a href="/">Home</a><a href="/docs">Docs</a><a class="cloud-cta" href="/signup">Start free</a>';
  document.querySelector("header.top").appendChild(bar);
})();
