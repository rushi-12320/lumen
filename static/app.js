/* Lumen console - plain JavaScript, no build step. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (v, d = 1) => v === null || v === undefined || Number.isNaN(v) ? "–" : Number(v).toFixed(d);
const pct = (v, d = 1) => v === null || v === undefined ? "–" : `${Number(v).toFixed(d)}%`;
const usd = v => v === null || v === undefined ? "–" : v < 0.01 ? `$${Number(v).toFixed(5)}` : `$${Number(v).toFixed(2)}`;
const ms = v => v === null || v === undefined ? "–" : v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${Math.round(v)} ms`;
const short = h => h ? `${h.slice(0, 10)}…${h.slice(-6)}` : "–";
const when = ts => ts ? ts.replace("T", " ").replace("+00:00", "").slice(0, 19) : "–";
const sleep = t => new Promise(r => setTimeout(r, t));
const store = { get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } }, set(k, v) { try { localStorage.setItem(k, v); } catch { /* */ } } };

async function api(path, opts = {}) {
  const init = { method: opts.method || (opts.body ? "POST" : "GET"), headers: { "Content-Type": "application/json" } };
  if (opts.body) init.body = JSON.stringify(opts.body);
  const r = await fetch(path, init);
  if (!r.ok) {
    let m = r.statusText;
    try { const j = await r.json(); m = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch { /* */ }
    throw new Error(m);
  }
  return r.json();
}
function toast(msg, kind = "") { const t = $("#toast"); t.textContent = msg; t.className = `toast on ${kind}`; clearTimeout(toast._t); toast._t = setTimeout(() => t.className = "toast", 4500); }
const me = () => ($("#operator").value || "").trim();
function needName() { const n = me(); if (!n) { toast("Type your name in 'Signed in as' first - every decision is signed.", "bad"); $("#operator").focus(); } return n || null; }
const P = () => store.get("project", "PRJ-SUPPORT");

const badge = (t, c) => `<span class="badge ${c}">${esc(t)}</span>`;
const SC = { pending_approval: "b-warn", critic_blocked: "b-bad", applied: "b-ok", approved: "b-ok", rejected: "b-mute", critic_review: "b-info",
  open: "b-warn", acknowledged: "b-mute", completed: "b-ok", aborted: "b-bad", running: "b-info", match: "b-ok", diverged: "b-bad",
  allow: "b-ok", block: "b-bad", needs_approval: "b-warn", pending: "b-warn", executed: "b-ok", failed: "b-bad", ok: "b-ok", error: "b-bad",
  high: "b-bad", medium: "b-warn", low: "b-info" };
const st = s => badge(String(s ?? "").replace(/_/g, " "), SC[s] || "b-mute");
let META = null, OV = null;

/* ------------------------------------------------------------------ nav + status */
const NAV = [
  ["Observe", [["", "Overview", "◎"], ["traces", "Traces", "≡"], ["monitors", "Monitors & Incidents", "⚑", "inc"]]],
  ["Improve", [["evals", "Evaluations", "⊿"], ["prompts", "Prompt Registry", "❏"]]],
  ["Agents", [["agents", "Ops Agent", "⟳"], ["approvals", "Approvals", "✓", "appr"]]],
  ["Govern", [["guardrails", "Guardrails", "⛨"], ["ledger", "Audit Ledger", "⛓"], ["settings", "Settings & Keys", "⚙"]]],
];
function renderNav() {
  const route = location.hash.replace(/^#\/?/, "").split("?")[0];
  const cnt = OV ? { inc: OV.open_incidents, appr: OV.pending_proposals + OV.pending_llm } : {};
  $("#nav").innerHTML = NAV.map(([g, items]) => `<div class="grp">${g}</div>` + items.map(([r, l, i, k]) =>
    `<a href="#/${r}" class="${route === r ? "on" : ""}"><span class="ico">${i}</span>${l}${k && cnt[k] ? `<span class="badge ${k === "inc" ? "b-bad" : "b-warn"}">${cnt[k]}</span>` : ""}</a>`).join("")).join("");
}
async function refreshStatus() {
  try { OV = await api(`/api/overview?project=${P()}&hours=24`); } catch (e) { $("#status-strip").innerHTML = `<span class="chip bad"><span class="dot"></span>${esc(e.message)}</span>`; return; }
  const s = OV.summary, chip = (c, t) => `<span class="chip ${c}"><span class="dot"></span>${t}</span>`;
  $("#status-strip").innerHTML = [
    chip(OV.last_hour.error_rate > 5 ? "bad" : "ok", `errors ${pct(OV.last_hour.error_rate)} (1h)`),
    chip(OV.last_hour.p95_latency_ms > 6000 ? "warn" : "ok", `p95 ${ms(OV.last_hour.p95_latency_ms)} (1h)`),
    chip(OV.agents_paused ? "bad" : "ok", OV.agents_paused ? "agents PAUSED" : "agents active"),
    chip(OV.ledger.ok ? "ok" : "bad", OV.ledger.ok ? `ledger verified · ${OV.ledger.count}` : "ledger BROKEN"),
    chip(OV.llm.enabled ? "violet" : "", OV.llm.enabled ? `local LLM ${esc(OV.llm.model)} · approval required` : "local LLM off"),
    OV.pending_llm ? chip("warn", `${OV.pending_llm} LLM job(s) awaiting permission`) : "",
  ].join("");
  $("#side-foot").innerHTML = `Merkle root<br><code>${esc(short(OV.ledger.merkle_root))}</code><br><span class="small">No cloud LLM · no API keys used</span>`;
  renderNav();
}
async function loadProjects() {
  const ps = await api("/api/projects");
  if (!ps.find(p => p.id === P())) store.set("project", ps[0] ? ps[0].id : "");
  $("#project").innerHTML = ps.map(p => `<option value="${esc(p.id)}" ${p.id === P() ? "selected" : ""}>${esc(p.name)} · ${p.traces}</option>`).join("");
}
function startEvents() {
  const show = e => {
    const t = { agent_step: `<b>${esc(e.agent)}</b> ${esc(e.title)}`, run: `<b>orchestrator</b> ${esc(e.run_id)} ${esc(e.status)}`,
      incident: `<b>incident</b> [${esc(e.severity)}] ${esc(e.title)}`, proposal: `<b>proposal</b> ${esc(e.title)}`,
      decision: `<b>human</b> ${esc(e.by)} → ${esc(e.decision)} ${esc(e.id)}`, traces: `<b>ingest</b> ${esc(e.count)} traces`,
      llm_request: `<b>llm gate</b> ${esc(e.id)} ${esc(e.status || "awaiting permission")}`, prompt: `<b>prompt</b> ${esc(e.name)} v${esc(e.version)} ${esc(e.action)} by ${esc(e.by)}`,
      experiment: `<b>eval</b> ${esc(e.name)}`, killswitch: `<b>kill switch</b> ${e.paused ? "paused" : "resumed"} by ${esc(e.by)}` }[e.kind] || esc(e.kind);
    $("#ticker").innerHTML = `${esc(when(e.ts).slice(11))} · ${t}`;
  };
  try {
    const es = new EventSource("/api/events/stream");
    es.onmessage = m => { const e = JSON.parse(m.data); show(e); if (["run", "decision", "llm_request", "prompt", "killswitch"].includes(e.kind)) refreshStatus(); };
  } catch { /* */ }
}

/* ------------------------------------------------------------------ drawer & charts */
function openDrawer(html) {
  $("#drawer").innerHTML = `<div class="row" style="justify-content:flex-end"><button class="icon-btn" id="dclose" aria-label="Close">✕</button></div>${html}`;
  $("#drawer").classList.add("on"); $("#drawer-bg").classList.add("on"); $("#dclose").onclick = closeDrawer;
}
function closeDrawer() { $("#drawer").classList.remove("on"); $("#drawer-bg").classList.remove("on"); }

function lineChart(points, series, { h = 200, fmtY = v => fmt(v, 0) } = {}) {
  const W = 760, H = h, L = 52, R = 12, T = 10, B = 24;
  const ys = series.flatMap(s => points.map(p => p[s.key])).filter(v => v !== null && v !== undefined);
  if (!points.length || !ys.length) return `<div class="empty">No data</div>`;
  const ymax = Math.max(...ys, ...series.filter(s => s.ref !== undefined).map(s => s.ref)) * 1.1 || 1;
  const x = i => L + (i * (W - L - R)) / Math.max(1, points.length - 1), y = v => T + (H - T - B) * (1 - v / ymax);
  let s = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img">`;
  for (let k = 0; k <= 4; k++) { const v = (k * ymax) / 4; s += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="#262e38"/><text x="${L - 6}" y="${y(v) + 3}" text-anchor="end">${esc(fmtY(v))}</text>`; }
  for (const se of series) {
    if (se.ref !== undefined) { s += `<line x1="${L}" x2="${W - R}" y1="${y(se.ref)}" y2="${y(se.ref)}" stroke="${se.color}" stroke-dasharray="5 4"/>`; continue; }
    if (se.bars) {
      const bw = Math.max(2, (W - L - R) / points.length - 3);
      points.forEach((p, i) => { const v = p[se.key]; if (v) s += `<rect x="${x(i) - bw / 2}" y="${y(v)}" width="${bw}" height="${H - B - y(v)}" fill="${se.color}" opacity=".7"><title>${se.name}: ${v}</title></rect>`; });
      continue;
    }
    const pts = points.map((p, i) => p[se.key] === null || p[se.key] === undefined ? null : `${x(i)},${y(p[se.key])}`).filter(Boolean);
    s += `<polyline fill="none" stroke="${se.color}" stroke-width="2" points="${pts.join(" ")}"/>`;
  }
  [0, Math.floor(points.length / 2), points.length - 1].forEach(i => s += `<text x="${x(i)}" y="${H - 6}" text-anchor="middle">${esc(when(points[i].ts).slice(11, 16))}</text>`);
  return s + `</svg><div class="legend">${series.map(se => `<span><i style="background:${se.color}"></i>${esc(se.name)}</span>`).join("")}</div>`;
}

function md(text) {
  const lines = esc(text).split("\n"); let html = "", list = false;
  for (const ln of lines) {
    if (ln.startsWith("- ")) { if (!list) { html += "<ul>"; list = true; } html += `<li>${ln.slice(2)}</li>`; continue; }
    if (list) { html += "</ul>"; list = false; }
    if (ln.startsWith("## ")) html += `<h2>${ln.slice(3)}</h2>`; else if (ln.startsWith("# ")) html += `<h1>${ln.slice(2)}</h1>`; else if (ln.trim()) html += `<p>${ln}</p>`;
  }
  return `<div class="md">${html}${list ? "</ul>" : ""}</div>`;
}

const V = {};
const view = () => $("#view");
const errCard = e => `<div class="card"><h2 class="bad-t">Could not load</h2><p class="muted">${esc(e.message || e)}</p></div>`;
const kpi = (l, v, n, cls = "") => `<div class="card kpi"><div class="lbl">${l}</div><div class="val ${cls}">${v}</div><div class="note">${n}</div></div>`;
const tl = id => `<a href="#" data-trace="${esc(id)}" class="mono">${esc(id.slice(0, 10))}</a>`;

/* ------------------------------------------------------------------ Overview */
V[""] = async () => {
  const o = OV || await api(`/api/overview?project=${P()}&hours=24`);
  const s = o.summary;
  const brk = (rows, label) => `<div class="tbl-wrap"><table><thead><tr><th>${label}</th><th class="num">Requests</th><th class="num">Errors</th><th class="num">p95</th><th class="num">Cost</th><th class="num">Eval pass</th><th class="num">Grounded</th></tr></thead><tbody>
    ${rows.map(r => `<tr><td class="mono">${esc(r.value)}</td><td class="num">${r.requests}</td><td class="num">${pct(r.error_rate)}</td><td class="num">${ms(r.p95_latency_ms)}</td><td class="num">${usd(r.cost_total)}</td><td class="num">${pct(r.eval_pass_rate)}</td><td class="num">${fmt(r.groundedness_avg, 2)}</td></tr>`).join("")}</tbody></table></div>`;
  view().innerHTML = `<h1>Overview</h1><p class="sub">Last 24 h · ${esc(when(o.window.until))} UTC · production prompts ${Object.entries(o.production_prompts).map(([n, v]) => `<span class="mono">${esc(n)} v${v}</span>`).join(", ") || "none"}</p>
  <div class="grid g6">${kpi("Requests", s.requests, `${o.last_hour.requests} in the last hour`)}${kpi("Error rate", pct(s.error_rate, 2), `last hour ${pct(o.last_hour.error_rate, 2)}`, s.error_rate > 5 ? "bad-t" : "ok-t")}
    ${kpi("p95 latency", ms(s.p95_latency_ms), `last hour ${ms(o.last_hour.p95_latency_ms)}`, o.last_hour.p95_latency_ms > 6000 ? "warn-t" : "")}
    ${kpi("Cost", usd(s.cost_total), `${usd(s.cost_per_request)} / request`)}${kpi("Eval pass rate", pct(s.eval_pass_rate), `groundedness ${fmt(s.groundedness_avg, 2)}`, s.eval_pass_rate < 90 ? "warn-t" : "ok-t")}
    ${kpi("Security", `${s.pii_leaks} / ${s.injection_attempts}`, "PII leaks / injection attempts", s.pii_leaks ? "bad-t" : "")}</div>
  <div class="grid g2 mt"><div class="card"><h2>Latency p95</h2>${lineChart(o.series, [{ key: "p95_latency_ms", name: "p95 latency (ms)", color: "#8b9dff" }, { ref: 6000, name: "alert 6000 ms", color: "#ef5b5b" }])}</div>
    <div class="card"><h2>Quality</h2>${lineChart(o.series.map(p => ({ ...p, g: p.groundedness_avg === null ? null : p.groundedness_avg * 100 })), [{ key: "eval_pass_rate", name: "eval pass rate %", color: "#2fbf9b" }, { key: "g", name: "groundedness × 100", color: "#e8b339" }, { ref: 75, name: "groundedness alert", color: "#ef5b5b" }])}</div>
    <div class="card"><h2>Traffic & errors</h2>${lineChart(o.series.map(p => ({ ...p, err: p.requests ? Math.round(p.requests * (p.error_rate || 0) / 100) : 0 })), [{ key: "requests", name: "requests / hour", color: "#5aa9e6", bars: true }, { key: "err", name: "errors", color: "#ef5b5b" }])}</div>
    <div class="card"><h2>Cost</h2>${lineChart(o.series, [{ key: "cost_total", name: "USD / hour", color: "#a78bfa" }], { fmtY: v => `$${v.toFixed(3)}` })}</div></div>
  <div class="grid g2 mt"><div class="card"><h2>By route</h2>${brk(o.by_route, "Route")}</div><div class="card"><h2>By model</h2>${brk(o.by_model, "Model")}</div>
    <div class="card"><h2>By prompt version</h2>${brk(o.by_prompt, "Version")}</div>
    <div class="card"><h2>Online evaluators</h2><table><tbody>${Object.entries(o.evals).map(([k, v]) => `<tr><td>${esc(k)}<div class="small muted">${esc(META.evaluators[k] || "")}</div></td><td class="num">${v.n}</td><td class="num ${v.pass_rate < 90 ? "warn-t" : "ok-t"}">${pct(v.pass_rate)}</td></tr>`).join("")}</tbody></table></div></div>`;
};

/* ------------------------------------------------------------------ Traces */
V.traces = async (q) => {
  let offset = 0;
  view().innerHTML = `<h1>Traces</h1><p class="sub">Every request with spans, cost, latency and online evaluator results. PII is redacted before storage.</p>
  <div class="card"><div class="row"><select id="fr"><option value="">all routes</option>${OV.by_route.map(r => `<option>${esc(r.value)}</option>`).join("")}</select>
    <select id="fm"><option value="">all models</option>${OV.by_model.map(r => `<option>${esc(r.value)}</option>`).join("")}</select>
    <select id="fs"><option value="">any status</option><option>ok</option><option>error</option></select>
    <label class="small"><input type="checkbox" id="ff" ${q.get("failed") ? "checked" : ""}> failed evals</label><label class="small"><input type="checkbox" id="fi"> injection</label><label class="small"><input type="checkbox" id="fp"> PII leak</label>
    <input id="fq" placeholder="search text / id / user" style="flex:1;min-width:160px"><button class="btn" id="tick">+20 live traces</button></div>
    <div class="tbl-wrap mt"><table><thead><tr><th>Trace</th><th>Time</th><th>Route</th><th>Model</th><th>Prompt</th><th class="num">Latency</th><th class="num">Tokens</th><th class="num">Cost</th><th>Status</th><th>Failed evals</th><th>Input</th></tr></thead><tbody id="rows"></tbody></table></div>
    <div class="row mt"><span class="small muted" id="cnt"></span><span style="flex:1"></span><button class="btn sm" id="more">Load more</button></div></div>`;
  const load = async (append) => {
    const p = new URLSearchParams({ project: P(), limit: 50, offset });
    [["name", "#fr"], ["model", "#fm"], ["status", "#fs"]].forEach(([k, s]) => $(s).value && p.set(k, $(s).value));
    if ($("#ff").checked) p.set("failed", "true"); if ($("#fi").checked) p.set("injection", "true"); if ($("#fp").checked) p.set("pii", "true");
    if ($("#fq").value.trim()) p.set("q", $("#fq").value.trim());
    const d = await api(`/api/traces?${p}`);
    const h = d.items.map(t => `<tr class="click" data-trace="${esc(t.id)}"><td class="mono">${esc(t.id.slice(0, 10))}</td><td class="small">${esc(when(t.ts).slice(5))}</td><td>${esc(t.name)}</td><td class="small">${esc(t.model)}</td>
      <td class="small">${t.prompt_version ? `v${esc(t.prompt_version)}` : "–"}</td><td class="num">${ms(t.latency_ms)}</td><td class="num">${t.tokens_in}/${t.tokens_out}</td><td class="num">${usd(t.cost)}</td><td>${st(t.status)}</td>
      <td class="small">${t.eval_failed.map(e => badge(e, "b-bad")).join(" ")}${t.injection ? badge("injection", "b-vio") : ""}${t.redactions ? badge(`${t.redactions} PII redacted`, "b-info") : ""}</td><td class="small muted" style="max-width:260px">${esc(t.input)}</td></tr>`).join("");
    $("#rows").innerHTML = append ? $("#rows").innerHTML + h : (h || `<tr><td colspan="11" class="empty">No traces</td></tr>`);
    $("#cnt").textContent = `${Math.min(offset + d.items.length, d.total)} of ${d.total}`; $("#more").disabled = offset + d.items.length >= d.total;
  };
  const reset = () => { offset = 0; load(); };
  $$("#fr,#fm,#fs,#ff,#fi,#fp").forEach(e => e.onchange = reset);
  $("#fq").oninput = () => { clearTimeout(V._q); V._q = setTimeout(reset, 300); };
  $("#more").onclick = () => { offset += 50; load(true); };
  $("#tick").onclick = async () => { const r = await api("/api/sim/tick", { body: { project: P(), n: 20 } }); toast(`+${r.added} traces (current production prompts)`, "ok"); reset(); refreshStatus(); };
  await load();
};

async function traceDrawer(id) {
  try {
    const d = await api(`/api/traces/${encodeURIComponent(id)}`), t = d.trace;
    const total = Math.max(t.latency_ms, ...d.spans.map(s => (s.start_ms || 0) + (s.latency_ms || 0)), 1);
    const dss = await api(`/api/datasets?project=${P()}`);
    openDrawer(`<h1 class="mono">${esc(t.name)} · ${esc(t.id.slice(0, 12))}</h1><p class="sub">${esc(when(t.ts))} UTC · ${esc(t.model)} · prompt ${esc(t.prompt_name || "–")} v${esc(t.prompt_version || "–")} · user ${esc(t.user_id || "–")} · ${st(t.status)}</p>
      <div class="grid g4">${kpi("Latency", ms(t.latency_ms), "")}${kpi("Tokens", `${t.tokens_in}/${t.tokens_out}`, "in / out")}${kpi("Cost", usd(t.cost), "")}${kpi("PII redacted", t.redactions, t.injection ? "injection attempt!" : "")}</div>
      <h3 class="mt">Spans</h3>${d.spans.map(s => `<div class="small"><b>${esc(s.type)}</b> ${esc(s.name)} ${s.model ? `· ${esc(s.model)}` : ""} · ${ms(s.latency_ms)} ${s.error ? `<span class="bad-t">${esc(s.error)}</span>` : ""}</div>
        <div class="wf"><span class="wf-${esc(s.type)} ${s.status === "error" ? "wf-error" : ""}" style="left:${(100 * (s.start_ms || 0)) / total}%;width:${Math.max(1, (100 * (s.latency_ms || 0)) / total)}%"></span></div>`).join("")}
      <h3 class="mt">Evaluators</h3>${d.evals.map(e => `<div class="find ${e.passed ? "pass" : "fail"}">${badge(e.passed ? "pass" : "fail", e.passed ? "b-ok" : "b-bad")} <b>${esc(e.evaluator)}</b> ${fmt(e.score, 2)} <span class="small muted">${esc(e.detail)}</span></div>`).join("")}
      <h3 class="mt">Input</h3><div class="txt">${esc(t.input)}</div><h3>Output</h3><div class="txt">${esc(t.output)}</div>
      ${d.spans.filter(s => s.type === "retrieval").map(s => `<h3>Retrieved context</h3><div class="txt">${esc(s.output)}</div>`).join("")}
      <div class="row mt"><select id="dsel">${dss.map(x => `<option value="${esc(x.id)}">${esc(x.name)}</option>`).join("")}</select><button class="btn" id="todata">Add to dataset</button></div>`);
    $("#todata").onclick = async () => { const by = needName(); if (!by) return; try { const r = await api(`/api/datasets/${$("#dsel").value}/items`, { body: { trace_ids: [t.id], by } }); toast(r.ok ? "Added to dataset (signed in ledger)" : r.reasons.join("; "), r.ok ? "ok" : "bad"); } catch (e) { toast(e.message, "bad"); } };
  } catch (e) { toast(e.message, "bad"); }
}

/* ------------------------------------------------------------------ Monitors */
V.monitors = async () => {
  const [rules, incs] = await Promise.all([api(`/api/rules?project=${P()}`), api(`/api/incidents?project=${P()}`)]);
  const ops = { gt: ">", lt: "<", gte: "≥", lte: "≤" };
  view().innerHTML = `<h1>Monitors & Incidents</h1><p class="sub">Alert rules are evaluated by the Monitor agent against a baseline. Breaches become incidents; the Investigator explains them.</p>
  <div class="card"><h2>Alert rules</h2><div class="tbl-wrap"><table><thead><tr><th>Rule</th><th>Condition</th><th>Window</th><th>Filter</th><th>Severity</th><th>Enabled</th></tr></thead><tbody>
    ${rules.map(r => `<tr><td>${esc(r.name)}</td><td class="mono">${esc(META.metrics[r.metric])} ${ops[r.op]} ${r.threshold}</td><td>${r.window_min} min</td><td class="small">${esc(JSON.stringify(r.filter))}</td><td>${st(r.severity)}</td>
      <td><input type="checkbox" data-rule="${esc(r.id)}" ${r.enabled ? "checked" : ""} aria-label="enable ${esc(r.name)}"></td></tr>`).join("")}</tbody></table></div>
    <h3 class="mt">New rule</h3><div class="row"><input id="rn" placeholder="name"><select id="rm">${Object.entries(META.metrics).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}</select>
      <select id="ro"><option value="gt">&gt;</option><option value="lt">&lt;</option></select><input id="rt" type="number" step="any" placeholder="threshold" style="width:110px">
      <input id="rw" type="number" value="60" style="width:80px" aria-label="window minutes"> min <input id="rf" placeholder="route filter (optional)" style="width:170px">
      <select id="rs"><option>medium</option><option>high</option><option>low</option></select><button class="btn primary" id="radd">Add rule</button></div></div>
  <div class="card mt"><h2>Incidents</h2><div class="tbl-wrap"><table><thead><tr><th>Severity</th><th>Incident</th><th>Raised</th><th class="num">Evidence</th><th>Status</th><th></th></tr></thead><tbody>
    ${incs.map(i => `<tr class="click" data-inc="${esc(i.id)}"><td>${st(i.severity)}</td><td>${esc(i.title)}<div class="small muted">${esc(i.id)} · run ${esc(i.run_id)}</div></td><td class="small">${esc(when(i.created_at))}</td><td class="num">${i.evidence.length}</td><td>${st(i.status)}</td>
      <td>${i.status === "open" ? `<button class="btn sm" data-ack="${esc(i.id)}">Acknowledge</button>` : ""}</td></tr>`).join("") || `<tr><td colspan="6" class="empty">No incidents</td></tr>`}</tbody></table></div></div>`;
  $$("[data-rule]").forEach(c => c.onchange = async () => { const by = needName(); if (!by) { c.checked = !c.checked; return; } await api(`/api/rules/${c.dataset.rule}/toggle`, { body: { enabled: c.checked, by } }); toast("Rule updated", "ok"); });
  $("#radd").onclick = async () => {
    const by = needName(); if (!by) return;
    try { await api("/api/rules", { body: { project: P(), name: $("#rn").value, metric: $("#rm").value, op: $("#ro").value, threshold: Number($("#rt").value), window_min: Number($("#rw").value), filter: $("#rf").value ? { name: $("#rf").value } : {}, severity: $("#rs").value, by } }); toast("Rule added", "ok"); V.monitors(); } catch (e) { toast(e.message, "bad"); }
  };
  $$("[data-ack]").forEach(b => b.onclick = async ev => { ev.stopPropagation(); const by = needName(); if (!by) return; await api(`/api/incidents/${b.dataset.ack}/ack`, { body: { by } }); refreshStatus(); V.monitors(); });
  $$("[data-inc]").forEach(r => r.onclick = async () => {
    const i = incs.find(x => x.id === r.dataset.inc);
    const run = await api(`/api/runs/${i.run_id}`).catch(() => null);
    const f = run && run.summary && run.summary.findings ? run.summary.findings.find(x => i.title.startsWith(x.rule)) : null;
    openDrawer(`<h1>${esc(i.title)}</h1><p class="sub">${st(i.severity)} · ${esc(i.id)} · ${esc(when(i.created_at))}</p>
      <div class="grid g3">${kpi("Window value", fmt(i.detail.value, 3), `${i.detail.n_window} traces · ${i.detail.window_min} min`)}${kpi("Baseline", fmt(i.detail.baseline, 3), `${i.detail.n_baseline} traces`)}${kpi("Threshold", `${ops[i.detail.op]} ${i.detail.threshold}`, esc(META.metrics[i.detail.metric]))}</div>
      <h3 class="mt">Investigator - root-cause hypotheses</h3>${f && f.hypotheses.length ? `<table><thead><tr><th>Segment</th><th class="num">Value</th><th class="num">Others</th><th class="num">Share</th><th class="num">Confidence</th></tr></thead><tbody>
        ${f.hypotheses.map(h => `<tr><td class="mono">${esc(h.dimension)} = ${esc(h.value)} ${h.new_in_window ? badge("new", "b-vio") : ""}</td><td class="num">${fmt(h.window_value, 3)}</td><td class="num">${fmt(h.others_value, 3)}<div class="small muted">${esc(h.compared_with)}</div></td><td class="num">${pct(h.share * 100, 0)}</td><td class="num">${h.confidence}</td></tr>`).join("")}</tbody></table>
        ${f.first_seen ? `<p class="small">First seen ${esc(when(f.first_seen))} UTC.</p>` : ""}` : '<p class="muted small">No single segment explains it.</p>'}
      <h3 class="mt">Evidence traces</h3><div class="small">${i.evidence.map(tl).join(", ")}</div>`);
  });
};

/* ------------------------------------------------------------------ Ops agent */
const AG = ["planner", "monitor", "investigator", "curator", "remediator", "critic", "reporter", "auditor"];
const pipe = (done = [], on = null) => `<div class="pipeline">${AG.map((a, i) => `${i ? '<span class="arrow">→</span>' : ""}<span class="pipe ${done.includes(a) ? "done" : ""} ${on === a ? "on" : ""}">${a}</span>`).join("")}</div>`;
const steps = ss => `<div class="steps">${ss.map(s => `<div class="step ${esc(s.status)}"><div class="ag">${esc(s.agent)}</div><div>${st(s.status)}</div><div>${esc(s.title)}
  ${s.citations && s.citations.length ? `<div class="small muted">cites: ${s.citations.slice(0, 6).map(tl).join(", ")}${s.citations.length > 6 ? ` +${s.citations.length - 6}` : ""}</div>` : ""}
  <details><summary>details</summary><pre>${esc(JSON.stringify(s.detail, null, 2))}</pre></details></div></div>`).join("")}</div>`;
V.agents = async () => {
  view().innerHTML = `<h1>Ops Agent</h1><p class="sub">Eight deterministic agents watch production, explain regressions and propose fixes. They cannot change anything themselves: rollbacks, dataset edits and LLM calls wait for your approval. Each run is hashed and can be replayed.</p>
  <div class="card"><div class="row" style="justify-content:space-between">${pipe()}<button class="btn primary" id="go">Run ops agent</button></div><div id="live" class="mt"></div></div>
  <div class="card mt"><h2>Run history</h2><div class="tbl-wrap"><table><thead><tr><th>Run</th><th>Kind</th><th>Started</th><th>Status</th><th>Audit / replay</th><th>Input</th><th>Output</th><th></th></tr></thead><tbody id="runs"></tbody></table></div></div>`;
  const loadRuns = async () => {
    const rs = await api(`/api/runs?project=${P()}`);
    $("#runs").innerHTML = rs.map(r => `<tr><td class="mono">${esc(r.id)}${r.replay_of ? `<div class="small muted">of ${esc(r.replay_of)}</div>` : ""}</td><td>${esc(r.kind)}</td><td class="small">${esc(when(r.started_at))}</td><td>${st(r.status)}</td>
      <td>${r.audit ? badge(r.audit, r.audit === "AUDIT_PASSED" ? "b-ok" : "b-bad") : r.match !== null && r.match !== undefined ? badge(r.match ? "identical" : "diverged", r.match ? "b-ok" : "b-bad") : ""}</td><td class="hash">${esc(short(r.input_hash))}</td><td class="hash">${esc(short(r.output_hash))}</td>
      <td class="row" style="gap:6px;flex-wrap:nowrap"><button class="btn sm" data-view="${esc(r.id)}">Steps</button>${r.kind === "cycle" && r.output_hash ? `<button class="btn sm" data-replay="${esc(r.id)}">Replay</button>` : ""}</td></tr>`).join("") || `<tr><td colspan="8" class="empty">No runs</td></tr>`;
    $$("[data-view]").forEach(b => b.onclick = async () => {
      const r = await api(`/api/runs/${b.dataset.view}`); const rep = r.summary && r.summary.report;
      openDrawer(`<h1 class="mono">${esc(r.id)}</h1><p class="sub">${st(r.status)} · <span class="hash">in ${esc(r.input_hash)} · out ${esc(r.output_hash)}</span></p>
        ${rep ? `<div class="card tight mb">${md(rep.body)}${rep.ai_summary ? `<h3>Local-LLM summary ${rep.ai_summary.grounded ? badge("numbers verified", "b-ok") : badge("rejected: ungrounded numbers", "b-bad")}</h3><p>${esc(rep.ai_summary.text || (rep.ai_summary.ungrounded_numbers || []).join(", "))}</p>` : rep.llm_request ? `<p class="small muted">Local-LLM summary requested (${esc(rep.llm_request)}) - approve it on Approvals.</p>` : ""}</div>` : ""}
        ${r.kind === "replay" ? `<div class="find ${r.summary.match ? "pass" : "fail"}">${esc(r.summary.explanation)}</div>` : ""}${steps(r.steps || [])}`);
    });
    $$("[data-replay]").forEach(b => b.onclick = async () => {
      b.disabled = true; const r = await api(`/api/runs/${b.dataset.replay}/replay`, { method: "POST" });
      openDrawer(`<h1>Replay ${r.match ? '<span class="ok-t">identical</span>' : '<span class="bad-t">diverged</span>'}</h1><p class="sub">${esc(r.explanation)}</p>
        <table><tbody><tr><td>Input</td><td class="hash">${esc(r.original_input_hash)}<br>${esc(r.replay_input_hash)} ${r.input_match ? badge("same data", "b-ok") : badge("data changed", "b-warn")}</td></tr>
        <tr><td>Output</td><td class="hash">${esc(r.original_output_hash)}<br>${esc(r.replay_output_hash)} ${r.match ? badge("same decisions", "b-ok") : badge("different", "b-bad")}</td></tr></tbody></table>`);
      loadRuns();
    });
  };
  $("#go").onclick = async () => {
    const b = $("#go"); b.disabled = true; b.textContent = "Running…";
    try {
      const r = await api("/api/agents/run", { body: { project: P() } });
      $("#live").innerHTML = `<div id="pl"></div><div id="ls" class="mt"></div>`;
      const done = [];
      for (let k = 0; k < r.steps.length; k++) {
        const s = r.steps[k]; AG.slice(0, AG.indexOf(s.agent)).forEach(a => !done.includes(a) && done.push(a));
        $("#pl").innerHTML = pipe(done, s.agent); $("#ls").innerHTML = steps(r.steps.slice(0, k + 1)); await sleep(80);
      }
      $("#pl").innerHTML = pipe(AG);
      $("#ls").insertAdjacentHTML("afterbegin", `<div class="find ${r.status === "completed" ? "pass" : "fail"} mb"><b>${esc(r.run_id)}</b> ${st(r.status)} ${r.audit ? badge(r.audit.verdict, r.audit.verdict === "AUDIT_PASSED" ? "b-ok" : "b-bad") : ""} · <a href="#/approvals">review proposals →</a></div>`);
      refreshStatus(); loadRuns();
    } catch (e) { $("#live").innerHTML = errCard(e); }
    b.disabled = false; b.textContent = "Run ops agent";
  };
  await loadRuns();
};

/* ------------------------------------------------------------------ Evaluations */
V.evals = async (q) => {
  const [dss, exps, pls] = await Promise.all([api(`/api/datasets?project=${P()}`), api(`/api/experiments?project=${P()}`), api(`/api/prompts?project=${P()}`)]);
  const off = META.offline;
  view().innerHTML = `<h1>Evaluations</h1><p class="sub">Datasets, experiments and the release gate. Deterministic evaluators run instantly; the LLM judge runs on your local model only after you approve it.</p>
  <div class="grid g2"><div class="card"><div class="row" style="justify-content:space-between"><h2>Datasets</h2><span><input id="dn" placeholder="new dataset name" style="width:170px"> <button class="btn sm" id="dadd">Create</button></span></div>
    <table><tbody>${dss.map(d => `<tr class="click" data-ds="${esc(d.id)}"><td><b>${esc(d.name)}</b><div class="small muted">${esc(d.description || "")}</div></td><td class="num">${d.items} items</td></tr>`).join("") || '<tr><td class="empty">No datasets</td></tr>'}</tbody></table></div>
  <div class="card"><h2>New experiment</h2><div class="grid g2">
    <label class="fld">Dataset<select id="xd">${dss.map(d => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join("")}</select></label>
    <label class="fld">Prompt<select id="xp"><option value="">(none)</option>${pls.map(p => `<option>${esc(p.name)}</option>`).join("")}</select></label>
    <label class="fld">Prompt version<input id="xv" placeholder="e.g. 5"></label><label class="fld">Name<input id="xn" placeholder="experiment name"></label></div>
    <div class="small muted mt">Evaluators</div><div class="row">${off.map(e => `<label class="small"><input type="checkbox" data-ev="${e}" ${["not_empty", "groundedness", "answer_f1", "pii_leak", "refusal"].includes(e) ? "checked" : ""}> ${esc(e)}</label>`).join("")}</div>
    <label class="fld mt">Candidate outputs (JSONL: {"item_id" or "input", "output", "latency_ms"?, "cost"?})<textarea id="xo" placeholder='{"input": "How do I reset my password?", "output": "..."}'></textarea></label>
    <div class="row mt"><button class="btn primary" id="xup">Evaluate uploaded outputs</button><button class="btn" id="xloc">Generate with local LLM (asks permission)</button></div></div></div>
  <div class="card mt"><div class="row" style="justify-content:space-between"><h2>Experiments</h2><span class="row"><select id="ca">${exps.map(e => `<option value="${esc(e.id)}">${esc(e.name)}</option>`).join("")}</select> vs <select id="cb">${exps.map((e, i) => `<option value="${esc(e.id)}" ${i === 0 ? "selected" : ""}>${esc(e.name)}</option>`).join("")}</select><button class="btn" id="cmp">Compare</button></span></div>
    <div class="tbl-wrap"><table><thead><tr><th>Experiment</th><th>Dataset</th><th>Prompt</th><th class="num">Items</th><th class="num">Pass rate</th><th class="num">Cost</th><th>Release gate</th><th>LLM judge</th><th></th></tr></thead><tbody>
    ${exps.map(e => { const s = e.summary || {}; return `<tr><td>${esc(e.name)}<div class="small muted">${esc(e.id)} · ${esc(e.source)} · ${esc(when(e.created_at))}</div></td><td>${esc(e.dataset_name)}</td><td class="small">${esc(e.prompt_name || "–")} v${esc(e.prompt_version || "–")}</td><td class="num">${s.items ?? "–"}</td>
      <td class="num">${s.pass_rate !== undefined && s.pass_rate !== null ? pct(s.pass_rate * 100) : "–"}</td><td class="num">${usd(s.cost_total)}</td><td>${s.gate ? badge(s.gate.passed ? "PASSED" : "FAILED", s.gate.passed ? "b-ok" : "b-bad") : '<span class="muted small">not run</span>'}</td>
      <td class="small">${s.llm_judge ? esc(s.llm_judge.status) : "–"}</td><td class="row" style="gap:6px;flex-wrap:nowrap"><button class="btn sm" data-exp="${esc(e.id)}">Open</button><button class="btn sm" data-gate="${esc(e.id)}">Run gate</button><button class="btn sm" data-judge="${esc(e.id)}">LLM judge</button></td></tr>`; }).join("") || '<tr><td colspan="9" class="empty">No experiments</td></tr>'}</tbody></table></div></div>
  <div id="cmpres"></div>`;
  if (exps.length > 1) $("#ca").value = exps[1].id;
  const evs = () => $$("[data-ev]").filter(c => c.checked).map(c => c.dataset.ev);
  $("#dadd").onclick = async () => { const by = needName(); if (!by) return; await api("/api/datasets", { body: { project: P(), name: $("#dn").value, by } }).then(() => V.evals(q)).catch(e => toast(e.message, "bad")); };
  $("#xup").onclick = async () => {
    const by = needName(); if (!by) return;
    try { const r = await api("/api/experiments", { body: { project: P(), dataset_id: $("#xd").value, name: $("#xn").value || "uploaded candidate", prompt_name: $("#xp").value || null, prompt_version: $("#xv").value || null, evaluators: evs(), outputs_text: $("#xo").value, by } });
      toast(`Experiment ${r.id}: pass rate ${pct(r.summary.pass_rate * 100)}${r.llm_request && r.llm_request.ok ? " · LLM judge awaiting your permission" : ""}`, "ok"); V.evals(q); } catch (e) { toast(e.message, "bad"); }
  };
  $("#xloc").onclick = async () => {
    const by = needName(); if (!by) return;
    if (!$("#xp").value || !$("#xv").value) return toast("Choose a prompt and version to generate with", "bad");
    try { const r = await api("/api/experiments/local-run", { body: { project: P(), dataset_id: $("#xd").value, prompt_name: $("#xp").value, prompt_version: Number($("#xv").value), evaluators: evs(), by } });
      toast(r.ok ? `LLM job ${r.id} created (${r.n_calls} calls, ~${r.est_tokens} tokens) - approve it on Approvals` : (r.reasons || []).join("; "), r.ok ? "ok" : "bad"); refreshStatus(); } catch (e) { toast(e.message, "bad"); }
  };
  $$("[data-gate]").forEach(b => b.onclick = async () => { const g = await api(`/api/experiments/${b.dataset.gate}/gate`, { body: { by: me() || "console" } }); gateDrawer(g); V.evals(q); });
  $$("[data-judge]").forEach(b => b.onclick = async () => { const by = needName(); if (!by) return; const r = await api(`/api/experiments/${b.dataset.judge}/judge`, { body: { by } }); toast(r.ok ? `LLM judge job ${r.id}: ${r.n_calls} calls awaiting your permission` : (r.reasons || []).join("; "), r.ok ? "ok" : "bad"); refreshStatus(); });
  $$("[data-exp]").forEach(b => b.onclick = () => expDrawer(b.dataset.exp));
  $$("[data-ds]").forEach(r => r.onclick = () => dsDrawer(r.dataset.ds));
  $("#cmp").onclick = async () => {
    try {
      const c = await api(`/api/compare?a=${$("#ca").value}&b=${$("#cb").value}`);
      $("#cmpres").innerHTML = `<div class="card mt"><h2>${esc(c.a.name)} → ${esc(c.b.name)}</h2><p class="small muted">${c.common_items} shared items · ${c.regression_count} regressions · ${c.improvement_count} improvements · pass rate ${pct(c.a.pass_rate * 100)} → ${pct(c.b.pass_rate * 100)} · cost ${usd(c.a.cost_total)} → ${usd(c.b.cost_total)}</p>
        <div class="tbl-wrap"><table><thead><tr><th>Evaluator</th><th class="num">A</th><th class="num">B</th><th class="num">Δ</th><th class="num">95% CI of Δ</th><th class="num">W / L / T</th><th class="num">sign test p</th><th></th></tr></thead><tbody>
        ${Object.entries(c.evaluators).map(([k, e]) => `<tr><td>${esc(k)}</td><td class="num">${fmt(e.mean_a, 3)}</td><td class="num">${fmt(e.mean_b, 3)}</td><td class="num ${e.delta < 0 ? "bad-t" : e.delta > 0 ? "ok-t" : ""}">${e.delta > 0 ? "+" : ""}${fmt(e.delta, 3)}</td>
          <td class="num mono">${e.ci95_delta ? `[${fmt(e.ci95_delta[0], 3)}, ${fmt(e.ci95_delta[1], 3)}]` : "–"}</td><td class="num">${e.wins} / ${e.losses} / ${e.ties}</td><td class="num">${e.sign_test_p}</td><td>${e.significant ? badge("significant", e.delta < 0 ? "b-bad" : "b-ok") : ""}</td></tr>`).join("")}</tbody></table></div>
        <h3 class="mt">Regressions (passed in A, fail in B)</h3>${c.regressions.slice(0, 10).map(r => `<div class="find fail small"><b>${esc(r.input)}</b> - failed ${esc(r.failed.join(", "))}<br><span class="muted">A:</span> ${esc(r.output_a)}<br><span class="muted">B:</span> ${esc(r.output_b)}</div>`).join("") || '<p class="muted small">None.</p>'}</div>`;
    } catch (e) { toast(e.message, "bad"); }
  };
};
function gateDrawer(g) {
  openDrawer(`<h1>Release gate ${g.passed ? '<span class="ok-t">PASSED</span>' : '<span class="bad-t">FAILED</span>'}</h1><p class="sub">${esc(g.experiment)} vs baseline ${esc(g.baseline || "none")} · ${esc(when(g.evaluated_at))}</p>
    ${g.checks.map(c => `<div class="find ${c.result}">${badge(c.result, c.result === "pass" ? "b-ok" : c.result === "warn" ? "b-warn" : "b-bad")} <b>${esc(c.check)}</b> <span class="small muted">${esc(c.detail)}</span></div>`).join("")}
    <p class="small muted mt">CI: <code>python tools/lumen_gate.py gate --key … --dataset … --outputs outputs.jsonl</code> exits 1 when the gate fails.</p>`);
}
async function expDrawer(id) {
  const e = await api(`/api/experiments/${id}`), s = e.summary;
  openDrawer(`<h1>${esc(e.name)}</h1><p class="sub">${esc(e.id)} · ${esc(e.source)} · pass rate ${pct(s.pass_rate * 100)} · ${s.items} items</p>
    <table><thead><tr><th>Evaluator</th><th class="num">Mean</th><th class="num">Pass rate</th></tr></thead><tbody>${Object.entries(s.evaluators || {}).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${fmt(v.mean, 3)}</td><td class="num">${pct(v.pass_rate * 100)}</td></tr>`).join("")}</tbody></table>
    ${s.gate ? `<div class="find ${s.gate.passed ? "pass" : "fail"} mt">Gate ${s.gate.passed ? "passed" : "failed"}: ${esc(s.gate.checks.filter(c => c.result !== "pass").map(c => c.check).join(", ") || "all checks passed")}</div>` : ""}
    <h3 class="mt">Items (failures first)</h3>${e.results.slice(0, 40).map(r => `<div class="find ${r.passed ? "pass" : "fail"} small"><b>${esc(r.input)}</b><br>${esc(r.output)}<br>${Object.entries(r.scores).map(([k, v]) => badge(`${k} ${fmt(v.score, 2)}`, v.passed ? "b-ok" : "b-bad")).join(" ")}</div>`).join("")}`);
}
async function dsDrawer(id) {
  const d = await api(`/api/datasets/${id}`);
  openDrawer(`<h1>${esc(d.name)}</h1><p class="sub">${d.items.length} items · ${esc(d.description || "")}</p>
    <label class="fld">Add items (JSONL or CSV with input, expected, context)<textarea id="dit" placeholder='{"input": "...", "expected": "...", "context": "..."}'></textarea></label><button class="btn primary mt" id="dsave">Add items</button>
    <h3 class="mt">Items</h3>${d.items.map(i => `<div class="find small"><b>${esc(i.input)}</b>${i.expected ? `<br><span class="muted">expected:</span> ${esc(i.expected)}` : ""}${i.source_trace ? `<br><span class="muted">from trace</span> ${tl(i.source_trace)}` : ""}</div>`).join("")}`);
  $("#dsave").onclick = async () => { const by = needName(); if (!by) return; try { const r = await api(`/api/datasets/${id}/items`, { body: { text: $("#dit").value, by } }); toast(`Added ${r.added} items`, "ok"); dsDrawer(id); } catch (e) { toast(e.message, "bad"); } };
}

/* ------------------------------------------------------------------ Prompts */
V.prompts = async () => {
  const pls = await api(`/api/prompts?project=${P()}`);
  view().innerHTML = `<h1>Prompt Registry</h1><p class="sub">Versioned prompts your app fetches at runtime (<code>lumen.get_prompt(name)</code>). A version reaches production only with a passing release gate and a named approver; rollbacks come from the ops agent after the Critic checks them.</p>
  ${pls.map(p => `<div class="card mt"><div class="row" style="justify-content:space-between"><h2 class="mono">${esc(p.name)}</h2><span>${Object.entries(p.labels).map(([l, v]) => badge(`${l} → v${v.version}`, l === "production" ? "b-ok" : "b-mute")).join(" ")}</span></div>
    <div class="tbl-wrap"><table><thead><tr><th>Version</th><th>Model</th><th>Created</th><th>Note</th><th>Release gate</th><th></th></tr></thead><tbody>
    ${p.versions.map(v => `<tr><td>v${v.version} ${p.labels.production && p.labels.production.version === v.version ? badge("production", "b-ok") : ""}</td><td class="small">${esc(v.model)}</td><td class="small">${esc(when(v.created_at))} · ${esc(v.created_by)}</td><td class="small">${esc(v.note || "")}</td>
      <td>${v.gate ? badge(v.gate.passed ? "passed" : "failed", v.gate.passed ? "b-ok" : "b-bad") : '<span class="small muted">no experiment</span>'}</td>
      <td class="row" style="gap:6px;flex-wrap:nowrap"><button class="btn sm" data-show="${esc(p.name)}|${v.version}">Template</button>${!(p.labels.production && p.labels.production.version === v.version) ? `<button class="btn sm" data-promote="${esc(p.name)}|${v.version}">Promote</button>` : ""}</td></tr>`).join("")}</tbody></table></div></div>`).join("")}
  <div class="card mt"><h2>New version</h2><div class="grid g3"><label class="fld">Prompt name<input id="pn" placeholder="rag_answer"></label><label class="fld">Model<input id="pm" value="smart-model"></label><label class="fld">Note<input id="pnote"></label></div>
    <label class="fld mt">Template ({input}, {context})<textarea id="pt"></textarea></label><button class="btn primary mt" id="padd">Create version</button></div>`;
  $$("[data-show]").forEach(b => b.onclick = async () => { const [n, v] = b.dataset.show.split("|"); const p = await api(`/api/prompts/${n}/${v}?project=${P()}`); openDrawer(`<h1 class="mono">${esc(n)} v${v}</h1><p class="sub">${esc(p.model)} · ${esc(p.note || "")}</p><div class="txt">${esc(p.template)}</div>`); });
  $$("[data-promote]").forEach(b => b.onclick = async () => {
    const by = needName(); if (!by) return; const [n, v] = b.dataset.promote.split("|");
    const r = await api("/api/prompts/promote", { body: { project: P(), name: n, version: Number(v), by } }).catch(e => ({ ok: false, reasons: [e.message] }));
    toast(r.ok ? `${n} v${v} is now production` : `Blocked: ${(r.reasons || []).join("; ")}`, r.ok ? "ok" : "bad"); refreshStatus(); V.prompts();
  });
  $("#padd").onclick = async () => { const by = needName(); if (!by) return; try { const r = await api("/api/prompts", { body: { project: P(), name: $("#pn").value, template: $("#pt").value, model: $("#pm").value, note: $("#pnote").value, by } }); toast(`Created v${r.version} - run an experiment and the gate before promoting`, "ok"); V.prompts(); } catch (e) { toast(e.message, "bad"); } };
};

/* ------------------------------------------------------------------ Approvals */
V.approvals = async () => {
  const [ps, lr] = await Promise.all([api(`/api/proposals?project=${P()}`), api("/api/llm/requests")]);
  const open = ps.filter(p => ["pending_approval", "critic_blocked"].includes(p.status));
  const pend = lr.filter(r => r.status === "pending");
  view().innerHTML = `<h1>Approvals</h1><p class="sub">Nothing changes and no LLM is called until a named person approves it here. Guardrails re-check every approval.</p>
  <h2>LLM permission requests (${pend.length})</h2>${pend.map(r => `<div class="card mt"><div class="row" style="justify-content:space-between"><div><b>${esc(r.purpose.replace("_", " "))}</b> for <span class="mono">${esc(r.target)}</span><div class="small muted">${esc(r.id)} · requested by ${esc(r.created_by)} · ${esc(when(r.created_at))}</div></div>${st(r.status)}</div>
    <div class="grid g4 mt">${kpi("Calls", r.n_calls, "one per item")}${kpi("Est. tokens", r.est_tokens.toLocaleString(), "in + max out")}${kpi("Model", esc(r.model), "local")}${kpi("Endpoint", `<span style="font-size:13px" class="mono">${esc(r.endpoint)}</span>`, "data stays on this network")}</div>
    <h3 class="mt">Exactly what will be sent (first prompt, PII redacted)</h3><div class="txt">${esc(r.preview)}</div>
    <div class="row"><button class="btn good" data-llm="approve" data-id="${esc(r.id)}">Approve & run</button><button class="btn danger" data-llm="reject" data-id="${esc(r.id)}">Reject</button></div></div>`).join("") || '<div class="card empty">No LLM requests waiting.</div>'}
  <h2 class="mt2">Agent proposals (${open.length})</h2>${open.map(p => `<div class="card mt"><div class="row" style="justify-content:space-between"><div><b>${esc(p.title)}</b><div class="small muted">${esc(p.id)} · ${esc(p.kind.replace("_", " "))} · ${esc(when(p.created_at))} · run ${esc(p.run_id)}</div></div>${st(p.status)}</div>
    ${p.payload.action ? `<p class="small">${esc(p.payload.action)}</p>` : ""}${p.kind === "rollback_prompt" ? `<p class="small">Production '${esc(p.payload.prompt)}' v${p.payload.from} → v${p.payload.to} · ${esc(p.payload.metric)} ${fmt(p.payload.window_value, 3)} vs ${fmt(p.payload.others_value, 3)} for other versions</p>` : ""}
    <h3 class="mt">Critic</h3>${(p.critic || []).map(f => `<div class="find ${f.result}">${badge(f.result, f.result === "pass" ? "b-ok" : f.result === "warn" ? "b-warn" : "b-bad")} <b>${esc(f.check)}</b> <span class="small muted">${esc(f.detail)}</span></div>`).join("")}
    <p class="small muted">Evidence: ${p.evidence.slice(0, 10).map(tl).join(", ")}${p.evidence.length > 10 ? ` +${p.evidence.length - 10}` : ""}</p>
    <div class="row"><input data-note="${esc(p.id)}" placeholder="note (optional)" style="flex:1;min-width:180px">${p.status === "pending_approval" ? `<button class="btn good" data-dec="approve" data-id="${esc(p.id)}">${p.kind === "rollback_prompt" ? "Approve rollback" : p.kind === "dataset_items" ? "Approve & add items" : "Accept as work item"}</button>` : '<span class="small bad-t">Blocked by the Critic</span>'}
      <button class="btn danger" data-dec="reject" data-id="${esc(p.id)}">Reject</button></div></div>`).join("") || '<div class="card empty">No open proposals. Run the ops agent.</div>'}
  <div class="card mt2"><h2>History</h2><div class="tbl-wrap"><table><thead><tr><th>Item</th><th>Status</th><th>By</th><th>When</th></tr></thead><tbody>
    ${[...ps.filter(p => !open.includes(p)).map(p => ({ t: p.title, s: p.status, by: p.decided_by, w: p.decided_at })), ...lr.filter(r => r.status !== "pending").map(r => ({ t: `LLM ${r.purpose} ${r.id}${r.result ? ` · ${r.result.calls} calls, ${r.result.errors} errors` : ""}`, s: r.status, by: r.decided_by, w: r.decided_at }))]
      .map(x => `<tr><td>${esc(x.t)}</td><td>${st(x.s)}</td><td>${esc(x.by || "–")}</td><td class="small">${esc(when(x.w))}</td></tr>`).join("") || '<tr><td colspan="4" class="empty">Nothing yet</td></tr>'}</tbody></table></div></div>`;
  $$("[data-llm]").forEach(b => b.onclick = async () => {
    const by = needName(); if (!by) return;
    try { const r = await api(`/api/llm/requests/${b.dataset.id}/decision`, { body: { decision: b.dataset.llm, by } });
      toast(r.ok ? (r.status === "running" ? "Approved - running on your local model" : `Request ${r.status}`) : `Blocked: ${(r.reasons || []).join("; ")}`, r.ok ? "ok" : "bad"); } catch (e) { toast(e.message, "bad"); }
    refreshStatus(); V.approvals();
  });
  $$("[data-dec]").forEach(b => b.onclick = async () => {
    const by = needName(); if (!by) return;
    try { const r = await api(`/api/proposals/${b.dataset.id}/decision`, { body: { decision: b.dataset.dec, by, note: ($(`[data-note="${b.dataset.id}"]`) || {}).value || "" } });
      toast(r.ok ? `${r.status}${r.production ? ` - production is now v${r.production}` : ""}${r.added ? ` - ${r.added} items added` : ""}` : `Blocked: ${(r.reasons || []).join("; ")}`, r.ok ? "ok" : "bad"); } catch (e) { toast(e.message, "bad"); }
    refreshStatus(); V.approvals();
  });
};

/* ------------------------------------------------------------------ Guardrails */
V.guardrails = async () => {
  const [{ policy }, log] = await Promise.all([api("/api/policy"), api("/api/guard-log?limit=150")]);
  const nums = [["gate_min_pass_rate", "Gate: min pass rate (0-1)"], ["gate_max_regression_pct", "Gate: max regression (pts)"], ["gate_max_cost_increase_pct", "Gate: max cost increase (%)"],
    ["max_llm_calls_per_request", "Max LLM calls per request"], ["min_evidence_traces", "Min evidence traces"], ["max_proposals_per_day", "Max proposals / day"],
    ["max_steps_per_run", "Step budget per run"], ["slo_ms", "Latency SLO (ms)"], ["baseline_hours", "Baseline window (h)"]];
  const tc = { auto: "b-ok", human_approval: "b-warn", admin: "b-vio", forbidden: "b-bad" };
  view().innerHTML = `<h1>Guardrails</h1><p class="sub">Enforced in code on every agent and human action. Unknown actions are denied; forbidden actions cannot be enabled. Cloud LLM calls and reading secrets are hard-forbidden.</p>
  <div class="grid g3"><div class="card"><h2>Kill switch</h2><div class="kpi"><div class="val ${policy.agents_paused ? "bad-t" : "ok-t"}">${policy.agents_paused ? "PAUSED" : "ACTIVE"}</div></div><button class="btn ${policy.agents_paused ? "good" : "danger"} mt" id="kill">${policy.agents_paused ? "Resume agents" : "Pause all agents"}</button>
    <h3 class="mt">Data protection</h3><label class="small"><input type="checkbox" id="pii" ${policy.redact_pii ? "checked" : ""}> Redact PII before storage</label><br><label class="small"><input type="checkbox" id="nonlocal" ${policy.llm_allow_non_local ? "checked" : ""}> Allow non-local LLM endpoints (not recommended)</label></div>
    <div class="card span2"><div class="row" style="justify-content:space-between"><h2>Red-team suite</h2><button class="btn primary" id="rt">Run attacks</button></div><p class="small muted">Self-approval, cloud LLM calls, secret access, data leaving the network, promotion without gate, runaway jobs and loops, PII storage, ledger tampering…</p><div id="rtres"></div></div></div>
  <div class="grid g2 mt"><div class="card"><h2>Limits</h2><div class="grid g2">${nums.map(([k, l]) => `<label class="fld">${l}<input type="number" step="any" data-pk="${k}" value="${esc(policy[k])}"></label>`).join("")}</div><button class="btn primary mt" id="save">Save (admin)</button></div>
    <div class="card"><h2>Permission tiers</h2><table><tbody>${Object.entries(policy.permissions).map(([a, t]) => `<tr><td class="mono">${esc(a)}</td><td>${badge(t.replace("_", " "), tc[t] || "b-mute")}</td></tr>`).join("")}</tbody></table></div></div>
  <div class="card mt"><h2>Guard decisions</h2><div class="tbl-wrap"><table><thead><tr><th>Time</th><th>Actor</th><th>Action</th><th>Target</th><th>Decision</th><th>Reason</th></tr></thead><tbody>
    ${log.slice(0, 120).map(r => `<tr><td class="small">${esc(when(r.ts))}</td><td class="mono">${esc(r.actor)}</td><td class="mono">${esc(r.action)}</td><td class="small">${esc(r.target)}</td><td>${st(r.decision)}</td><td class="small muted">${esc(r.reasons.join("; "))}</td></tr>`).join("")}</tbody></table></div></div>`;
  const save = async changes => { const by = needName(); if (!by) return; const r = await api("/api/policy", { method: "PUT", body: { changes, by } }); toast(r.ok ? "Policy saved (sealed in ledger)" : (r.reasons || []).join("; "), r.ok ? "ok" : "bad"); refreshStatus(); V.guardrails(); };
  $("#kill").onclick = async () => { const by = needName(); if (!by) return; await api("/api/policy/killswitch", { body: { paused: !policy.agents_paused, by } }); refreshStatus(); V.guardrails(); };
  $("#pii").onchange = e => save({ redact_pii: e.target.checked });
  $("#nonlocal").onchange = e => save({ llm_allow_non_local: e.target.checked });
  $("#save").onclick = () => { const ch = {}; $$("[data-pk]").forEach(i => { if (String(policy[i.dataset.pk]) !== i.value) ch[i.dataset.pk] = Number(i.value); }); Object.keys(ch).length ? save(ch) : toast("Nothing changed"); };
  $("#rt").onclick = async () => {
    const r = await api("/api/redteam", { method: "POST" });
    $("#rtres").innerHTML = `<p><b class="${r.passed === r.total ? "ok-t" : "bad-t"}">${r.passed} / ${r.total} attacks stopped</b></p><div class="tbl-wrap"><table><tbody>${r.cases.map(c => `<tr><td>${badge(c.passed ? "held" : "FAILED", c.passed ? "b-ok" : "b-bad")}</td><td><b>${esc(c.name)}</b><div class="small muted">${esc(c.attack)}</div></td><td class="small">${esc(c.expected)}</td><td class="small mono">${esc(c.actual.slice(0, 90))}</td></tr>`).join("")}</tbody></table></div>`;
  };
};

/* ------------------------------------------------------------------ Ledger */
V.ledger = async () => {
  let offset = 0, type = "";
  view().innerHTML = `<h1>Audit Ledger</h1><p class="sub">Append-only SHA-256 hash chain of agent runs, guard decisions, approvals, LLM requests, prompt changes and ingestion batches.</p>
  <div class="grid g2"><div class="card" id="vc"></div><div class="card"><h2>Tamper test</h2><p class="small muted">Edits one entry in an in-memory copy and re-verifies.</p><button class="btn" id="tamp">Simulate tampering</button><div id="tres" class="mt"></div></div></div>
  <div class="card mt"><div class="row" style="justify-content:space-between"><h2>Entries</h2><select id="lt"><option value="">all event types</option></select></div><div class="tbl-wrap"><table><thead><tr><th class="num">Seq</th><th>Time</th><th>Event</th><th>Actor</th><th>Target</th><th>Payload</th><th>Hash</th></tr></thead><tbody id="lr"></tbody></table></div>
    <div class="row mt"><span class="small muted" id="lc"></span><span style="flex:1"></span><button class="btn sm" id="lp">Newer</button><button class="btn sm" id="ln">Older</button></div></div>`;
  const v = await api("/api/ledger/verify");
  $("#vc").innerHTML = `<h2>Chain verification</h2><div class="kpi"><div class="val ${v.ok ? "ok-t" : "bad-t"}">${v.ok ? "VERIFIED" : "BROKEN"}</div><div class="note">${v.count} entries</div></div><p class="small mt">Merkle root<br><span class="hash">${esc(v.merkle_root)}</span></p>`;
  const load = async () => {
    const p = new URLSearchParams({ limit: 50, offset }); if (type) p.set("event_type", type);
    const d = await api(`/api/ledger?${p}`);
    if ($("#lt").options.length === 1) $("#lt").innerHTML += d.types.map(t => `<option>${esc(t)}</option>`).join("");
    $("#lr").innerHTML = d.items.map(e => `<tr><td class="num mono">${e.seq}</td><td class="small">${esc(when(e.ts))}</td><td>${badge(e.event_type, /GUARD|REDTEAM/.test(e.event_type) ? "b-vio" : /LLM/.test(e.event_type) ? "b-info" : /HUMAN|PROMPT|POLICY|KEY/.test(e.event_type) ? "b-warn" : "b-mute")}</td><td class="mono small">${esc(e.actor)}</td><td class="small">${esc(e.target)}</td><td class="small mono" style="max-width:360px;word-break:break-word">${esc(e.payload.slice(0, 150))}</td><td class="hash">${esc(short(e.entry_hash))}</td></tr>`).join("");
    $("#lc").textContent = `${offset + 1}–${offset + d.items.length} of ${d.total}`; $("#lp").disabled = offset === 0; $("#ln").disabled = offset + d.items.length >= d.total;
  };
  $("#lt").onchange = e => { type = e.target.value; offset = 0; load(); }; $("#lp").onclick = () => { offset = Math.max(0, offset - 50); load(); }; $("#ln").onclick = () => { offset += 50; load(); };
  $("#tamp").onclick = async () => { const r = await api("/api/ledger/tamper-demo"); $("#tres").innerHTML = `<div class="find fail">Edited seq ${r.tampered_seq} (${esc(r.event_type)}) → chain <b>broken at seq ${r.after_tampering.broken_at}</b>: ${esc(r.after_tampering.reason)}</div>`; };
  await load();
};

/* ------------------------------------------------------------------ Settings */
V.settings = async () => {
  const [keys, llm, pricing, projects] = await Promise.all([api(`/api/keys?project=${P()}`), api("/api/llm/config"), api("/api/pricing"), api("/api/projects")]);
  const host = location.origin;
  view().innerHTML = `<h1>Settings & Keys</h1><p class="sub">Projects, ingestion keys, the local LLM and model pricing.</p>
  <div class="grid g2"><div class="card"><h2>Local LLM (Ollama)</h2><p class="small muted">Lumen never uses cloud LLMs or API keys. Point it at an Ollama server on your machine. Every job still needs your approval on the Approvals page.</p>
    <label class="small"><input type="checkbox" id="le" ${llm.enabled ? "checked" : ""}> Enabled</label>
    <div class="grid g2 mt"><label class="fld">Endpoint<input id="lu" value="${esc(llm.base_url)}"></label><label class="fld">Model<input id="lm" value="${esc(llm.model)}"></label></div>
    <p class="small ${llm.endpoint_ok ? "ok-t" : "bad-t"}">${esc(llm.endpoint_reason)}</p><div class="row"><button class="btn primary" id="lsave">Save (admin)</button><button class="btn" id="ltest">Test connection (lists models, sends no data)</button></div><div id="lres" class="mt small"></div></div>
  <div class="card"><h2>Ingestion keys - ${esc((projects.find(p => p.id === P()) || {}).name || P())}</h2><div class="row"><input id="kn" placeholder="key name, e.g. prod-backend"><button class="btn primary" id="kadd">Create key (admin)</button></div><div id="knew" class="mt"></div>
    <table class="mt"><tbody>${keys.map(k => `<tr><td><b>${esc(k.name)}</b><div class="small muted mono">${esc(k.prefix)}… · ${esc(when(k.created_at))} · ${esc(k.created_by)}</div></td><td>${k.revoked ? badge("revoked", "b-mute") : `<button class="btn sm danger" data-rev="${esc(k.id)}">Revoke</button>`}</td></tr>`).join("") || '<tr><td class="muted small">No keys yet.</td></tr>'}</tbody></table></div></div>
  <div class="grid g2 mt"><div class="card"><h2>Integrate</h2><p class="small muted">Python SDK (copy <code>sdk/lumen_sdk.py</code> into your app):</p>
    <code class="blk">from lumen_sdk import Lumen
lumen = Lumen(api_key="lmn_…", host="${esc(host)}")

with lumen.start_trace("rag_answer", input=q, prompt_name="rag_answer",
                       prompt_version=lumen.get_prompt("rag_answer")["version"]) as t:
    with t.span("retrieval", name="kb_search", input=q) as s:
        s.output = docs
    with t.span("llm", name="generate", model="smart-model") as s:
        s.output, s.tokens_in, s.tokens_out = answer, 812, 64
    t.output = answer</code>
    <p class="small muted mt">REST:</p><code class="blk">curl -X POST ${esc(host)}/v1/traces -H "X-Lumen-Key: lmn_…" \\
  -H "Content-Type: application/json" \\
  -d '{"name":"chat","model":"fast-model","input":"hi","output":"hello",
       "tokens_in":5,"tokens_out":3,"latency_ms":420}'</code>
    <p class="small muted mt">CI release gate:</p><code class="blk">python tools/lumen_gate.py gate --key lmn_… --dataset rag-golden \\
  --name "PR-42" --prompt-name rag_answer --prompt-version 5 --outputs outputs.jsonl</code></div>
  <div class="card"><h2>Model pricing (USD per 1M tokens)</h2><p class="small muted">Example values - set your contract prices.</p><table><thead><tr><th>Model</th><th class="num">Input</th><th class="num">Output</th></tr></thead><tbody id="prt">
    ${Object.entries(pricing).map(([m, p]) => `<tr><td><input data-pm value="${esc(m)}"></td><td><input data-pi type="number" step="any" value="${p.in}" style="width:90px"></td><td><input data-po type="number" step="any" value="${p.out}" style="width:90px"></td></tr>`).join("")}
    <tr><td><input data-pm placeholder="new model"></td><td><input data-pi type="number" step="any" style="width:90px"></td><td><input data-po type="number" step="any" style="width:90px"></td></tr></tbody></table><button class="btn primary mt" id="psave">Save pricing</button>
    <h2 class="mt2">Projects</h2>${projects.map(p => `<div class="small">${esc(p.name)} <span class="muted mono">${esc(p.id)} · ${p.traces} traces</span></div>`).join("")}<div class="row mt"><input id="pnm" placeholder="new project name"><button class="btn" id="padd">Create project</button></div></div></div>`;
  $("#lsave").onclick = async () => { const by = needName(); if (!by) return; const r = await api("/api/llm/config", { method: "PUT", body: { changes: { enabled: $("#le").checked, base_url: $("#lu").value, model: $("#lm").value }, by } }); toast(r.ok ? `Saved${r.endpoint_ok ? "" : " - endpoint not allowed: " + r.endpoint_reason}` : (r.reasons || []).join("; "), r.ok && r.endpoint_ok ? "ok" : "bad"); refreshStatus(); V.settings(); };
  $("#ltest").onclick = async () => { const by = needName(); if (!by) return; const r = await api("/api/llm/test", { body: { by } }); $("#lres").innerHTML = r.ok ? `<span class="ok-t">Connected.</span> Models: ${esc(r.models.join(", ") || "none")} ${r.model_installed ? "" : '<span class="warn-t">- configured model not installed (ollama pull …)</span>'}` : `<span class="bad-t">${esc(r.reason)}</span>`; };
  $("#kadd").onclick = async () => { const by = needName(); if (!by) return; const r = await api("/api/keys", { body: { project: P(), name: $("#kn").value || "key", by } }); if (r.ok) { $("#knew").innerHTML = `<div class="find warn">Copy now - shown once:<br><code class="mono">${esc(r.key)}</code></div>`; } else toast((r.reasons || []).join("; "), "bad"); };
  $$("[data-rev]").forEach(b => b.onclick = async () => { const by = needName(); if (!by) return; await api(`/api/keys/${b.dataset.rev}/revoke`, { body: { by } }); V.settings(); });
  $("#psave").onclick = async () => { const by = needName(); if (!by) return; const pr = {}; $$("#prt tr").forEach(tr => { const m = $("[data-pm]", tr).value.trim(); if (m) pr[m] = { in: Number($("[data-pi]", tr).value || 0), out: Number($("[data-po]", tr).value || 0) }; }); await api("/api/pricing", { method: "PUT", body: { pricing: pr, by } }); toast("Pricing saved - applies to new traces", "ok"); };
  $("#padd").onclick = async () => { const by = needName(); if (!by) return; const r = await api("/api/projects", { body: { name: $("#pnm").value, by } }); store.set("project", r.id); await loadProjects(); refreshStatus(); V.settings(); };
};

/* ------------------------------------------------------------------ router */
async function route() {
  closeDrawer();
  const [path, qs] = location.hash.replace(/^#\/?/, "").split("?");
  renderNav();
  view().innerHTML = `<p class="muted">Loading…</p>`;
  try { await (V[path] || V[""])(new URLSearchParams(qs || "")); } catch (e) { view().innerHTML = errCard(e); }
  $("#side").classList.remove("on");
}
document.addEventListener("click", e => { const a = e.target.closest("[data-trace]"); if (a) { e.preventDefault(); traceDrawer(a.dataset.trace); } });
document.addEventListener("keydown", e => { if (e.key === "Escape") closeDrawer(); });
$("#drawer-bg").onclick = closeDrawer;
$("#menu").onclick = () => $("#side").classList.toggle("on");
$("#operator").value = store.get("operator", "");
$("#operator").oninput = e => store.set("operator", e.target.value.trim());
$("#project").onchange = async e => { store.set("project", e.target.value); await refreshStatus(); route(); };
window.addEventListener("hashchange", route);
(async () => {
  try { META = await api("/api/meta"); await loadProjects(); } catch (e) { view().innerHTML = errCard(e); return; }
  await refreshStatus(); startEvents(); route(); setInterval(refreshStatus, 20000);
})();
