/* Lumen website + account pages: forms, pricing toggle, onboarding, settings, admin. Plain JavaScript. */
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = n => Number(n || 0).toLocaleString("en-IN");
const day = ts => ts ? new Date(ts).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) : "–";
const store = { get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } }, set(k, v) { try { localStorage.setItem(k, v); } catch { /* */ } } };

async function api(path, opts = {}) {
  const init = { method: opts.method || (opts.body !== undefined ? "POST" : "GET"), headers: { "Content-Type": "application/json" }, credentials: "same-origin" };
  if (opts.body !== undefined) init.body = JSON.stringify(opts.body);
  const r = await fetch(path, init);
  let j = null;
  try { j = await r.json(); } catch { /* */ }
  if (!r.ok) {
    const e = new Error((j && (typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail))) || r.statusText);
    e.status = r.status; e.data = j; throw e;
  }
  return j;
}
function say(el, text, kind = "ok") { if (!el) { alert(text); return; } el.textContent = text; el.className = `msg show ${kind}`; }
function copyBtn(btn, text) { btn.onclick = async () => { try { await navigator.clipboard.writeText(text); btn.textContent = "Copied"; setTimeout(() => btn.textContent = "Copy", 1500); } catch { /* */ } }; }

/* ------------------------------------------------------------------ header menu, currency */
$("[data-menu]")?.addEventListener("click", () => $(".site-head").classList.toggle("open"));
const tz = (Intl.DateTimeFormat().resolvedOptions().timeZone || "");
let CUR = store.get("lumen_cur", /Calcutta|Kolkata/.test(tz) ? "INR" : "USD");
function setCur(c) { CUR = c; store.set("lumen_cur", c); document.body.classList.remove("cur-INR", "cur-USD"); document.body.classList.add(`cur-${c}`); $$(".cur-toggle button").forEach(b => b.classList.toggle("on", b.dataset.set === c)); }
$$(".cur-toggle button").forEach(b => b.onclick = () => setCur(b.dataset.set));
setCur(CUR);

/* ------------------------------------------------------------------ forms posting JSON */
const Q = new URLSearchParams(location.search);
$$("form[data-api]").forEach(f => {
  $$("[data-q]", f).forEach(el => { const v = Q.get(el.dataset.q); if (v) { el.value = v; if (el.dataset.lock) el.readOnly = true; } });
  f.addEventListener("submit", async e => {
    e.preventDefault();
    const msg = $(".msg", f), btn = $("button[type=submit]", f), body = {};
    $$("input[name], select[name], textarea[name]", f).forEach(el => { body[el.name] = el.type === "checkbox" ? el.checked : el.value; });
    if (f.dataset.confirm && body.password !== body.password2) { say(msg, "The two passwords are different.", "bad"); return; }
    delete body.password2;
    btn.disabled = true;
    try {
      const r = await api(f.dataset.api, { body });
      if (r && r.next) { location.href = r.next; return; }
      say(msg, (r && r.note) || f.dataset.ok || "Done.", "ok");
      if (f.dataset.reset !== undefined) f.reset();
    } catch (err) { say(msg, err.message, "bad"); }
    finally { btn.disabled = false; }
  });
});
$$("[data-logout]").forEach(b => b.onclick = async e => { e.preventDefault(); const r = await api("/auth/logout", { method: "POST" }); location.href = r.next || "/"; });

/* ------------------------------------------------------------------ invite page */
if ($("#invite")) {
  const token = $("#invite").dataset.token;
  $("#join-btn")?.addEventListener("click", async () => {
    try { const r = await api("/auth/accept-invite", { body: { token } }); location.href = r.next; }
    catch (err) { say($("#invite .msg"), err.message, "bad"); }
  });
  api("/api/me").then(me => { $("#invite-in").hidden = false; $("#invite-out").hidden = true; $("#invite-who").textContent = me.user.email; })
    .catch(() => { /* signed out: show the sign-up form */ });
}

/* ------------------------------------------------------------------ onboarding */
async function onboarding() {
  const root = $("#onboarding");
  let me;
  try { me = await api("/api/me"); } catch { location.href = "/login?next=/app/onboarding"; return; }
  if (!me.org) {
    root.innerHTML = `<h1>Create your organisation</h1><form class="card" id="neworg"><div class="msg"></div><div class="field"><label>Organisation name</label>
      <input name="name" type="text" required placeholder="Acme AI"></div><button class="btn primary" type="submit">Create</button></form>`;
    $("#neworg").onsubmit = async e => { e.preventDefault(); try { const r = await api("/api/orgs", { body: { name: e.target.name.value } }); location.href = r.next; } catch (err) { say($("#neworg .msg"), err.message, "bad"); } };
    return;
  }
  const host = location.origin, pr = me.projects[0] || {}, admin = ["admin", "owner"].includes(me.role);
  $("#ob-org").textContent = me.org.name;
  $("#ob-project").textContent = pr.name || "–";
  if (!me.user.verified) {
    $("#ob-verify").hidden = false;
    $("#ob-resend").onclick = async () => { try { const r = await api("/auth/resend-verification", { method: "POST" }); say($("#ob-verify .msg"), r.note); } catch (err) { say($("#ob-verify .msg"), err.message, "bad"); } };
  }
  const meta = await api("/api/meta").catch(() => null);
  const wheel = meta && meta.sdk ? meta.sdk.wheel : "/sdk/lumen_sdk.py";
  const code = key => ({
    py: `pip install ${host}${wheel}\n\n# in your app, once at startup:\nimport lumen_sdk\nlumen_sdk.init(api_key="${key}", host="${host}")\n\n# that's all: OpenAI, Anthropic and Gemini calls are traced from here on, e.g.\n# client.chat.completions.create(model="gpt-4o-mini", messages=[...])`,
    rag: `import lumen_sdk\nlumen_sdk.init(api_key="${key}", host="${host}")\n\n# one request = one trace; the retrieval span turns on the groundedness check\nwith lumen_sdk.trace("support_answer", input=question, user_id=user_id) as t:\n    with t.span("retrieval", name="kb_search", input=question) as s:\n        s.output = docs\n    answer = client.chat.completions.create(model="gpt-4o-mini", messages=messages)\n    t.output = answer.choices[0].message.content`,
    otel: `# JS/TS, Go, Java, LangChain, LlamaIndex, Vercel AI SDK: any OpenTelemetry exporter\nOTEL_EXPORTER_OTLP_TRACES_ENDPOINT=${host}/v1/otlp/v1/traces\nOTEL_EXPORTER_OTLP_TRACES_HEADERS=x-lumen-key=${key}`,
    curl: `curl -X POST ${host}/v1/traces \\\n  -H "X-Lumen-Key: ${key}" -H "Content-Type: application/json" \\\n  -d '{"traces":[{"name":"hello","input":"Hi","output":"Hello! How can I help?","model":"my-model","latency_ms":420,"tokens_in":12,"tokens_out":8}]}'`,
  });
  const show = key => {
    const c = code(key), saved = store.get("lumen_ob_tab", "py"), tab = c[saved] ? saved : "py";
    $("#ob-code").textContent = c[tab];
    $$("[data-tab]").forEach(b => { b.classList.toggle("primary", b.dataset.tab === tab); b.onclick = () => { store.set("lumen_ob_tab", b.dataset.tab); show(key); }; });
    copyBtn($("#ob-copy"), c[tab]);
  };
  show("lmn_YOUR_KEY");
  if (!admin) { $("#ob-key-btn").disabled = true; $("#ob-key-note").textContent = "Ask an admin or owner of your organisation to create a key for you."; }
  api(`/api/keys?project=${encodeURIComponent(pr.id)}`).then(ks => {
    const live = (ks || []).filter(k => !k.revoked);
    if (live.length) {
      $("#ob-step1").classList.add("done");
      $("#ob-key-btn").textContent = "Create another key";
      $("#ob-key-note").textContent = `You already have ${live.length} key${live.length > 1 ? "s" : ""} (${live.map(k => k.prefix + "…").join(", ")}). Keys are shown only once; create a new one if you lost it.`;
    }
  }).catch(() => { /* */ });
  $("#ob-key-btn").onclick = async () => {
    try {
      const r = await api("/api/keys", { body: { project: pr.id, name: "first key", by: me.user.name } });
      if (!r.ok) throw new Error(r.reason || "could not create the key");
      $("#ob-key").hidden = false; $("#ob-key-val").textContent = r.key; copyBtn($("#ob-key-copy"), r.key);
      $("#ob-step1").classList.add("done"); $("#ob-key-btn").disabled = true; show(r.key);
    } catch (err) { say($("#ob-key-msg"), err.message, "bad"); }
  };
  const poll = async () => {
    try {
      const ps = await api("/api/projects");
      const p = ps.find(x => x.id === pr.id) || ps[0];
      const fromApp = p ? p.traces - (p.playground || 0) : 0;          // sample-bot traces do not count here
      if (fromApp > 0) {
        $("#ob-step2").classList.add("done");
        $("#ob-wait").innerHTML = `<b>First trace received.</b> ${fromApp} so far.`;
        $("#ob-open").classList.add("primary"); return;
      }
    } catch { /* */ }
    setTimeout(poll, 3000);
  };
  poll();
  $("#ob-invite").onsubmit = async e => {
    e.preventDefault();
    try { const r = await api("/api/org/invites", { body: { email: e.target.email.value, role: e.target.role.value } }); say($("#ob-invite .msg"), r.note); e.target.reset(); }
    catch (err) { say($("#ob-invite .msg"), err.message, "bad"); }
  };
}
if ($("#onboarding")) onboarding();

/* ------------------------------------------------------------------ settings */
async function settings() {
  let me;
  try { me = await api("/api/me"); } catch { location.href = "/login?next=/app/settings"; return; }
  const admin = ["admin", "owner"].includes(me.role), owner = me.role === "owner";
  const tabs = () => { const t = (location.hash || "#account").slice(1); $$(".tabs a").forEach(a => a.classList.toggle("on", a.hash === `#${t}`)); $$(".panel").forEach(p => p.classList.toggle("on", p.id === `p-${t}`)); };
  window.addEventListener("hashchange", tabs); tabs();
  // account
  $("#acc-name").value = me.user.name; $("#acc-email").textContent = me.user.email;
  $("#acc-verified").innerHTML = me.user.verified ? `<span class="pill ok">confirmed</span>` : `<span class="pill warn">not confirmed</span> <button class="btn small" id="acc-resend">Resend link</button>`;
  $("#acc-resend")?.addEventListener("click", async () => { try { const r = await api("/auth/resend-verification", { method: "POST" }); say($("#acc-msg"), r.note); } catch (err) { say($("#acc-msg"), err.message, "bad"); } });
  $("#acc-form").onsubmit = async e => { e.preventDefault(); try { await api("/api/me", { method: "PUT", body: { name: $("#acc-name").value } }); say($("#acc-msg"), "Saved."); } catch (err) { say($("#acc-msg"), err.message, "bad"); } };
  $("#orgs").innerHTML = me.orgs.map(o => `<tr><td>${esc(o.name)}</td><td>${esc(o.role)}</td><td>${o.id === me.org?.id ? `<span class="pill ok">current</span>` : `<button class="btn small" data-switch="${o.id}">Switch</button>`}</td></tr>`).join("");
  $$("[data-switch]").forEach(b => b.onclick = async () => { const r = await api("/auth/switch-org", { body: { org_id: b.dataset.switch } }); location.href = "/app/settings"; });
  $("#neworg").onsubmit = async e => { e.preventDefault(); try { const r = await api("/api/orgs", { body: { name: e.target.name.value } }); location.href = r.next; } catch (err) { say($("#neworg .msg"), err.message, "bad"); } };
  if (!me.org) return;
  $$("[data-org-name]").forEach(el => el.textContent = me.org.name);
  // team
  const team = async () => {
    const t = await api("/api/org/members");
    $("#members").innerHTML = t.members.map(m => `<tr><td>${esc(m.name)}${m.id === t.me ? " (you)" : ""}</td><td>${esc(m.email)}</td>
      <td>${admin && m.id !== t.me ? `<select data-role="${m.id}">${["member", "admin", "owner"].map(r => `<option ${r === m.role ? "selected" : ""}>${r}</option>`).join("")}</select>` : esc(m.role)}</td>
      <td>${(admin && m.id !== t.me) || m.id === t.me ? `<button class="btn small danger" data-remove="${m.id}">${m.id === t.me ? "Leave" : "Remove"}</button>` : ""}</td></tr>`).join("");
    $("#invites").innerHTML = t.invites.length ? t.invites.map(i => `<tr><td>${esc(i.email)}</td><td>${esc(i.role)}</td><td>expires ${day(i.expires_at)}</td><td>${admin ? `<button class="btn small" data-revoke="${esc(i.email)}">Revoke</button>` : ""}</td></tr>`).join("") : `<tr><td colspan="4" class="muted">No pending invitations.</td></tr>`;
    $("#team-limit").textContent = `${t.members.length + t.invites.length} of ${t.limit} seats used`;
    $$("[data-role]").forEach(s => s.onchange = async () => { try { await api(`/api/org/members/${s.dataset.role}`, { method: "PUT", body: { role: s.value } }); say($("#team-msg"), "Role changed."); } catch (err) { say($("#team-msg"), err.message, "bad"); team(); } });
    $$("[data-remove]").forEach(b => b.onclick = async () => { if (!confirm("Remove this person from the organisation?")) return; try { const r = await api(`/api/org/members/${b.dataset.remove}`, { method: "DELETE" }); if (r.next) location.href = r.next; else team(); } catch (err) { say($("#team-msg"), err.message, "bad"); } });
    $$("[data-revoke]").forEach(b => b.onclick = async () => { await api("/api/org/invites/revoke", { body: { email: b.dataset.revoke } }); team(); });
  };
  team();
  if (!admin) $("#invite-form").hidden = true;
  $("#invite-form").onsubmit = async e => { e.preventDefault(); try { const r = await api("/api/org/invites", { body: { email: e.target.email.value, role: e.target.role.value } }); say($("#team-msg"), r.note); e.target.reset(); team(); } catch (err) { say($("#team-msg"), err.message, "bad"); } };
  // billing
  const bill = async () => {
    const b = await api("/api/billing"), u = b.usage, L = u.limits;
    const meter = (used, cap) => { const p = Math.min(100, Math.round(100 * used / Math.max(1, cap))); return `<div class="meter ${p >= 80 ? "hot" : ""}"><i style="width:${p}%"></i></div><span class="small muted">${num(used)} of ${num(cap)} (${p}%)</span>`; };
    const st = { active: ["paid", "ok"], cancelling: ["ends at period end", "warn"], past_due: ["payment overdue", "bad"], granted: ["granted", "ok"] }[b.status] || null;
    $("#bill-plan").innerHTML = `<h3>${esc(L.name)} plan ${st ? `<span class="pill ${st[1]}">${st[0]}</span>` : b.why === "trial" ? `<span class="pill">trial until ${day(b.trial_ends_at)}</span>` : ""}</h3>
      <dl class="kv"><dt>Traces this month</dt><dd>${meter(u.traces, L.traces_per_month)}</dd><dt>LLM calls this month</dt><dd>${meter(u.llm_calls, L.llm_calls_per_month)}</dd>
      <dt>Projects</dt><dd>up to ${L.projects}</dd><dt>Data kept for</dt><dd>${L.retention_days} days</dd>${b.period_end ? `<dt>Current period ends</dt><dd>${day(b.period_end)}</dd>` : ""}</dl>`;
    const acts = $("#bill-actions"), team = b.plans.team, paid = !!b.live;   // a subscription the provider can still charge
    const granted = !paid && (b.why === "granted" || L.plan === "enterprise");
    let h = "";
    if (!b.can_manage) h = `<p class="muted">Only an admin or owner can change the plan.</p>`;
    else if (granted) h = `<p>Your ${esc(L.name)} plan was set up with our team, so there is nothing to pay here. To change it, <a href="/contact">contact us</a>.</p>`;
    else if (paid) h = `${b.status === "past_due" ? `<p class="small"><span class="pill bad">payment failed</span> Update your card to keep the Team plan.</p>` : ""}<div class="btn-row"><button class="btn" id="bill-manage">Update card / invoices</button>${b.status !== "cancelling" ? `<button class="btn danger" id="bill-cancel">Cancel at period end</button>` : ""}</div>`;
    else {
      const prices = ["INR", "USD"].filter(c => b.available[c]);
      h = `<p>Team: ${prices.length ? prices.map(c => `<b>${c === "INR" ? "₹" + num(team.price.INR) + "/month + GST" : "$" + num(team.price.USD) + "/month"}</b>`).join(" or ") : "payments are not set up yet"}.</p>
        <div class="btn-row">${prices.map(c => `<button class="btn primary" data-buy="${c}">Upgrade, pay in ${c}</button>`).join("")}
        ${!b.trial_used && L.plan === "free" ? `<button class="btn" id="bill-trial">Start ${team.trial_days || 14}-day free trial</button>` : ""}</div>
        ${b.verified ? "" : `<p class="small muted">Confirm your email address before upgrading.</p>`}`;
    }
    acts.innerHTML = h;
    $("#bill-trial")?.addEventListener("click", async () => { try { await api("/api/org/trial", { method: "POST" }); bill(); } catch (err) { say($("#bill-msg"), err.message, "bad"); } });
    $("#bill-cancel")?.addEventListener("click", async () => { if (!confirm("Cancel the Team plan at the end of the paid period?")) return; try { const r = await api("/api/billing/cancel", { method: "POST" }); say($("#bill-msg"), r.note); bill(); } catch (err) { say($("#bill-msg"), err.message, "bad"); } });
    $("#bill-manage")?.addEventListener("click", async () => { try { const r = await api("/api/billing/manage"); if (r.url) location.href = r.url; else say($("#bill-msg"), "Nothing to manage yet.", "bad"); } catch (err) { say($("#bill-msg"), err.message, "bad"); } });
    $$("[data-buy]").forEach(btn => btn.onclick = () => buy(btn.dataset.buy));
  };
  const buy = async cur => {
    try {
      const r = await api("/api/billing/checkout", { body: { currency: cur } });
      if (r.provider === "stripe") { location.href = r.url; return; }
      await new Promise((ok, no) => { if (window.Razorpay) return ok(); const s = document.createElement("script"); s.src = "https://checkout.razorpay.com/v1/checkout.js"; s.onload = ok; s.onerror = () => no(new Error("Could not load Razorpay checkout.")); document.head.appendChild(s); });
      new window.Razorpay({ key: r.key_id, subscription_id: r.subscription_id, name: r.name, description: r.description, prefill: r.prefill, theme: { color: "#2A5DB0" },
        handler: async resp => { try { await api("/api/billing/razorpay/verify", { body: resp }); say($("#bill-msg"), "Payment received - the Team plan is active."); bill(); } catch (err) { say($("#bill-msg"), err.message, "bad"); } } }).open();
    } catch (err) { say($("#bill-msg"), err.message, "bad"); }
  };
  if (Q.get("billing") === "success") say($("#bill-msg"), "Payment received. The Team plan becomes active as soon as the payment provider confirms (usually seconds).");
  if (Q.get("billing") === "cancelled") say($("#bill-msg"), "Checkout cancelled - nothing was charged.", "bad");
  bill();
  // data
  if (!admin) $("#data-export").hidden = true;
  if (!owner) $("#danger").hidden = true;
  $("#danger").onsubmit = async e => { e.preventDefault(); if (!confirm("Delete the organisation and ALL its data? This cannot be undone.")) return; try { const r = await api("/api/org", { method: "DELETE", body: { confirm: e.target.confirm.value } }); location.href = r.next; } catch (err) { say($("#del-msg"), err.message, "bad"); } };
}
if ($("#settings")) settings();

/* ------------------------------------------------------------------ admin */
async function adminPage() {
  const load = async () => {
    const d = await api("/api/admin/orgs");
    $("#adm-totals").innerHTML = `<span class="pill">${d.totals.users} users</span> <span class="pill">${d.totals.orgs} organisations</span> <span class="pill ok">${d.totals.paying} paying</span> <span class="pill warn">${d.totals.trials} on trial</span> <span class="pill">MRR ₹${num(d.mrr.INR)} + $${num(d.mrr.USD)}</span>`;
    $("#adm-rows").innerHTML = d.orgs.map(o => `<tr><td><b>${esc(o.name)}</b><br><span class="small muted">${esc(o.id)} · ${esc(o.owner || "")}</span></td>
      <td>${esc(o.plan)}<br><span class="small muted">${esc(o.why)}${o.provider ? " · " + esc(o.provider) : ""}</span></td><td>${num(o.traces)}<br><span class="small muted">${num(o.llm_calls)} LLM</span></td>
      <td>${o.members}</td><td>${day(o.created_at)}</td>
      <td><select data-plan="${o.id}"><option value="">plan…</option><option>free</option><option>team</option><option>enterprise</option></select>
      <button class="btn small" data-trial="${o.id}">+14 d trial</button> <button class="btn small ${o.suspended ? "" : "danger"}" data-susp="${o.id}" data-v="${o.suspended ? 0 : 1}">${o.suspended ? "Unsuspend" : "Suspend"}</button></td></tr>`).join("");
    $$("[data-plan]").forEach(s => s.onchange = async () => { if (s.value) { await api(`/api/admin/orgs/${s.dataset.plan}`, { body: { plan: s.value } }); load(); } });
    $$("[data-trial]").forEach(b => b.onclick = async () => { await api(`/api/admin/orgs/${b.dataset.trial}`, { body: { trial_days: 14 } }); load(); });
    $$("[data-susp]").forEach(b => b.onclick = async () => { if (!confirm("Change suspension?")) return; await api(`/api/admin/orgs/${b.dataset.susp}`, { body: { suspended: b.dataset.v === "1" } }); load(); });
  };
  load().catch(err => say($("#adm-msg"), err.message, "bad"));
}
if ($("#admin")) adminPage();
