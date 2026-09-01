/* Jarvis web client. No build step, no dependencies, no inline scripts (the API's
 * CSP is script-src 'self'), and it keeps working when the network drops: the
 * service worker caches the shell and every mutation goes to a local-first server
 * that journals the change even when its own peers are unreachable. */

const TOKEN_KEY = "jarvis.token";
const DEVICE_KEY = "jarvis.device";
const API = "";

/* ------------------------------------------------------------------ plumbing */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  token: localStorage.getItem(TOKEN_KEY) || "",
  device: localStorage.getItem(DEVICE_KEY) || browserDeviceId(),
  me: null,
  health: null,
  view: "chat",
  pendingAssistant: false,
  lastAnswerAt: 0,
  recorder: null,
};

function browserDeviceId() {
  let id = "";
  try { id = localStorage.getItem(DEVICE_KEY) || ""; } catch { /* private mode */ }
  if (!id) {
    id = "web-" + (crypto.randomUUID ? crypto.randomUUID().slice(0, 8) : Date.now().toString(36));
    try { localStorage.setItem(DEVICE_KEY, id); } catch { /* ignore */ }
  }
  return id;
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body && !(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
  if (state.token) headers["Authorization"] = "Bearer " + state.token;
  let response;
  try {
    response = await fetch(API + path, {
      ...options,
      headers,
      body: options.body && !(options.body instanceof FormData) ? JSON.stringify(options.body) : options.body,
      cache: "no-store",
    });
  } catch (networkError) {
    throw new Error("offline: cannot reach the local Jarvis server");
  }
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
  if (!response.ok) {
    const detail = data && data.detail;
    const message = typeof detail === "string" ? detail : detail && detail.message ? detail.message : response.statusText;
    const error = new Error(message);
    error.status = response.status;
    error.payload = detail;
    throw error;
  }
  return data;
}

function toast(message, kind = "info") {
  const banner = $("#offline-banner");
  banner.hidden = false;
  banner.textContent = message;
  banner.className = "banner " + (kind === "error" ? "danger" : kind === "warn" ? "warn" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { banner.hidden = true; }, 5200);
}

const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const el = (tag, className, html) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (html !== undefined) node.innerHTML = html;
  return node;
};

/* ---------------------------------------------------------------- audio capture */
async function recordClip(seconds = 3.0, onProgress = null) {
  const AudioCtor = window.AudioContext || window.webkitAudioContext;
  if (!AudioCtor) throw new Error("this browser cannot capture audio");
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }, video: false });
  } catch (err) {
    throw new Error("microphone blocked — " + err.name + ". Grant mic access, or use the synthesised demo.");
  }
  // 16 kHz capture means the server needs no resampler and no decoder for browser codecs
  const ctx = new AudioCtor({ sampleRate: 16000 });
  const source = ctx.createMediaStreamSource(stream);
  const processor = ctx.createScriptProcessor(4096, 1, 1);
  const chunks = [];
  let collected = 0;
  const target = Math.floor(16000 * seconds);
  await new Promise((resolve) => {
    processor.onaudioprocess = (event) => {
      const input = event.inputBuffer.getChannelData(0);
      const copy = new Float32Array(input.length);
      copy.set(input);
      chunks.push(copy);
      collected += input.length;
      if (onProgress) onProgress(Math.min(1, collected / target));
      if (collected >= target) resolve();
    };
    source.connect(processor);
    processor.connect(ctx.destination);
    setTimeout(resolve, (seconds + 1.2) * 1000);
  });
  processor.disconnect(); source.disconnect();
  stream.getTracks().forEach((track) => track.stop());
  await ctx.close();
  const merged = new Float32Array(collected);
  let offset = 0;
  for (const chunk of chunks) { merged.set(chunk, offset); offset += chunk.length; }
  let peak = 0;
  for (let i = 0; i < merged.length; i++) peak = Math.max(peak, Math.abs(merged[i]));
  const scale = peak > 0.001 ? Math.min(1, 0.9 / peak) : 1;   // normalise so gain does not change the print
  return pcmToWav(merged, scale);
}

function pcmToWav(float32, scale) {
  const length = float32.length;
  const buffer = new ArrayBuffer(44 + length * 2);
  const view = new DataView(buffer);
  const ascii = (at, text) => { for (let i = 0; i < text.length; i++) view.setUint8(at + i, text.charCodeAt(i)); };
  ascii(0, "RIFF"); view.setUint32(4, 36 + length * 2, true); ascii(8, "WAVE");
  ascii(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, 16000, true); view.setUint32(28, 32000, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  ascii(36, "data"); view.setUint32(40, length * 2, true);
  for (let i = 0; i < length; i++) {
    const sample = Math.max(-1, Math.min(1, float32[i] * scale));
    view.setInt16(44 + i * 2, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
  }
  let binary = "";
  const bytes = new Uint8Array(buffer);
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

const b64FromBytes = (bytes) => {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(binary);
};

/* --------------------------------------------------------------------- views */
function show(view) {
  state.view = view;
  $$(".tab").forEach((tab) => tab.classList.toggle("is-active", tab.dataset.view === view));
  $$(".view").forEach((section) => { section.hidden = section.id !== "view-" + view; });
  const signedIn = Boolean(state.me);
  $("#view-signin").hidden = signedIn;
  $("#view-chat").hidden = !signedIn || view !== "chat";
  if (!signedIn) return;
  const loaders = { profile: loadProfile, voice: loadVoice, control: loadControl, media: loadGallery, memory: loadMemory, sync: loadSync, chat: loadMe };
  (loaders[view] || (() => {}))();
}

/* -------------------------------------------------------------------- sign in */
async function beginSignIn() {
  const form = $("#signin-form");
  const data = Object.fromEntries(new FormData(form).entries());
  const button = $("button.primary", form);
  button.disabled = true;
  try {
    const result = await api("/api/auth/dev-login", {
      method: "POST",
      body: { ...data, display_name: data.display_name || undefined, device_id: state.device, device_name: navigator.platform || "browser", platform: "web" },
    });
    state.token = result.session.token;
    localStorage.setItem(TOKEN_KEY, state.token);
    state.me = result.user;
    toast("Signed in. Everything is stored on this device only.");
    await afterSignIn();
  } catch (err) {
    toast(err.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function beginOidc(provider) {
  try {
    const result = await api("/api/auth/login/begin", { method: "POST", body: { provider, device_id: state.device } });
    if (result.mode === "dev-idp") {
      $("#dev-note").textContent = result.note;
      toast("No client id for " + provider + " — using the local dev sign-in instead.");
      return;
    }
    window.location.href = result.authorize_url;
  } catch (err) {
    toast(err.message, "error");
  }
}

async function afterSignIn() {
  $("#signout").hidden = false;
  await loadMe();
  show("chat");
}

async function signOut() {
  try { await api("/api/auth/logout", { method: "POST" }); } catch { /* already gone */ }
  localStorage.removeItem(TOKEN_KEY);
  state.token = "";
  state.me = null;
  location.reload();
}

/* ----------------------------------------------------------------------- me */
async function loadMe() {
  try {
    const me = await api("/api/me");
    state.me = me.user;
    $("#who").textContent = (me.user && me.user.display_name) || "signed in";
    renderSideStatus(me);
    renderQuickActions();
    $("#engine-badge").textContent = state.health && state.health.credentials.llm ? "model engine" : "local engine";
  } catch (err) {
    if (err.status === 401) { state.token = ""; localStorage.removeItem(TOKEN_KEY); show("chat"); }
  }
}

function renderSideStatus(me) {
  const vp = (me.user && me.user.voiceprint) || {};
  const rows = [
    ["scope", me.session.scope],
    ["device", state.device],
    ["voiceprint", vp.enrolled ? vp.samples + " samples" : "not enrolled"],
    ["pending ops", me.pending_ops],
    ["interests", (me.profile && me.profile.interests ? me.profile.interests.length : 0)],
  ];
  $("#side-status").innerHTML = rows.map(([k, v]) => `<div><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("");
}

function renderQuickActions() {
  const actions = [
    "brainstorm names for a weekend project", "draft an email declining a meeting", "should I take the longer commute?",
    "plan my week", "what do you remember about my flights?", "remember that I like aisle seats",
    "generate a poster of mountains at sunset", "sync my devices",
  ];
  const box = $("#quick-actions");
  box.innerHTML = "";
  actions.forEach((text) => {
    const chip = el("button", "chip", esc(text));
    chip.type = "button";
    chip.addEventListener("click", () => { $("#input").value = text; $("#composer").requestSubmit(); });
    box.appendChild(chip);
  });
}

/* --------------------------------------------------------------------- chat */
function bubble(role, html) {
  const msg = el("div", "msg " + role);
  msg.appendChild(el("div", "bubble", html));
  $("#thread").appendChild(msg);
  $("#thread").scrollTop = $("#thread").scrollHeight;
  return msg;
}

async function sendTurn(text) {
  if (state.pendingAssistant) return;
  state.pendingAssistant = true;
  $("#send").disabled = true;
  bubble("user", esc(text));
  const thinking = bubble("bot", '<span class="muted">thinking…</span>');
  // time the previous answer: reading duration is a real (if weak) verbosity signal
  const engagement = state.lastAnswerAt ? Math.min(3600, (Date.now() - state.lastAnswerAt) / 1000) : null;
  try {
    const reply = await api("/api/assistant", { method: "POST", body: { text, engagement_seconds: engagement } });
    thinking.querySelector(".bubble").innerHTML = formatReply(reply);
    const meta = el("div", "meta");
    const edits = (reply.meta && reply.meta.style_edits || []).join(", ") || "no style changes";
    meta.innerHTML = `<span class="pill">${esc(reply.engine)}</span><span>intent: ${esc(reply.intent)}</span><span>style: ${esc(edits)}</span>`;
    const actions = el("div", "actions");
    [["👍 useful", 1], ["👎 not that", -1]].forEach(([label, value]) => {
      const b = el("button", "ghost", label);
      b.type = "button";
      b.addEventListener("click", async () => {
        const result = await api("/api/assistant/feedback", { method: "POST", body: { valence: value } });
        meta.appendChild(el("span", "muted", `learned: ${esc(JSON.stringify(result.adjusted || {}))}`));
      });
      actions.appendChild(b);
    });
    meta.appendChild(actions);
    thinking.appendChild(meta);
    if (reply.actions && reply.actions.length) {
      const row = el("div", "chips");
      reply.actions.slice(0, 3).forEach((action) => {
        if (action.kind !== "navigate") return;
        const b = el("button", "chip", action.label || "Open");
        b.type = "button";
        b.addEventListener("click", () => show(action.view));
        row.appendChild(b);
      });
      if (row.children.length) thinking.querySelector(".bubble").appendChild(row);
    }
    if (reply.cards && reply.cards.length) {
      const cards = el("div", "cards");
      reply.cards.slice(0, 6).forEach((card) => {
        cards.appendChild(el("div", "mini-card", `<div class="tag">${esc(card.kind)}${card.angle ? " · " + esc(card.angle) : ""}</div>${esc(card.body || card.title || "")}`));
      });
      thinking.querySelector(".bubble").appendChild(cards);
    }
    if (reply.follow_ups && reply.follow_ups.length) {
      const follow = el("div", "chips");
      reply.follow_ups.forEach((text2) => {
        const chip = el("button", "chip", esc(text2));
        chip.type = "button";
        chip.addEventListener("click", () => { $("#input").value = text2; $("#composer").requestSubmit(); });
        follow.appendChild(chip);
      });
      thinking.appendChild(follow);
    }
    state.lastAnswerAt = Date.now();
    loadProfile();
  } catch (err) {
    thinking.querySelector(".bubble").innerHTML = `<span style="color:var(--danger)">${esc(err.message)}</span>`;
  } finally {
    state.pendingAssistant = false;
    $("#send").disabled = false;
    $("#thread").scrollTop = $("#thread").scrollHeight;
  }
}

function formatReply(reply) {
  let text = esc(reply.text);
  text = text.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/^• (.+)$/gm, "• $1");
  return text;
}

/* ------------------------------------------------------------------ profile */
async function loadProfile() {
  if (!state.token) return;
  try {
    const prefs = await api("/api/preferences");
    const box = $("#traits");
    box.innerHTML = "";
    prefs.traits.forEach((t) => {
      const row = el("div", "trait");
      row.innerHTML = `<div class="name"><b>${esc(t.aspect)}</b><span class="ends">${esc(t.low)} ↔ ${esc(t.high)}</span></div>
        <div><input type="range" min="-1" max="1" step="0.05" value="${Number(t.raw).toFixed(2)}" data-key="${esc(t.key)}">
        <div class="bar"><i></i></div></div>
        <div class="val">${Number(t.effective).toFixed(2)} · ${esc(t.label)}<br><span class="muted">${t.hits} obs</span></div>`;
      const slider = $("input", row);
      const fill = $(".bar i", row);
      const paint = () => { fill.style.width = `${(Number(slider.value) + 1) * 50}%`; };
      paint();
      slider.addEventListener("change", async () => {
        paint();
        const result = await api("/api/preferences/trait", { method: "POST", body: { key: t.key, value: Number(slider.value) } });
        $("#directive").textContent = result.style_directive;
        toast(`${t.aspect}: set — I will use it from now on`);
      });
      box.appendChild(row);
    });
    $("#directive").textContent = prefs.directives.map((d) => "• " + d).join("\n");
    $("#obs-count").textContent = `${prefs.observations} observations · ${prefs.feedback_count} explicit ratings`;
    const interests = $("#interests");
    interests.innerHTML = "";
    (prefs.interests || []).slice(0, 14).forEach((i) => interests.appendChild(el("span", "chip static", `${esc(i.topic)} <span class="score">${i.score.toFixed(2)}</span>`)));
  } catch (err) { /* offline or signed out */ }
}

/* -------------------------------------------------------------------- voice */
async function loadVoice() {
  try {
    const status = await api("/api/voice/status");
    const box = $("#voice-status");
    const rows = [
      ["enrolled", status.enrolled ? `${status.samples} samples` : "no"],
      ["accept threshold", status.threshold ?? "—"],
      ["provider", status.provider ?? "—"],
      ["calibrated on real speech", status.calibrated ? "yes" : "no"],
      ["other speakers known", status.other_speakers_enrolled],
      ["model hash", status.fingerprint ?? "—"],
    ];
    box.innerHTML = rows.map(([k, v]) => `<div class="kv"><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join("")
      + (status.enrolled && !status.calibrated
        ? `<p class="note">Scores are computed, but <b>privileged actions stay locked</b> until the threshold is calibrated against real speech from this microphone.</p>` : "");
    const list = $("#cmd-list");
    list.innerHTML = "";
    const commands = await api("/api/commands");
    (commands.templates || []).forEach((phrase) => {
      const takes = (commands.takes || {})[phrase];
      const chip = el("span", "chip static");
      chip.append(`${esc(phrase)}${takes ? ` · ${takes} take${takes > 1 ? "s" : ""}` : ""}`);
      // an enrolled recording must be deletable from here, not only with curl
      const forget = el("button", "chip-x", "×");
      forget.type = "button";
      forget.title = `forget "${phrase}" and every recording of it`;
      forget.addEventListener("click", async () => {
        forget.disabled = true;
        try {
          await api(`/api/commands/${encodeURIComponent(phrase)}`, { method: "DELETE" });
          logTo("#cmd-log", `forgot "${phrase}" — its takes are gone from this device`, "ok");
        } catch (err) {
          logTo("#cmd-log", err.message, "bad");
        }
        loadVoice();
      });
      chip.appendChild(forget);
      list.appendChild(chip);
    });
    if (commands.calibrated === false && (commands.templates || []).length) {
      logTo("#cmd-log", "not calibrated yet — record a second take of a phrase before trusting a threshold", "warn");
    }
    if (commands.threshold) logTo("#cmd-log", `threshold ${commands.threshold}${commands.calibrated_threshold_suggestion ? " · suggested " + commands.calibrated_threshold_suggestion : ""}`, "warn");
  } catch (err) { logTo("#voice-log", err.message, "bad"); }
}

function logTo(selector, message, kind = "") {
  const box = $(selector);
  if (!box) return;
  box.appendChild(el("div", kind, `${new Date().toLocaleTimeString()} · ${esc(message)}`));
  box.scrollTop = box.scrollHeight;
}

async function withRecording(button, seconds, runner) {
  const original = button.textContent;
  button.classList.add("is-recording");
  button.textContent = "Listening… speak now";
  button.disabled = true;
  try {
    const audio = await recordClip(seconds, (progress) => { button.textContent = `Listening… ${Math.round(progress * 100)}%`; });
    await runner(audio);
  } catch (err) {
    toast(err.message, "error");
    logTo("#voice-log", err.message, "bad");
  } finally {
    button.classList.remove("is-recording");
    button.textContent = original;
    button.disabled = false;
  }
}

/* ------------------------------------------------------------------ control */
async function loadControl() {
  try {
    const [catalog, targets, audit, devices] = await Promise.all([
      api("/api/control/catalog"),
      api("/api/devices"),
      api("/api/control/audit?limit=12"),
      api("/api/devices"),
    ]);
    const targetBox = $("#targets");
    targetBox.innerHTML = "";
    (catalog.targets || []).forEach((t) => {
      const item = el("div", "item");
      item.innerHTML = `<div class="grow"><div class="title">${esc(t.name)}</div>
        <div class="sub">${esc(t.kind)} · ${esc(t.endpoint)} · ${t.capabilities.length} capabilities
        ${t.pairing_verified ? '<span class="pill ok">paired</span>' : '<span class="pill warn">not paired</span>'}</div></div>
        <button class="ghost danger" data-remove="${esc(t.id)}">remove</button>`;
      item.querySelector("[data-remove]").addEventListener("click", async () => {
        await api("/api/control/targets/" + t.id, { method: "DELETE" });
        loadControl();
      });
      targetBox.appendChild(item);
    });
    const capBox = $("#catalog");
    capBox.innerHTML = "";
    (catalog.capabilities || []).forEach((c) => {
      const allowed = (catalog.rules || []).some((r) => r.capability === c.capability && r.allow);
      const item = el("div", "item");
      item.innerHTML = `<div class="grow"><div class="title mono">${esc(c.capability)}</div>
        <div class="sub">${esc(c.description)}${c.irreversible ? " · <b>irreversible</b>" : ""}</div></div>
        <span class="pill ${c.risk === "forbidden" ? "danger" : c.risk === "high" ? "warn" : "ok"}">${esc(c.risk)}</span>
        <button class="ghost" data-allow="${esc(c.capability)}">${allowed ? "allowed ✓" : "allow"}</button>`;
      item.querySelector("[data-allow]").addEventListener("click", async () => {
        try {
          await api("/api/control/rules", { method: "POST", body: { capability: c.capability, allow: true, config: { max_per_hour: 12 } } });
          toast(`Allowed ${c.capability} (max 12/hour). Risky actions still need voice step-up.`);
          loadControl();
        } catch (err) { toast(err.message, "error"); }
      });
      capBox.appendChild(item);
    });
    const select = $("#exec-capability");
    select.innerHTML = (catalog.capabilities || []).map((c) => `<option value="${esc(c.capability)}">${esc(c.capability)} · ${esc(c.risk)}</option>`).join("");
    const targetSelect = $("#exec-target");
    targetSelect.innerHTML = (catalog.targets || []).map((t) => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("");
    if (catalog.paused) { $("#pause-control").textContent = "Resume device control"; $("#pause-control").classList.remove("danger"); }
    else { $("#pause-control").textContent = "Pause all device control"; $("#pause-control").classList.add("danger"); }
    const auditBox = $("#audit");
    auditBox.innerHTML = "";
    (audit.entries || []).forEach((e) => {
      const kind = e.decision === "executed" ? "ok" : e.decision === "denied" ? "danger" : "warn";
      auditBox.appendChild(el("div", "item",
        `<span class="pill ${kind}">${esc(e.decision)}</span>
         <div class="grow"><div class="title mono">${esc(e.action)} ${esc(JSON.stringify(e.args))}</div>
         <div class="sub">${esc(e.reason || "")}${e.target ? " · target " + esc(e.target) : ""}${e.duration_ms != null ? " · " + e.duration_ms + " ms" : ""}</div></div>
         <span class="muted small">${new Date(e.created_at * 1000).toLocaleTimeString()}</span>`));
    });
    if (!(audit.entries || []).length) auditBox.appendChild(el("div", "muted small", "no control activity yet"));
  } catch (err) { toast(err.message, "error"); }
}

/* -------------------------------------------------------------------- media */
async function loadGallery() {
  try {
    const gallery = await api("/api/artifacts?limit=24");
    const box = $("#gallery");
    box.innerHTML = "";
    (gallery.items || []).forEach((item) => {
      const art = el("div", "art");
      const img = document.createElement("img");
      img.loading = "lazy";
      img.alt = item.prompt.slice(0, 90);
      img.src = item.url + "?token=" + encodeURIComponent(state.token);
      art.appendChild(img);
      art.appendChild(el("div", "cap", `<span>${esc(item.kind)} · ${esc(item.provider)}</span><span>${item.width}×${item.height}</span>`));
      box.appendChild(art);
    });
    if (!(gallery.items || []).length) box.appendChild(el("div", "muted small", "nothing generated yet"));
    const note = $("#gen-note");
    if (state.health) note.textContent = state.health.credentials.image || state.health.credentials.video
      ? "A remote model is configured; failures fall back to the offline renderer."
      : "Offline renderer: real PNG and animated GIF files, produced on this machine.";
  } catch (err) { /* ignore */ }
}

/* ------------------------------------------------------------------- memory */
async function loadMemory(query = "") {
  try {
    const data = query ? await api("/api/memory/search?q=" + encodeURIComponent(query) + "&limit=12") : await api("/api/memory?limit=40");
    const items = query ? data.hits : data.items;
    const box = $("#mem-list");
    box.innerHTML = "";
    (items || []).forEach((m) => {
      const item = el("div", "item");
      item.innerHTML = `<div class="grow"><div class="title">${esc(m.body)}</div>
        <div class="sub">${esc(m.source)} · ${m.age_days}d${m.score ? " · score " + m.score.toFixed(3) : ""}${(m.tags || []).length ? " · " + m.tags.map(esc).join(", ") : ""}</div></div>
        <button class="ghost danger" data-forget="${esc(m.id)}">forget</button>`;
      item.querySelector("[data-forget]").addEventListener("click", async () => {
        await api("/api/memory/" + m.id, { method: "DELETE" });
        loadMemory(query);
      });
      box.appendChild(item);
    });
    if (!(items || []).length) box.appendChild(el("div", "muted small", query ? "no matches" : "nothing remembered yet"));
    const tasks = await api("/api/tasks?status=open");
    const taskBox = $("#tasks");
    taskBox.innerHTML = "";
    (tasks.items || []).forEach((t) => {
      const row = el("div", "item");
      row.innerHTML = `<div class="grow">${esc(t.description)}</div><button class="ghost" data-done="${esc(t.id)}">done</button>`;
      row.querySelector("[data-done]").addEventListener("click", async () => { await api(`/api/tasks/${t.id}/complete`, { method: "POST" }); loadMemory(query); });
      taskBox.appendChild(row);
    });
    if (!(tasks.items || []).length) taskBox.appendChild(el("div", "muted small", "no open tasks"));
  } catch (err) { /* ignore */ }
}

/* --------------------------------------------------------------------- sync */
async function loadSync() {
  try {
    const status = await api("/api/sync/status");
    const rows = [
      ["device", status.device_id],
      ["pending ops", status.pending_ops],
      ["cursor", status.cursor],
      ["oplog size", status.oplog_size],
      ["last pull", status.last_pull_at ? new Date(status.last_pull_at * 1000).toLocaleTimeString() : "never"],
      ["mode", status.mode],
    ];
    $("#sync-status").innerHTML = rows.map(([k, v]) => `<div class="kv"><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join("");
    const devices = await api("/api/devices");
    const box = $("#device-list");
    box.innerHTML = "";
    (devices.devices || []).forEach((d) => {
      box.appendChild(el("div", "item",
        `<div class="grow"><div class="title">${esc(d.name)} ${d.id === status.device_id ? '<span class="pill">this device</span>' : ""}</div>
         <div class="sub mono">${esc(d.id)} · ${esc(d.platform)} · ${d.sessions} active session(s)</div></div>
         <span class="pill ${d.trust_level === "trusted" ? "ok" : d.trust_level === "revoked" ? "danger" : "warn"}">${esc(d.trust_level)}</span>
         ${d.trust_level !== "trusted" ? `<button class="ghost" data-trust="${esc(d.id)}">trust</button>` : ""}
         ${d.trust_level !== "revoked" ? `<button class="ghost danger" data-revoke="${esc(d.id)}">revoke</button>` : ""}`));
    });
    $$("[data-trust]", box).forEach((b) => b.addEventListener("click", async () => {
      try { await api(`/api/devices/${b.dataset.trust}/trust`, { method: "POST" }); loadSync(); }
      catch (err) { toast(err.status === 403 ? "Trust changes need a step-up verified session — use the Voice tab first." : err.message, "error"); }
    }));
    $$("[data-revoke]", box).forEach((b) => b.addEventListener("click", async () => { await api(`/api/devices/${b.dataset.revoke}/revoke`, { method: "POST" }); loadSync(); }));
    const capabilities = $("#capability-table");
    capabilities.innerHTML = "";
    Object.entries(state.health?.capabilities || {}).forEach(([key, value]) => {
      capabilities.appendChild(el("div", "item", `<div class="grow"><div class="title">${esc(key.replace(/_/g, " "))}</div><div class="sub">${esc(value.label)}</div></div>
        <span class="pill ${value.offline ? "ok" : "warn"}">${value.offline ? "offline-capable" : "needs network"}</span>`));
    });
  } catch (err) { toast(err.message, "error"); }
}

/* ----------------------------------------------------------------- health */
async function refreshHealth() {
  try {
    const health = await api("/api/health");
    state.health = health;
    const online = health.network.online;
    const conn = $("#conn");
    conn.textContent = online ? `online · ${health.network.latency_ms ?? "?"} ms` : "offline";
    conn.className = "pill " + (online ? "ok" : "warn");
    if (!window.navigator.onLine) { conn.textContent = "offline (no network)"; }
    return health;
  } catch (err) {
    $("#conn").textContent = "server unreachable";
    $("#conn").className = "pill danger";
    return null;
  }
}

/* ---------------------------------------------------------------- boot */
async function boot() {
  if ("serviceWorker" in navigator) {
    try { await navigator.serviceWorker.register("/sw.js", { scope: "/" }); } catch { /* dev server may be http */ }
  }
  $$(".tab").forEach((tab) => tab.addEventListener("click", () => show(tab.dataset.view)));
  $("#signin-form").addEventListener("submit", (event) => { event.preventDefault(); beginSignIn(); });
  $$("[data-oidc]").forEach((b) => b.addEventListener("click", () => beginOidc(b.dataset.oidc)));
  $("#signout").addEventListener("click", signOut);

  $("#composer").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = $("#input");
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    sendTurn(text);
  });
  $("#input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("#composer").requestSubmit(); }
  });
  $("#mic-btn").addEventListener("click", () => withRecording($("#mic-btn"), 4, async (audio) => {
    const result = await api("/api/asr/transcribe", { method: "POST", body: { audio_b64: audio } });
    const box = $("#dictation");
    box.hidden = false;
    if (result.available && result.text) { $("#input").value = result.text; box.textContent = ""; }
    else {
      box.textContent = result.error || "no speech detected";
      logTo("#cmd-log", `ASR engine: ${result.engine}`, "warn");
    }
  }));

  $("#reset-traits").addEventListener("click", async () => { await api("/api/preferences/reset", { method: "POST" }); loadProfile(); toast("Learned traits reset — I start fresh."); });

  $("#enroll-btn").addEventListener("click", () => withRecording($("#enroll-btn"), 3.2, async (audio) => {
    const result = await api("/api/voice/enroll", { method: "POST", body: { audio_b64: audio } });
    logTo("#voice-log", `enrolled: ${result.enrolled_samples} samples · threshold ${result.threshold} · snr ${result.quality.snr_db} dB`, result.needs_more_samples ? "warn" : "ok");
    loadVoice();
  }));
  $("#verify-btn").addEventListener("click", () => withRecording($("#verify-btn"), 2.6, async (audio) => {
    const result = await api("/api/voice/verify", { method: "POST", body: { audio_b64: audio } });
    logTo("#voice-log", `${result.accepted ? "matched" : "rejected"} · sim ${result.similarity} vs thr ${result.threshold}${result.blocked_reason ? " · " + result.blocked_reason : ""}`, result.accepted ? "ok" : "bad");
    (result.notes || []).forEach((n) => logTo("#voice-log", n, "warn"));
    if (result.accepted) toast("Step-up granted for 10 minutes — risky device actions are unlocked.");
  }));
  $("#reset-voice").addEventListener("click", async () => { await api("/api/voice/reset", { method: "POST" }); loadVoice(); toast("Voiceprint deleted."); });
  $("#demo-enroll").addEventListener("click", async () => {
    const f0 = Number($("#demo-f0").value || 118);
    for (let i = 0; i < 3; i++) {
      const audio = await synthClip("sample " + i + " phrase for enrollment", f0);
      const r = await api("/api/voice/enroll", { method: "POST", body: { audio_b64: audio } });
      logTo("#voice-log", `synth sample ${i + 1}: threshold ${r.threshold}`, "ok");
    }
    loadVoice();
  });
  $("#demo-verify").addEventListener("click", async () => {
    const f0 = Number($("#demo-f0").value || 118);
    const audio = await synthClip("the same words said by the enrolled owner", f0);
    const r = await api("/api/voice/verify", { method: "POST", body: { audio_b64: audio } });
    logTo("#voice-log", `owner check: sim ${r.similarity} thr ${r.threshold} → ${r.accepted ? "granted" : "blocked (" + (r.blocked_reason || "score") + ")"}`, r.accepted ? "ok" : "warn");
  });
  $("#demo-impostor").addEventListener("click", async () => {
    const audio = await synthClip("someone else saying the words entirely", 205);
    const r = await api("/api/voice/verify", { method: "POST", body: { audio_b64: audio } });
    logTo("#voice-log", `stranger check: sim ${r.similarity} thr ${r.threshold} → ${r.accepted ? "ACCEPTED (bad)" : "rejected"}`, r.accepted ? "bad" : "ok");
  });

  $("#cmd-record").addEventListener("click", () => {
    const phrase = $("#cmd-phrase").value.trim();
    if (!phrase) { toast("Type the phrase first", "warn"); return; }
    withRecording($("#cmd-record"), 2.6, async (audio) => {
      const r = await api("/api/commands/enroll", { method: "POST", body: { phrase, audio_b64: audio } });
      logTo("#cmd-log", `enrolled "${phrase}" · ${r.takes} take(s) · suggested threshold ${r.suggested_threshold ?? "n/a"}`, "ok");
      loadVoice();
    });
  });
  $("#cmd-test").addEventListener("click", () => withRecording($("#cmd-test"), 2.6, async (audio) => {
    const r = await api("/api/commands/recognize", { method: "POST", body: { audio_b64: audio } });
    logTo("#cmd-log", r.matched ? `heard "${r.matched}" (distance ${r.distance})` : `no match · ${r.reason}`, r.matched ? "ok" : "warn");
    if (r.executed) logTo("#cmd-log", "action: " + JSON.stringify(r.executed).slice(0, 160), "ok");
  }));
  $("#cmd-calibrate").addEventListener("click", async () => {
    const r = await api("/api/commands/auto-calibrate", { method: "POST" });
    logTo("#cmd-log", `threshold ${r.threshold} — ${r.note}`, "ok");
  });

  $("#target-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("/api/control/targets", {
        method: "POST",
        body: { name: $("#target-name").value, kind: $("#target-kind").value, endpoint: "local", pairing_verified: $("#target-paired").checked },
      });
      $("#target-name").value = "";
      loadControl();
    } catch (err) { toast(err.status === 403 ? "Adding a controllable device needs a step-up verified session. Use the Voice tab." : err.message, "error"); }
  });
  $("#exec-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    let args = {};
    const raw = $("#exec-args").value.trim();
    if (raw) { try { args = JSON.parse(raw); } catch { toast("Arguments must be JSON, e.g. {\"app\":\"nano\"}", "error"); return; } }
    try {
      const result = await api("/api/control/execute", {
        method: "POST",
        body: { capability: $("#exec-capability").value, args, device_id: $("#exec-target").value || null, dry_run: $("#exec-dry").checked, confirmation: $("#exec-confirm").value || null },
      });
      $("#exec-result").innerHTML = `<div class="${result.status === "executed" ? "ok" : result.status === "denied" ? "bad" : "warn"}">${esc(result.status)}${result.reason ? " — " + esc(result.reason) : ""}</div>`
        + (result.confirmation_token ? `<div class="warn">confirmation token issued: <span class="mono">${esc(result.confirmation_token)}</span> — re-run with it to proceed</div>` : "")
        + `<div>${esc(JSON.stringify(result.result || result.plan || result.message || "", null, 1)).slice(0, 900)}</div>`;
      if (result.confirmation_token) {
        const input = $("#exec-confirm");
        input.value = result.confirmation_token;
        $("#exec-dry").checked = false;
        toast("Irreversible action held for confirmation; token is valid for 5 minutes and one use.");
      }
      loadControl();
    } catch (err) { logTo("#exec-result", err.message, "bad"); }
  });
  $("#pause-control").addEventListener("click", async () => {
    const paused = $("#pause-control").textContent.startsWith("Pause");
    await api("/api/control/pause", { method: "POST", body: { paused } });
    toast(paused ? "All device control paused and synced to every device." : "Device control resumed.");
    loadControl();
  });

  $("#gen-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const kind = $("#gen-kind").value;
    const button = $("button.primary", event.target);
    button.disabled = true; button.textContent = "Rendering…";
    try {
      const body = { prompt: $("#gen-prompt").value, style: $("#gen-style").value, force_offline: false };
      if (kind === "image") { body.width = Number($("#gen-width").value); body.height = Number($("#gen-height").value); }
      else { body.width = Math.min(480, Number($("#gen-width").value)); body.height = Math.min(320, Number($("#gen-height").value)); body.seconds = 2.5; body.fps = 8; }
      const result = await api(`/api/media/${kind}`, { method: "POST", body });
      if (result.status !== "ready") throw new Error(result.error || "generation failed");
      toast(`${kind} ready via ${result.provider} (${Math.round(result.bytes / 1024)} KB)`);
      loadGallery();
    } catch (err) { toast(err.message, "error"); } finally { button.disabled = false; button.textContent = "Make it"; }
  });

  $("#mem-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = $("#mem-body").value.trim();
    if (!body) return;
    await api("/api/assistant", { method: "POST", body: { text: "remember that " + body } });
    $("#mem-body").value = "";
    loadMemory();
  });
  $("#mem-search").addEventListener("click", () => loadMemory($("#mem-query").value.trim()));
  $("#mem-query").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); loadMemory($("#mem-query").value.trim()); } });
  $("#task-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    await api("/api/tasks", { method: "POST", body: { description: $("#task-body").value } });
    $("#task-body").value = "";
    loadMemory();
  });

  $("#sync-now").addEventListener("click", async () => {
    const result = await api("/api/sync/run", { method: "POST" });
    toast(`sync: pushed ${result.pushed} · pulled ${result.pulled} · applied ${result.applied} · conflicts ${result.conflicts}${result.online ? "" : " (offline — queued)"}`);
    loadSync();
  });
  $("#add-device").addEventListener("click", async () => {
    const id = prompt("Device id to pair (the other device shows its own id in Settings):", "phone-1");
    if (!id) return;
    const result = await api("/api/devices/pair", { method: "POST", body: { device_id: id, device_name: "New device" } });
    toast("Pairing code for the new device: " + result.code + " — approve it from a trusted device.");
    loadSync();
  });

  setInterval(refreshHealth, 15000);
  window.addEventListener("online", () => { refreshHealth(); toast("Back online — pushing queued changes."); api("/api/sync/run", { method: "POST" }).then(loadSync).catch(() => {}); });
  window.addEventListener("offline", () => { $("#conn").textContent = "offline"; $("#conn").className = "pill warn"; toast("Offline — local features keep working; changes are queued.", "warn"); });

  const health = await refreshHealth();
  if (state.token) {
    try { await loadMe(); await afterSignIn(); } catch { state.token = ""; localStorage.removeItem(TOKEN_KEY); }
  }
  $("#idp-mode").textContent = health && health.credentials.identity ? "Google / Apple / Microsoft client ids are configured." : "No provider client ids here — the local dev IdP is used so you can try everything immediately.";
  $("#dev-note").textContent = "The dev IdP is for development. Disable it with JARVIS_ALLOW_PASSWORDLESS_DEV_IDP=false and set real client ids.";
  show(state.token ? "chat" : "signin");
  $("#view-chat").hidden = !state.token;
  if (!state.token) $$(".view").forEach((v) => { if (v.id !== "view-signin") v.hidden = true; });
}

async function synthClip(text, f0) {
  /* ask the server for synthesised speech, then send it back through the same
     endpoints a microphone would use — no special-cased "demo mode" in the API */
  const pcm = await api("/api/voice/synth", { method: "POST", body: { text, f0 } });
  return pcm.audio_b64;
}

document.addEventListener("DOMContentLoaded", boot);
