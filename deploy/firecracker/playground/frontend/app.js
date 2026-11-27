// Firecracker Playground frontend: a static single-page app. Its only dependency is the
// Playground backend API (/api/v1); the backend URL comes from config.js or the settings dialog.
"use strict";

const CFG = window.PLAYGROUND_CONFIG || {};
const $ = (id) => document.getElementById(id);
const store = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

const state = {
  api: (store.get("pg.api", CFG.apiBaseUrl || "") || "").replace(/\/+$/, ""),
  token: store.get("pg.token", ""),
  user: store.get("pg.user", CFG.defaultUser || "alice"),
  me: null,
  sessions: new Map(),     // session_id -> session description
  external: [],
  current: null,           // open session id
  view: "chat",            // chat | ops
  lastEventId: 0,
  evCtrl: null,
  unread: {},              // session_id -> count
  live: null,              // live run rendering state for the open session
  tab: "overview",
  alerts: [],
};

// --------------------------------------------------------------------- API

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(method, path, body) {
  if (!state.api) throw new ApiError(0, "set the backend URL in Settings");
  const headers = { "Authorization": `Bearer ${state.token}`, "X-Playground-User": state.user };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let resp;
  try {
    resp = await fetch(`${state.api}/api/v1${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch (e) {
    throw new ApiError(0, `backend unreachable (${state.api}): ${e.message}`);
  }
  let data = null;
  try { data = await resp.json(); } catch { /* empty */ }
  if (!resp.ok) throw new ApiError(resp.status, (data && data.error) || resp.statusText);
  return data;
}

function toast(text, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = text;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "error" ? 8000 : 4000);
}

async function guarded(fn, okText) {
  try {
    const result = await fn();
    if (okText) toast(typeof okText === "function" ? okText(result) : okText, "ok");
    return result;
  } catch (e) {
    toast(`${e.status ? e.status + ": " : ""}${e.message}`, "error");
    if (e.status === 401) openSettings();
    return undefined;
  }
}

// ------------------------------------------------------------ event stream

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function connectEvents() {
  if (state.evCtrl) state.evCtrl.abort();
  const ctrl = new AbortController();
  state.evCtrl = ctrl;
  state.lastEventId = 0;
  while (!ctrl.signal.aborted) {
    try {
      const resp = await fetch(`${state.api}/api/v1/events?last_event_id=${state.lastEventId}`, {
        headers: { "Authorization": `Bearer ${state.token}`, "X-Playground-User": state.user },
        signal: ctrl.signal,
      });
      if (!resp.ok) {
        setConn(false);
        if (resp.status === 401) { openSettings(); return; }
        await sleep(3000);
        continue;
      }
      setConn(true);
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const raw = buf.slice(0, i);
          buf = buf.slice(i + 2);
          const data = raw.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trim()).join("\n");
          if (data) {
            try { onEvent(JSON.parse(data)); } catch (e) { console.warn("bad event", e); }
          }
        }
      }
    } catch (e) {
      if (ctrl.signal.aborted) return;
    }
    setConn(false);
    await sleep(2000);
  }
}

function setConn(on) { $("conn").className = `conn ${on ? "on" : "off"}`; $("conn").title = on ? "live" : "disconnected"; }

function onEvent(ev) {
  if (ev.id) state.lastEventId = Math.max(state.lastEventId, ev.id);
  const sid = ev.session_id;
  switch (ev.type) {
    case "session.updated":
      state.sessions.set(sid, { ...(state.sessions.get(sid) || {}), ...ev.session });
      renderSessions();
      if (sid === state.current) { renderHeader(); if (state.tab === "overview") renderTab(); }
      break;
    case "session.deleted":
      state.sessions.delete(sid);
      if (sid === state.current) openSession(null);
      renderSessions();
      break;
    case "run.started":
      markRunning(sid, ev.run_id);
      if (sid === state.current) startLive(ev.run_id, ev.text);
      break;
    case "run.chunk":
      if (sid === state.current) liveChunk(ev);
      break;
    case "run.finished":
      markRunning(sid, null);
      if (sid === state.current) finishLive(ev);
      else bumpUnread(sid);
      break;
    case "vm.message":
      if (sid === state.current) upsertVmCard(ev.message);
      else if (ev.message.type === "question" && ev.message.status === "pending") bumpUnread(sid);
      if (ev.message.type === "question" && ev.message.status === "pending" && sid !== state.current) {
        const s = state.sessions.get(sid);
        toast(`Question from a task in "${s ? s.title : sid}": ${ev.message.text}`);
      }
      break;
    case "alert":
      state.alerts = ev.alerts || [];
      renderAlertBadge();
      if (state.view === "ops") renderOps();
      break;
  }
}

function markRunning(sid, runId) {
  const s = state.sessions.get(sid);
  if (s) { s.running_run_id = runId; renderSessions(); }
  if (sid === state.current) renderHeader();
}

function bumpUnread(sid) { state.unread[sid] = (state.unread[sid] || 0) + 1; renderSessions(); }

// -------------------------------------------------------------- rendering

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Safe "markdown light": escaped text with code blocks, inline code, bold and paragraphs.
function renderText(text) {
  const parts = String(text ?? "").split(/```/);
  return parts.map((part, i) => {
    if (i % 2 === 1) {
      const body = part.replace(/^[\w+-]*\n/, "");
      return `<pre><code>${esc(body)}</code></pre>`;
    }
    return esc(part)
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .split(/\n{2,}/).map((p) => p.trim() ? `<p>${p.replace(/\n/g, "<br>")}</p>` : "").join("");
  }).join("");
}

function stateOf(s) { return (s && s.vm && s.vm.state) || "UNKNOWN"; }
function initials(name) { return name.slice(0, 2).toUpperCase(); }

function renderUser() {
  $("user-name").textContent = state.user;
  $("user-avatar").textContent = initials(state.user);
  const tier = [...state.sessions.values()].map((s) => s.vm && s.vm.tier).find(Boolean);
  $("user-tier").textContent = tier ? `tier: ${tier}` : "";
  const users = (state.me && state.me.users) || [state.user];
  $("user-options").innerHTML = users.map((u) =>
    `<div class="menu-item" data-user="${esc(u)}"><span class="avatar">${esc(initials(u))}</span>${esc(u)}${u === state.user ? " ✓" : ""}</div>`).join("");
}

function renderSessions() {
  const list = [...state.sessions.values()].sort((a, b) =>
    (b.last_query_ts || b.created_ts || 0) - (a.last_query_ts || a.created_ts || 0));
  $("session-list").innerHTML = list.length ? list.map((s) => {
    const unread = state.unread[s.session_id];
    return `<div class="session-item ${s.session_id === state.current ? "active" : ""}" data-sid="${esc(s.session_id)}" title="${esc(s.session_id)} · ${esc(stateOf(s))}">
      <span class="dot ${esc(stateOf(s))}"></span>
      <span class="title">${s.kind === "team" ? "👥 " : ""}${esc(s.title)}</span>
      ${s.running_run_id ? '<span class="spinner"></span>' : ""}
      ${unread ? `<span class="badge">${unread}</span>` : ""}
    </div>`;
  }).join("") : '<div class="muted small" style="padding:6px 10px">No sessions yet</div>';
  $("external-wrap").classList.toggle("hidden", !state.external.length);
  $("external-list").innerHTML = state.external.map((s) =>
    `<div class="session-item" data-adopt="${esc(s.session_id)}" title="Import into the playground">
      <span class="dot ${esc(stateOf(s))}"></span><span class="title">${esc(s.session_id)}</span><span class="small muted">import</span></div>`).join("");
  renderUser();
}

function renderHeader() {
  const s = state.sessions.get(state.current);
  $("chat-header").classList.toggle("hidden", !s || state.view !== "chat");
  if (!s) return;
  $("chat-kind").textContent = s.kind === "team" ? `AgentTeam · ${s.team_size}` : (s.kind === "external" ? "external" : "DeepAgent");
  $("chat-title").textContent = s.title;
  $("chat-state").textContent = stateOf(s);
  $("chat-state").className = `pill ${stateOf(s)}`;
  $("chat-running").classList.toggle("hidden", !s.running_run_id);
  $("btn-send").classList.toggle("hidden", !!s.running_run_id);
  $("btn-stop").classList.toggle("hidden", !s.running_run_id);
}

function renderAlertBadge() {
  const n = state.alerts.filter((a) => a.severity !== "info").length;
  $("alert-count").textContent = n;
  $("alert-count").className = `badge bad ${n ? "" : "hidden"}`;
}

// ------------------------------------------------------------------- chat

function setView(view) {
  state.view = view;
  $("chat-view").classList.toggle("hidden", view !== "chat");
  $("composer").classList.toggle("hidden", view !== "chat");
  $("ops-view").classList.toggle("hidden", view !== "ops");
  renderHeader();
}

async function openSession(sid) {
  setView("chat");
  state.current = sid;
  state.live = null;
  delete state.unread[sid];
  renderSessions();
  renderHeader();
  $("empty").classList.toggle("hidden", !!sid);
  $("messages").classList.toggle("hidden", !sid);
  $("messages").innerHTML = "";
  if (!sid) { $("details").classList.add("hidden"); return; }
  const s = await guarded(() => api("GET", `/sessions/${sid}`));
  if (!s || state.current !== sid) return;
  state.sessions.set(sid, { ...state.sessions.get(sid), ...s });
  renderHeader();
  const items = [
    ...(s.transcript || []).map((t) => ({ ts: t.ts, entry: t })),
    ...(s.vm_messages || []).map((m) => ({ ts: m.ts, vm: m })),
  ].sort((a, b) => a.ts - b.ts);
  let group = null;
  for (const item of items) {
    if (item.vm) { upsertVmCard(item.vm); group = null; continue; }
    const t = item.entry;
    if (t.role === "user") { appendUser(t.text); group = null; }
    else if (t.role === "assistant") {
      if (!group || group.member !== t.member || group.text) group = appendAssistant(t.member);
      group.text = t.text;
      group.body.innerHTML = renderText(t.text);
    }
    else if (t.kind === "error") { appendError(t.text); }
    else {
      if (!group || group.member !== t.member) group = appendAssistant(t.member);
      addChip(group, t.kind, t.text, "");
    }
  }
  if (s.running_run_id) startLive(s.running_run_id, null);
  if (!$("details").classList.contains("hidden")) renderTab();
  scrollDown(true);
}

function scrollDown(force) {
  const v = $("chat-view");
  if (force || v.scrollHeight - v.scrollTop - v.clientHeight < 160) v.scrollTop = v.scrollHeight;
}

function appendUser(text) {
  const el = document.createElement("div");
  el.className = "msg-user";
  el.textContent = text;
  $("messages").appendChild(el);
}

function appendAssistant(member) {
  const el = document.createElement("div");
  el.className = "msg-assistant";
  el.innerHTML = `${member ? `<div class="member">${esc(member)}</div>` : ""}
    <details class="activity hidden"><summary></summary><div class="chips steps"></div></details>
    <div class="body"></div>`;
  $("messages").appendChild(el);
  return {
    el, member, text: "", activity: el.querySelector(".activity"), chips: el.querySelector(".steps"),
    body: el.querySelector(".body"), pending: {}, tools: {}, steps: 0, running: false,
  };
}

function appendError(text) {
  const el = document.createElement("div");
  el.className = "msg-error";
  el.textContent = `⚠ ${text}`;
  $("messages").appendChild(el);
}

// Tool activity of one answer: a single collapsible list; each tool call and its result share one row.
function splitEvent(text, detail) {
  // live events: text = tool name, detail = "inputs: ..."; stored events: text = "name {...}"
  if (detail) return { name: text, detail };
  const i = String(text).indexOf(" ");
  return i < 0 ? { name: text, detail: "" } : { name: text.slice(0, i), detail: text.slice(i + 1) };
}

function previewOf(detail) {
  // payloads look like {'inputs': '{\'command\': \'curl ...\'}'}: take the first meaningful argument
  const m = String(detail).match(/(?:command|file_path|path|query|url|content)\\*['"]?\s*:\s*\\*(['"])([\s\S]{1,240})/);
  let raw = String(detail).replace(/^[a-z_]+:\s*/, "");
  if (m) {  // the value ends at the quote style that opened it
    const end = m[1] === '"' ? m[2].indexOf('"') : m[2].search(/\\*'/);
    raw = end >= 0 ? m[2].slice(0, end) : m[2];
  }
  return raw.replace(/\s+/g, " ").trim().slice(0, 140);
}

function updateActivity(group) {
  group.activity.classList.remove("hidden");
  const tools = Object.entries(group.tools).map(([n, c]) => (c > 1 ? `${n} ×${c}` : n)).join(", ");
  const verb = group.running ? "Working…" : "Used";
  group.activity.querySelector("summary").textContent =
    `${verb} ${group.steps} step${group.steps === 1 ? "" : "s"}${tools ? " · " + tools : ""}`;
}

function addChip(group, kind, text, detail, invokeId) {
  let { name, detail: info } = splitEvent(text, detail);
  if (!invokeId) {  // stored events keep the id inside their text
    const m = info.match(/'invoke_id': '([^']+)'/);
    if (m) { invokeId = m[1]; info = info.replace(/,? ?'invoke_id': '[^']+'/, ""); }
  }
  // Parallel calls finish out of order: pair a result with its call by id, else by tool name.
  const key = invokeId ? `id:${invokeId}` : `name:${name}`;
  if (kind === "tool_result") {
    const row = (group.pending[key] || []).shift();
    const failed = /success\\*['"]?\s*:\s*False/.test(info) || /error: (?!None)\S/.test(info);
    if (row) {
      row.querySelector(".state").textContent = failed ? "✗" : "✓";
      row.classList.toggle("failed", failed);
      row.querySelector(".detail").textContent += `

${info}`;
      updateActivity(group);
      return;
    }
  }
  const icon = { tool_call: "🔧", tool_result: "✓", team_event: "👥", other: "·" }[kind] || "·";
  const row = document.createElement("div");
  row.className = "chip step";
  row.innerHTML = `<div class="step-head"><span class="state">${kind === "tool_call" ? "…" : ""}</span>${icon}
    <b>${esc(name)}</b><span class="preview">${esc(previewOf(info))}</span></div><div class="detail"></div>`;
  row.querySelector(".detail").textContent = info;
  row.onclick = (e) => { if (!e.target.closest(".detail")) row.classList.toggle("open"); };
  group.chips.appendChild(row);
  if (kind === "tool_call") {
    (group.pending[key] = group.pending[key] || []).push(row);
    group.tools[name] = (group.tools[name] || 0) + 1;
  }
  group.steps += 1;
  updateActivity(group);
}

function startLive(runId, text) {
  if (text) appendUser(text);
  state.live = { runId, groups: new Map() };
  liveGroup(null).body.classList.add("cursor");
  scrollDown(true);
}

function liveGroup(member) {
  const key = member || "";
  if (!state.live.groups.has(key)) {
    // a member that starts talking gets its own block; drop the empty placeholder
    const placeholder = state.live.groups.get("");
    if (key && placeholder && !placeholder.text && !placeholder.chips.children.length) {
      placeholder.el.remove();
      state.live.groups.delete("");
    }
    const g = appendAssistant(member);
    g.running = true;
    g.body.classList.add("cursor");
    state.live.groups.set(key, g);
  }
  return state.live.groups.get(key);
}

function liveChunk(ev) {
  if (!state.live || state.live.runId !== ev.run_id) state.live = { runId: ev.run_id, groups: new Map() };
  const g = liveGroup(ev.member);
  if (ev.kind === "reasoning") { appendThinking(g, ev.text); scrollDown(false); return; }
  if (ev.kind === "delta" || ev.kind === "answer") closeThinking(g);
  if (ev.kind === "delta") { g.text += ev.text; g.body.innerHTML = renderText(g.text); }
  else if (ev.kind === "answer") { g.text = ev.text; g.body.innerHTML = renderText(g.text); }
  else {
    const detail = ev.data ? Object.entries(ev.data).filter(([k, v]) => v && k !== "invoke_id")
      .map(([k, v]) => `${k}: ${v}`).join("\n") : "";
    addChip(g, ev.kind, ev.text, detail, ev.data && ev.data.invoke_id);
  }
  scrollDown(false);
}

// Reasoning tokens stream into one collapsible "Thinking" block per answer (not one line per token).
function appendThinking(g, text) {
  if (!g.think) {
    const el = document.createElement("details");
    el.className = "thinking";
    el.open = true;
    el.innerHTML = '<summary>Thinking…</summary><div class="thinking-text"></div>';
    g.el.insertBefore(el, g.chips);
    g.think = { el, text: "", started: Date.now(), closed: false };
  }
  g.think.text += text;
  const box = g.think.el.querySelector(".thinking-text");
  box.textContent = g.think.text;
  box.scrollTop = box.scrollHeight;
}

function closeThinking(g) {
  if (!g.think || g.think.closed) return;
  g.think.closed = true;
  g.think.el.open = false;
  const secs = Math.max(1, Math.round((Date.now() - g.think.started) / 1000));
  g.think.el.querySelector("summary").textContent = `Thought for ${secs}s`;
}

function finishLive(ev) {
  if (state.live) {
    for (const g of state.live.groups.values()) {
      closeThinking(g);
      g.running = false;
      if (g.steps) updateActivity(g);
      g.body.classList.remove("cursor");
      if (!g.text && !g.chips.children.length) g.el.remove();
    }
  }
  if (ev.status === "error") appendError(ev.error || "run failed");
  if (ev.status === "cancelled") appendError("stopped");
  state.live = null;
  renderHeader();
  scrollDown(false);
}

function upsertVmCard(msg) {
  const id = `vm-${msg.session_id || state.current}-${msg.id}`;
  let el = document.getElementById(id);
  if (!el) {
    el = document.createElement("div");
    el.id = id;
    $("messages").appendChild(el);
  }
  const question = msg.type === "question";
  el.className = `vm-card ${question ? "question" : ""}`;
  const head = `${question ? "❓ Question" : "📣 Message"} from a task in the VM${msg.task ? ` · ${esc(msg.task)}` : ""}`;
  let tail = "";
  if (question && msg.status === "pending") {
    tail = `<form data-mid="${msg.id}"><input placeholder="Your answer" required><button class="btn primary">Answer</button></form>`;
  } else if (question && msg.status === "answered") {
    tail = `<div class="answer">✓ answered: ${esc(msg.answer)}</div>`;
  } else if (question) {
    tail = `<div class="expired">${esc(msg.status)}</div>`;
  }
  el.innerHTML = `<div class="head">${head}</div><div class="text">${esc(msg.text)}</div>${tail}`;
  const form = el.querySelector("form");
  if (form) {
    form.onsubmit = async (e) => {
      e.preventDefault();
      const answer = form.querySelector("input").value;
      const sid = state.current;
      const updated = await guarded(() => api("POST", `/sessions/${sid}/messages/${msg.id}/reply`, { text: answer }));
      if (updated) upsertVmCard(updated);
    };
  }
  scrollDown(false);
}

// ---------------------------------------------------------------- composer

async function send(text) {
  text = text.trim();
  if (!text) return;
  let sid = state.current;
  if (!sid) {
    const s = await guarded(() => api("POST", "/sessions", { kind: "deepagent" }));
    if (!s) return;
    state.sessions.set(s.session_id, s);
    await openSession(s.session_id);
    sid = s.session_id;
  }
  $("input").value = "";
  autoGrow();
  await guarded(() => api("POST", `/sessions/${sid}/query`, { text }));
}

function autoGrow() {
  const t = $("input");
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 220) + "px";
}

// ------------------------------------------------------------ session panel

const TABS = [
  ["overview", "Overview"], ["actions", "Lifecycle"], ["workspace", "Workspace"], ["secrets", "Secrets"],
  ["mcp", "MCP"], ["shell", "Shell"], ["files", "Files"], ["egress", "Egress"], ["debug", "Debug"],
];

function renderTabs() {
  $("tabs").innerHTML = TABS.map(([id, label]) =>
    `<button data-tab="${id}" class="${id === state.tab ? "active" : ""}">${label}</button>`).join("");
}

function kv(obj) {
  return `<div class="kv">${Object.entries(obj).map(([k, v]) =>
    `<div>${esc(k)}</div><div>${esc(typeof v === "object" && v !== null ? JSON.stringify(v) : v)}</div>`).join("")}</div>`;
}

async function renderTab() {
  renderTabs();
  const body = $("tab-body");
  const sid = state.current;
  const s = state.sessions.get(sid);
  if (!s) { body.innerHTML = '<p class="muted">Open a session.</p>'; return; }
  const vm = s.vm || {};
  const tab = state.tab;
  if (tab === "overview") {
    body.innerHTML = kv({
      session: sid, kind: s.kind, state: vm.state, tier: vm.tier, vcpus: vm.vcpus, mem_mib: vm.mem_mib,
      workspace_mib: vm.workspace_mib, cpu_template: vm.cpu_template, egress: vm.egress,
      allowlist: (vm.egress_allowlist || []).join(", ") || "—", snapshot_gen: vm.snapshot_gen,
      forked_from: vm.forked_from || "—", secrets: (vm.secret_keys || []).join(", ") || "—",
      pending_questions: vm.pending_questions, policy: vm.policy,
      last_active: vm.last_active_ts ? new Date(vm.last_active_ts * 1000).toLocaleTimeString() : "—",
    }) + `<div class="btn-row"><button class="btn" data-act="refresh">Refresh</button></div>`;
  } else if (tab === "actions") {
    body.innerHTML = `
      <p class="muted small">State: <b>${esc(vm.state)}</b>. A query to a paused or persisted session wakes it automatically.</p>
      <div class="btn-row">
        <button class="btn" data-act="vm" data-a="pause">Pause</button>
        <button class="btn" data-act="vm" data-a="persist">Persist to disk</button>
        <button class="btn" data-act="vm" data-a="resume">Resume</button>
      </div>
      <h3>Fork</h3><p class="muted small">Clone memory, processes and disks into new sessions.</p>
      <div class="field"><select id="fork-count"><option>1</option><option>2</option><option>3</option></select>
        <button class="btn" data-act="fork">Fork</button></div>
      <h3>Danger zone</h3>
      <div class="btn-row"><button class="btn danger" data-act="delete">Delete session and VM</button></div>
      <pre class="out hidden" id="act-out"></pre>`;
  } else if (tab === "workspace") {
    const ws = await guarded(() => api("GET", `/sessions/${sid}/workspace`));
    if (!ws || state.current !== sid) return;
    body.innerHTML = `
      <p class="muted small">/workspace is its own ${esc(ws.workspace_mib)} MiB drive: checkpoint it, then swap back without a reboot.</p>
      <div class="field"><input id="ckpt-name" placeholder="checkpoint name (a-z0-9_-)"><button class="btn" data-act="ckpt">Checkpoint</button></div>
      <table class="t"><tr><th>Checkpoint</th><th></th></tr>${(ws.checkpoints || []).map((c) =>
        `<tr><td>${esc(c)}</td><td><button class="btn" data-act="swap" data-name="${esc(c)}">Swap in</button></td></tr>`).join("") ||
        '<tr><td class="muted" colspan="2">none</td></tr>'}</table>`;
  } else if (tab === "secrets") {
    body.innerHTML = `
      <p class="muted small">Delivered to the VM through MMDS only; never stored on disk. Current: ${esc((vm.secret_keys || []).join(", ") || "none")}</p>
      <div class="field"><input id="sec-key" placeholder="NAME"><input id="sec-val" placeholder="value (empty = remove)"></div>
      <div class="btn-row"><button class="btn" data-act="secret">Set</button></div>
      <p class="muted small">Read in the VM: <code>curl -s -H "X-metadata-token: $(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-metadata-token-ttl-seconds: 60')" http://169.254.169.254/latest/meta-data/secrets/NAME</code></p>`;
  } else if (tab === "mcp") {
    body.innerHTML = `
      <p class="muted small">Runs a stdio MCP server inside this session's VM (registered as user--session--name).</p>
      <div class="field"><input id="mcp-name" placeholder="name" value="demo"></div>
      <div class="field"><input id="mcp-cmd" placeholder="command" value="/opt/mcp/bin/python"></div>
      <div class="field"><input id="mcp-args" placeholder="args (space separated)" value="/opt/fc/demo_mcp_server.py"></div>
      <div class="btn-row"><button class="btn" data-act="mcp">Register</button></div><pre class="out hidden" id="mcp-out"></pre>`;
  } else if (tab === "shell") {
    body.innerHTML = `
      <p class="muted small">Runs directly in the VM (not through the agent).</p>
      <textarea id="sh-cmd" rows="3" style="width:100%" placeholder="e.g. uname -a; ls -la /workspace">uname -a; ls -la /workspace</textarea>
      <div class="btn-row"><button class="btn primary" data-act="shell">Run</button>
        <button class="btn" data-act="shell-ask">Start an fc-ask task</button></div>
      <pre class="out hidden" id="sh-out"></pre>`;
  } else if (tab === "files") {
    await renderFiles(state.filesPath || "/workspace");
  } else if (tab === "egress") {
    const log = await guarded(() => api("GET", `/sessions/${sid}/egress`));
    if (!log || state.current !== sid) return;
    body.innerHTML = `<p class="muted small">Outbound connections (${vm.egress || "?"} egress). ${log.audit ? "" : "Audit is off."}</p>
      <table class="t"><tr><th>time</th><th>kind</th><th>destination</th><th>ok</th></tr>${(log.entries || []).slice().reverse().map((e) =>
        `<tr><td>${new Date(e.ts * 1000).toLocaleTimeString()}</td><td>${esc(e.kind)}</td><td>${esc(e.host || e.dst)}:${esc(e.dport)}</td>
         <td>${e.allowed ? "✓" : "✗ " + esc(e.reason || "")}</td></tr>`).join("") || '<tr><td colspan="4" class="muted">none</td></tr>'}</table>
      <div class="btn-row"><button class="btn" data-act="refresh-tab">Refresh</button></div>`;
  } else if (tab === "debug") {
    const dbg = await guarded(() => api("GET", `/sessions/${sid}/debug`));
    if (!dbg || state.current !== sid) return;
    body.innerHTML = `<pre class="out">${esc(JSON.stringify(dbg, null, 2))}</pre><div class="btn-row"><button class="btn" data-act="refresh-tab">Refresh</button></div>`;
  }
}

async function renderFiles(path) {
  const sid = state.current;
  const res = await guarded(() => api("GET", `/sessions/${sid}/files?path=${encodeURIComponent(path)}`));
  if (!res || state.current !== sid) return;
  state.filesPath = res.type === "dir" ? res.path : path;
  const parent = res.path.replace(/\/[^/]+\/?$/, "") || "/";
  const body = $("tab-body");
  if (res.type === "dir") {
    body.innerHTML = `<div class="field"><input id="files-path" value="${esc(res.path)}"><button class="btn" data-act="files-go">Go</button></div>
      <table class="t"><tr class="file-row" data-path="${esc(parent)}"><td>..</td><td></td></tr>${res.entries.map((e) =>
        `<tr class="file-row" data-path="${esc((res.path.replace(/\/$/, "")) + "/" + e.name)}"><td>${e.dir ? "📁" : "📄"} ${esc(e.name)}</td>
         <td class="muted">${e.dir ? "" : e.size + " B"}</td></tr>`).join("")}</table>`;
  } else {
    body.innerHTML = `<div class="field"><input id="files-path" value="${esc(res.path)}"><button class="btn" data-act="files-go">Go</button></div>
      <div class="btn-row"><button class="btn" data-act="files-up" data-path="${esc(parent)}">← back</button>
      <span class="muted small">${res.size} bytes${res.truncated ? " (truncated)" : ""}</span></div>
      <pre class="out">${esc(res.text)}</pre>`;
  }
}

async function onTabAction(btn) {
  const sid = state.current;
  const act = btn.dataset.act;
  const out = (id, data) => { const el = $(id); if (el) { el.classList.remove("hidden"); el.textContent = typeof data === "string" ? data : JSON.stringify(data, null, 2); } };
  if (act === "refresh" || act === "refresh-tab") {
    const s = await guarded(() => api("GET", `/sessions/${sid}`));
    if (s) state.sessions.set(sid, { ...state.sessions.get(sid), ...s });
    renderHeader();
    renderTab();
  } else if (act === "vm") {
    btn.disabled = true;
    const r = await guarded(() => api("POST", `/sessions/${sid}/vm/${btn.dataset.a}`, {}), (r) => `${btn.dataset.a}: ${r.vm && r.vm.state} in ${r.took_s}s`);
    btn.disabled = false;
    if (r) { state.sessions.set(sid, { ...state.sessions.get(sid), ...r }); renderHeader(); renderSessions(); renderTab(); }
  } else if (act === "fork") {
    btn.disabled = true;
    const r = await guarded(() => api("POST", `/sessions/${sid}/fork`, { count: Number($("fork-count").value) }),
      (r) => `forked into ${r.sessions.length} session(s) in ${r.took_s}s`);
    btn.disabled = false;
    if (r) { r.sessions.forEach((s) => state.sessions.set(s.session_id, s)); renderSessions(); }
  } else if (act === "delete") {
    if (!confirm("Delete this session and its VM?")) return;
    if (await guarded(() => api("DELETE", `/sessions/${sid}`), "deleted")) {
      state.sessions.delete(sid);
      openSession(null);
      renderSessions();
    }
  } else if (act === "ckpt") {
    await guarded(() => api("POST", `/sessions/${sid}/workspace`, { checkpoint: $("ckpt-name").value.trim() }), "checkpoint created");
    renderTab();
  } else if (act === "swap") {
    await guarded(() => api("POST", `/sessions/${sid}/workspace`, { swap: btn.dataset.name }), `workspace swapped to ${btn.dataset.name}`);
    renderTab();
  } else if (act === "secret") {
    const key = $("sec-key").value.trim();
    const val = $("sec-val").value;
    const r = await guarded(() => api("PUT", `/sessions/${sid}/secrets`, { [key]: val === "" ? null : val }), "secrets updated");
    if (r) { const s = state.sessions.get(sid); if (s.vm) s.vm.secret_keys = r.secret_keys; renderTab(); }
  } else if (act === "mcp") {
    const r = await guarded(() => api("POST", `/sessions/${sid}/mcp`, {
      name: $("mcp-name").value.trim(), command: $("mcp-cmd").value.trim(),
      args: $("mcp-args").value.trim() ? $("mcp-args").value.trim().split(/\s+/) : [],
    }), "MCP server registered");
    if (r) out("mcp-out", r);
  } else if (act === "shell" || act === "shell-ask") {
    const command = act === "shell" ? $("sh-cmd").value :
      "nohup sh -c 'a=$(fc-ask --timeout 900 Which color should the report use?); echo \"$a\" > /workspace/color.txt; fc-notify color saved: $a' >/dev/null 2>&1 & echo started";
    btn.disabled = true;
    const r = await guarded(() => api("POST", `/sessions/${sid}/shell`, { command }));
    btn.disabled = false;
    if (r) out("sh-out", `exit ${r.exit_code} (${r.took_s}s)\n${r.stdout}${r.stderr ? "\n[stderr]\n" + r.stderr : ""}`);
  } else if (act === "files-go") {
    renderFiles($("files-path").value.trim() || "/workspace");
  } else if (act === "files-up") {
    renderFiles(btn.dataset.path);
  }
}

// ---------------------------------------------------------------- operations

async function renderOps() {
  setView("ops");
  state.current = null;
  renderSessions();
  const el = $("ops-view");
  const o = await guarded(() => api("GET", "/ops"));
  if (!o || state.view !== "ops") return;
  state.alerts = o.alerts;
  renderAlertBadge();
  const m = o.metrics;
  const pick = (k) => m[k] ?? 0;
  const created = Object.entries(m).filter(([k]) => k.startsWith("fc_sessions_created_total"))
    .map(([k, v]) => `${k.replace(/.*source="([^"]+)".*/, "$1")}: ${v}`).join(" · ") || "—";
  const stat = (label, value) => `<div class="stat"><div class="muted small">${esc(label)}</div><div class="v">${esc(value)}</div></div>`;
  el.innerHTML = `
    <h2 style="margin-top:0">Operations</h2>
    <p class="muted small">FcSessionManager ${o.manager_ok ? "reachable" : "<b>unreachable</b>"} · queries running in the backend: ${o.running_queries}</p>
    <h3>Alerts</h3>
    ${o.alerts.length ? o.alerts.map((a) => `<div class="alert ${esc(a.severity)}"><b>${esc(a.name)}</b> — ${esc(a.message)}</div>`).join("") : '<p class="muted">No alerts.</p>'}
    <h3>Manager</h3>
    <div class="ops-grid">
      ${stat("VM slots used", `${pick("fc_vm_slots_used")} / ${pick("fc_vm_slots_max")}`)}
      ${stat("Warm pool ready", `${pick("fc_warm_pool_ready")} / ${pick("fc_warm_pool_target")}`)}
      ${stat("Host memory available", `${pick("fc_host_mem_available_mib")} MiB`)}
      ${stat("Guest memory (running)", `${pick("fc_guest_mem_mib_running")} MiB`)}
      ${stat("Create P95", `${pick('fc_session_create_seconds{quantile="0.95"}').toFixed(2)} s`)}
      ${stat("Restore P95", `${pick('fc_session_restore_seconds{quantile="0.95"}').toFixed(2)} s`)}
      ${stat("Persist P95", `${pick('fc_session_persist_seconds{quantile="0.95"}').toFixed(2)} s`)}
      ${stat("Egress events", pick("fc_egress_audit_events"))}
    </div>
    <p class="muted small">Sessions created by source: ${esc(created)}</p>
    <h3>Users</h3>
    <table class="t"><tr><th>user</th><th>tier</th><th>VMs</th><th>states</th><th></th></tr>${o.users.map((u) =>
      `<tr><td>${esc(u.user)}</td><td>${esc(u.tier || "—")}</td><td>${u.sessions}</td><td>${esc(Object.entries(u.states).map(([k, v]) => `${k} ${v}`).join(", ") || "—")}</td>
       <td><button class="btn" data-switch="${esc(u.user)}">Switch</button></td></tr>`).join("")}</table>
    <h3>Manager restart</h3>
    ${o.features.restart_manager ? `<p class="muted small">Graceful restart: running VMs are persisted and wake on the next request.</p>
      <button class="btn danger" id="btn-restart">Restart FcSessionManager</button><span id="restart-out" class="small muted"></span>`
      : '<p class="muted small">Disabled (start the backend with PLAYGROUND_DOCKER=1).</p>'}
    <div class="btn-row"><button class="btn" id="btn-ops-refresh">Refresh</button></div>`;
  const restart = $("btn-restart");
  if (restart) restart.onclick = async () => {
    if (!confirm("Restart the manager now? Running VMs are persisted first.")) return;
    restart.disabled = true;
    $("restart-out").textContent = " restarting…";
    const r = await guarded(() => api("POST", "/ops/restart-manager"), (r) => `manager restarted in ${r.took_s}s`);
    $("restart-out").textContent = r ? ` done in ${r.took_s}s` : " failed";
    restart.disabled = false;
    await loadSessions();
  };
  $("btn-ops-refresh").onclick = renderOps;
  el.querySelectorAll("[data-switch]").forEach((b) => b.onclick = () => switchUser(b.dataset.switch));
}

// ------------------------------------------------------------------ dialogs

function openSettings() {
  $("set-api").value = state.api || CFG.apiBaseUrl || "";
  $("set-token").value = state.token;
  $("set-status").textContent = "";
  if (!$("dlg-settings").open) $("dlg-settings").showModal();
}

async function saveSettings() {
  state.api = $("set-api").value.trim().replace(/\/+$/, "");
  state.token = $("set-token").value.trim();
  store.set("pg.api", state.api);
  store.set("pg.token", state.token);
  await boot();
}

function openNew() {
  $("new-form").reset();
  document.querySelector(".team-only").classList.add("hidden");
  $("dlg-new").showModal();
}

async function createFromDialog() {
  const kind = document.querySelector('input[name="kind"]:checked').value;
  const profile = {};
  const num = (id, key) => { const v = $(id).value; if (v !== "") profile[key] = Number(v); };
  num("new-vcpus", "vcpus"); num("new-mem", "mem_mib"); num("new-ws", "workspace_mib");
  num("new-ttl-pause", "ttl_pause_s"); num("new-ttl-persist", "ttl_persist_s");
  if ($("new-cpu").value) profile.cpu_template = $("new-cpu").value;
  const allow = $("new-allow").value.split(",").map((s) => s.trim()).filter(Boolean);
  if (allow.length) profile.egress_allowlist = allow;
  if (Object.keys(profile).length && profile.vcpus === undefined) profile.vcpus = 2;
  if (Object.keys(profile).length && profile.mem_mib === undefined) profile.mem_mib = kind === "team" ? 1536 : 1024;
  const body = { kind, title: $("new-title").value.trim() || undefined, team_size: Number($("new-team-size").value) };
  if (Object.keys(profile).length) body.profile = profile;
  else if (kind === "team") body.profile = { vcpus: 2, mem_mib: 1536 };
  toast("Creating the session VM…");
  const s = await guarded(() => api("POST", "/sessions", body), (s) => `session ready (${s.vm && s.vm.state})`);
  if (s) { state.sessions.set(s.session_id, s); renderSessions(); openSession(s.session_id); }
}

function openBatch() {
  const list = [...state.sessions.values()];
  $("batch-sessions").innerHTML = list.length ? list.map((s) =>
    `<label><input type="checkbox" value="${esc(s.session_id)}" ${s.running_run_id ? "disabled" : ""}> ${s.kind === "team" ? "👥 " : ""}${esc(s.title)}
     <span class="muted small">${esc(stateOf(s))}${s.running_run_id ? " · running" : ""}</span></label>`).join("")
    : '<p class="muted">Create some sessions first.</p>';
  $("batch-text").value = "";
  $("dlg-batch").showModal();
}

async function sendBatch() {
  const ids = [...$("batch-sessions").querySelectorAll("input:checked")].map((i) => i.value);
  const text = $("batch-text").value.trim();
  if (!ids.length || !text) return;
  const r = await guarded(() => api("POST", "/batch-query", { session_ids: ids, text }));
  if (r) {
    const failed = r.runs.filter((x) => x.error);
    toast(`started ${r.runs.length - failed.length} run(s)${failed.length ? `, ${failed.length} refused: ${failed.map((f) => f.error).join("; ")}` : ""}`,
      failed.length ? "error" : "ok");
  }
}

// --------------------------------------------------------------- user / boot

async function switchUser(user) {
  if (!user) return;
  state.user = user;
  store.set("pg.user", user);
  $("user-menu").classList.add("hidden");
  state.current = null;
  state.unread = {};
  await boot();
}

async function loadSessions() {
  const r = await guarded(() => api("GET", "/sessions"));
  if (!r) return;
  state.sessions = new Map(r.sessions.map((s) => [s.session_id, s]));
  state.external = r.external || [];
  renderSessions();
  renderHeader();
}

async function boot() {
  if (!state.api || !state.token) { openSettings(); return; }
  try {
    const health = await fetch(`${state.api}/healthz`).then((r) => r.json());
    if (health.api_version !== 1) toast(`backend API version ${health.api_version} (frontend expects 1)`, "error");
  } catch (e) {
    toast(`backend ${state.api} unreachable`, "error");
    openSettings();
    return;
  }
  state.me = await guarded(() => api("GET", "/me"));
  if (!state.me) return;
  await loadSessions();
  setView("chat");
  openSession(null);
  connectEvents();
}

function wire() {
  $("btn-settings").onclick = openSettings;
  $("settings-form").onsubmit = (e) => { if (e.submitter && e.submitter.value === "save") saveSettings(); };
  $("btn-new").onclick = openNew;
  document.querySelectorAll('input[name="kind"]').forEach((r) => r.onchange = () =>
    document.querySelector(".team-only").classList.toggle("hidden", r.value !== "team" || !r.checked));
  $("new-form").onsubmit = (e) => { if (e.submitter && e.submitter.value === "create") createFromDialog(); };
  $("btn-batch").onclick = openBatch;
  $("batch-form").onsubmit = (e) => { if (e.submitter && e.submitter.value === "send") sendBatch(); };
  $("btn-ops").onclick = renderOps;
  $("session-list").onclick = (e) => { const item = e.target.closest("[data-sid]"); if (item) openSession(item.dataset.sid); };
  $("external-list").onclick = async (e) => {
    const item = e.target.closest("[data-adopt]");
    if (!item) return;
    const s = await guarded(() => api("POST", "/sessions", { adopt: item.dataset.adopt }), "imported");
    if (s) { await loadSessions(); openSession(s.session_id); }
  };
  $("btn-user").onclick = () => $("user-menu").classList.toggle("hidden");
  $("user-options").onclick = (e) => { const item = e.target.closest("[data-user]"); if (item) switchUser(item.dataset.user); };
  $("user-form").onsubmit = (e) => { e.preventDefault(); switchUser($("user-input").value.trim()); $("user-input").value = ""; };
  $("composer").onsubmit = (e) => { e.preventDefault(); send($("input").value); };
  $("input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send($("input").value); }
  });
  $("input").addEventListener("input", autoGrow);
  $("btn-stop").onclick = () => guarded(() => api("POST", `/sessions/${state.current}/cancel`));
  $("btn-details").onclick = () => { $("details").classList.toggle("hidden"); if (!$("details").classList.contains("hidden")) renderTab(); };
  $("btn-details-close").onclick = () => $("details").classList.add("hidden");
  $("tabs").onclick = (e) => { const b = e.target.closest("[data-tab]"); if (b) { state.tab = b.dataset.tab; renderTab(); } };
  $("tab-body").onclick = (e) => {
    const row = e.target.closest(".file-row");
    if (row) { renderFiles(row.dataset.path); return; }
    const b = e.target.closest("[data-act]");
    if (b) onTabAction(b);
  };
  $("chat-title").onclick = async () => {
    const s = state.sessions.get(state.current);
    const title = s && prompt("Rename session", s.title);
    if (title) await guarded(() => api("PATCH", `/sessions/${state.current}`, { title }));
  };
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".user-box")) $("user-menu").classList.add("hidden");
  });
}

wire();
boot();
