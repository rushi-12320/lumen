/* Lumen Cloud: account bar, organisation switcher and banners for the console. Loaded before app.js. */
(() => {
  "use strict";
  const C = window.LUMEN_CLOUD;
  if (!C) return;
  const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = n => Number(n || 0).toLocaleString("en-IN");
  document.body.classList.add("lumen-cloud");

  // an expired session sends the person to the login page instead of showing errors
  const realFetch = window.fetch.bind(window);
  window.fetch = async (u, o) => {
    const r = await realFetch(u, o);
    if (r.status === 401 && typeof u === "string" && u.startsWith("/api/")) location.href = "/login?next=/app";
    return r;
  };

  // decisions are signed with the account, not a typed name (app.js reads "operator" from localStorage)
  try { localStorage.setItem("operator", C.user.name); } catch { /* */ }
  const op = document.getElementById("operator");
  if (op) { op.value = C.user.name; op.readOnly = true; op.title = "Every decision is signed with your Lumen account"; }

  const L = C.usage.limits, pct = Math.round(100 * C.usage.traces / Math.max(1, L.traces_per_month));
  const bar = document.createElement("div");
  bar.className = "cloud-acct";
  bar.innerHTML = `<select id="cloud-org" aria-label="Organisation">${C.orgs.map(o => `<option value="${esc(o.id)}" ${o.id === C.org.id ? "selected" : ""}>${esc(o.name)}</option>`).join("")}<option value="__new">+ New organisation…</option></select>
    <a class="cloud-plan" href="/app/settings#billing" title="${num(C.usage.traces)} of ${num(L.traces_per_month)} traces this month">${esc(L.name)} · ${pct}% used</a>
    <a href="/app/settings">Settings</a><button id="cloud-logout" class="cloud-link" type="button">Log out</button>`;
  document.querySelector("header.top").appendChild(bar);
  const links = document.createElement("div");
  links.className = "cloud-side";
  links.innerHTML = `<a href="/app/onboarding">↳ Get started</a><a href="/docs" target="_blank" rel="noopener">↳ Docs</a><a href="/app/settings#team">↳ Team</a>${C.admin ? '<a href="/admin">↳ Admin</a>' : ""}`;
  const side = document.getElementById("side"), foot = document.getElementById("side-foot");
  if (side && foot) side.insertBefore(links, foot);

  const main = document.querySelector(".main"), ticker = document.getElementById("ticker");
  const banner = html => { const b = document.createElement("div"); b.className = "cloud-banner"; b.innerHTML = html; main.insertBefore(b, ticker); };
  if (!C.user.verified) banner(`Please confirm your email address (${esc(C.user.email)}) to invite teammates, upgrade and run LLM jobs. <a href="/app/settings#account">Resend the link</a>`);
  if (pct >= 100) banner(`This month's trace limit is reached, so new traces are refused. <a href="/app/settings#billing">Upgrade</a>`);
  else if (pct >= 80) banner(`You have used ${pct}% of this month's traces. <a href="/app/settings#billing">See plans</a>`);

  document.getElementById("cloud-org").onchange = async e => {
    if (e.target.value === "__new") { location.href = "/app/settings#account"; return; }
    await realFetch("/auth/switch-org", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ org_id: e.target.value }) });
    try { localStorage.removeItem("project"); } catch { /* */ }
    location.href = "/app";
  };
  document.getElementById("cloud-logout").onclick = async () => { await realFetch("/auth/logout", { method: "POST" }); location.href = "/"; };
})();
