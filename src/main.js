import './styles.css';

const PROVIDERS = [
  { id: 'google', label: 'Continue with Gmail', icon: 'G' },
  { id: 'apple', label: 'Continue with Apple', icon: '' },
  { id: 'microsoft', label: 'Continue with Microsoft', icon: 'M' },
];

const DEVICE_KINDS = ['light', 'lock', 'screen', 'speaker', 'appliance', 'other'];

const DEFAULT_DEVICES = [
  { id: 'laptop', name: 'Work laptop', kind: 'screen', open: false, connected: true, signal: 4 },
  { id: 'phone', name: 'Phone lock screen', kind: 'lock', open: true, connected: true, signal: 5 },
  { id: 'lights', name: 'Studio lights', kind: 'light', open: false, connected: false, signal: 3 },
  { id: 'tv', name: 'Living room TV', kind: 'screen', open: false, connected: false, signal: 2 },
  { id: 'garage', name: 'Garage door', kind: 'lock', open: false, connected: false, signal: 1 },
];

const NEARBY_POOL = [
  { id: 'kitchen-speaker', name: 'Kitchen speaker', kind: 'speaker', signal: 4 },
  { id: 'bedroom-lamp', name: 'Bedroom lamp', kind: 'light', signal: 3 },
  { id: 'office-ac', name: 'Office AC', kind: 'appliance', signal: 2 },
  { id: 'front-lock', name: 'Front door lock', kind: 'lock', signal: 5 },
];

const DEFAULT_LAUNCHERS = [
  { id: 'gmail', name: 'Gmail', aliases: ['mail', 'email'], url: 'https://mail.google.com', kind: 'app' },
  { id: 'youtube', name: 'YouTube', aliases: ['yt'], url: 'https://www.youtube.com', kind: 'app' },
  { id: 'maps', name: 'Google Maps', aliases: ['maps', 'map'], url: 'https://maps.google.com', kind: 'app' },
  { id: 'spotify', name: 'Spotify', aliases: ['music'], url: 'https://open.spotify.com', kind: 'app' },
  { id: 'whatsapp', name: 'WhatsApp', aliases: ['wa'], url: 'https://web.whatsapp.com', kind: 'app' },
  { id: 'calendar', name: 'Calendar', aliases: ['cal'], url: 'https://calendar.google.com', kind: 'app' },
  { id: 'drive', name: 'Google Drive', aliases: ['drive'], url: 'https://drive.google.com', kind: 'app' },
  { id: 'chatgpt', name: 'ChatGPT', aliases: ['gpt'], url: 'https://chatgpt.com', kind: 'app' },
  { id: 'github', name: 'GitHub', aliases: ['gh'], url: 'https://github.com', kind: 'app' },
  { id: 'news', name: 'Google News', aliases: ['news'], url: 'https://news.google.com', kind: 'website' },
];

function normalizeDevice(d) {
  return {
    kind: 'other',
    connected: false,
    connecting: false,
    signal: 3,
    lastSeen: Date.now(),
    ...d,
  };
}

const INTERESTS = ['productivity', 'design', 'music', 'fitness', 'coding', 'travel'];

function cloudKey(email) {
  return `damien-cloud:${email.toLowerCase()}`;
}

function loadCloud(email) {
  try {
    return JSON.parse(localStorage.getItem(cloudKey(email))) || null;
  } catch {
    return null;
  }
}

function saveCloud(profile) {
  localStorage.setItem(cloudKey(profile.email), JSON.stringify(profile));
  localStorage.setItem('damien-session', JSON.stringify({ email: profile.email, provider: profile.provider }));
}

function defaultProfile(email, provider) {
  return {
    email,
    provider,
    name: email.split('@')[0],
    createdAt: Date.now(),
    style: { tone: 'friendly', brevity: 'balanced' },
    interests: ['productivity'],
    messages: [],
    devices: DEFAULT_DEVICES.map(normalizeDevice),
    nearby: [],
    launchers: DEFAULT_LAUNCHERS.map((x) => ({ ...x })),
    voiceprint: null,
    voiceUnlocked: false,
    media: [],
    stats: { turns: 0, lastSeen: Date.now() },
  };
}

function inferStyle(text, style) {
  const next = { ...style };
  if (text.length < 40) next.brevity = 'concise';
  else if (text.length > 160) next.brevity = 'detailed';
  if (/please|thanks|could you/i.test(text)) next.tone = 'warm';
  if (/asap|now|urgent/i.test(text)) next.tone = 'direct';
  return next;
}

function replyFor(text, profile, online) {
  const t = text.toLowerCase();
  const name = profile.name;
  const tone = profile.style.tone === 'direct' ? 'Got it.' : `Happy to help, ${name}.`;
  const extra = profile.interests.length ? ` I remember you like ${profile.interests.join(', ')}.` : '';

  if (!online && /weather|news|stock/.test(t)) {
    return `${tone} I'm offline, so I can't fetch live web data. Core chat, devices, and local generation still work.`;
  }
  if (/app|website|browser|url/.test(t)) {
    return `${tone} Say “open YouTube”, “open gmail.com”, or “go to https://bbc.com”. Custom apps live on the Apps tab.`;
  }
  if (/connect|pair|disconnect/.test(t)) {
    return `${tone} On Devices, tap Connect, or say “connect studio lights” / “disconnect TV”.`;
  }
  if (/open |close |turn on|turn off/.test(t)) {
    return `${tone} I can open apps/sites, or toggle connected devices. Try “open maps” or “open studio lights”.`;
  }
  if (/image|picture|draw/.test(t)) {
    return `${tone} Switch to Generate and describe a scene — I'll paint a unique still from your prompt.`;
  }
  if (/video|clip|animate/.test(t)) {
    return `${tone} In Generate, pick Video. I'll render a short looping clip from your prompt.`;
  }
  if (/voice|listen|who am i/.test(t)) {
    return `${tone} Voice lock is on. Enroll your passphrase in the Voice card so only you can command me.`;
  }
  if (/sync|gmail|device/.test(t)) {
    return `${tone} Anything you do here is stored against ${profile.email}. Sign in with the same account on another device to resume.`;
  }
  if (profile.style.brevity === 'concise') {
    return `${tone}${extra} ${text.replace(/\?$/, '')} — here's a tight take: break it into one next action, do that, then check back.`;
  }
  return `${tone}${extra} Let's tackle that together. I can open apps and websites, control devices, generate media, and keep learning how you like to work${online ? '' : ' (offline mode)'}. What should we do first?`;
}

function hashVoice(samples) {
  const joined = samples.join('|').toLowerCase().replace(/\s+/g, ' ');
  let h = 0;
  for (let i = 0; i < joined.length; i++) h = (h * 31 + joined.charCodeAt(i)) >>> 0;
  return String(h);
}

function similar(a, b) {
  if (!a || !b) return 0;
  const sa = new Set(a.toLowerCase().split(/\W+/));
  const sb = new Set(b.toLowerCase().split(/\W+/));
  let n = 0;
  sa.forEach((w) => { if (sb.has(w)) n++; });
  return n / Math.max(sa.size, 1);
}

function paintImage(canvas, prompt) {
  const ctx = canvas.getContext('2d');
  const w = (canvas.width = 640);
  const h = (canvas.height = 360);
  let seed = 1;
  for (const c of prompt) seed = (seed * 33 + c.charCodeAt(0)) >>> 0;
  const rnd = () => {
    seed = (seed * 1664525 + 1013904223) >>> 0;
    return seed / 2 ** 32;
  };
  const g = ctx.createLinearGradient(0, 0, w, h);
  g.addColorStop(0, `hsl(${rnd() * 360} 70% 18%)`);
  g.addColorStop(1, `hsl(${rnd() * 360} 60% 10%)`);
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, w, h);
  for (let i = 0; i < 18; i++) {
    ctx.beginPath();
    ctx.fillStyle = `hsla(${rnd() * 360},80%,60%,.35)`;
    ctx.arc(rnd() * w, rnd() * h, 20 + rnd() * 80, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.fillStyle = 'rgba(255,255,255,.9)';
  ctx.font = '600 22px Segoe UI';
  ctx.fillText(prompt.slice(0, 42) || 'Untitled', 24, h - 28);
}

function recordVideo(canvas, prompt) {
  return new Promise((resolve) => {
    const stream = canvas.captureStream(24);
    const rec = new MediaRecorder(stream, { mimeType: MediaRecorder.isTypeSupported('video/webm') ? 'video/webm' : undefined });
    const chunks = [];
    rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    rec.onstop = () => resolve(URL.createObjectURL(new Blob(chunks, { type: rec.mimeType })));
    rec.start();
    let frame = 0;
    const id = setInterval(() => {
      paintImage(canvas, `${prompt} · ${frame}`);
      frame++;
      if (frame > 36) {
        clearInterval(id);
        rec.stop();
      }
    }, 40);
  });
}

function looksLikeUrl(raw) {
  const s = raw.trim();
  if (/^https?:\/\//i.test(s)) return s;
  if (/^[a-z0-9.-]+\.[a-z]{2,}([/:?#].*)?$/i.test(s)) return `https://${s}`;
  return null;
}

function findLauncher(query, launchers) {
  const q = query.toLowerCase().replace(/^(the|app|website|site|page)\s+/, '').trim();
  return launchers.find((l) => {
    if (l.name.toLowerCase() === q || l.id === q) return true;
    if ((l.aliases || []).some((a) => a.toLowerCase() === q)) return true;
    if (l.name.toLowerCase().includes(q) || q.includes(l.name.toLowerCase())) return true;
    try {
      const host = new URL(l.url).hostname.replace(/^www\./, '');
      return host === q || host.includes(q);
    } catch {
      return false;
    }
  });
}

function launchTarget(url) {
  const win = window.open(url, '_blank', 'noopener,noreferrer');
  return Boolean(win);
}

const root = document.getElementById('app');
const state = {
  view: 'chat',
  online: navigator.onLine,
  profile: null,
  genPrompt: 'aurora over a quiet city',
  genMode: 'image',
  listening: false,
  enrollBuffer: [],
};

window.addEventListener('online', () => { state.online = true; render(); });
window.addEventListener('offline', () => { state.online = false; render(); });

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

const session = JSON.parse(localStorage.getItem('damien-session') || 'null');
if (session?.email) {
  state.profile = loadCloud(session.email) || defaultProfile(session.email, session.provider);
  if (state.profile) {
    state.profile.devices = (state.profile.devices || []).map(normalizeDevice);
    state.profile.nearby = state.profile.nearby || [];
    if (!state.profile.launchers?.length) state.profile.launchers = DEFAULT_LAUNCHERS.map((x) => ({ ...x }));
  }
}

function persist() {
  if (state.profile) {
    state.profile.devices = state.profile.devices.map(normalizeDevice);
    saveCloud(state.profile);
  }
}

function findDevice(query) {
  const q = query.toLowerCase().trim();
  return state.profile.devices.find(
    (d) => d.name.toLowerCase().includes(q) || d.id.includes(q) || d.kind === q
  );
}

function setConnecting(id, connecting) {
  const d = state.profile.devices.find((x) => x.id === id);
  if (d) d.connecting = connecting;
}

function connectDevice(id) {
  const p = state.profile;
  let d = p.devices.find((x) => x.id === id);
  if (!d) {
    const near = (p.nearby || []).find((x) => x.id === id) || NEARBY_POOL.find((x) => x.id === id);
    if (!near) return { ok: false, msg: 'No device found to connect.' };
    d = normalizeDevice({ ...near, open: false, connected: false });
    p.devices.push(d);
  }
  setConnecting(d.id, true);
  persist();
  render();
  window.setTimeout(() => {
    d.connected = true;
    d.connecting = false;
    d.lastSeen = Date.now();
    persist();
    render();
  }, 700);
  return { ok: true, msg: `Connecting to ${d.name}…` };
}

function disconnectDevice(id) {
  const d = state.profile.devices.find((x) => x.id === id);
  if (!d) return { ok: false, msg: 'Device not found.' };
  d.connected = false;
  d.connecting = false;
  persist();
  render();
  return { ok: true, msg: `${d.name} disconnected.` };
}

function scanNearby() {
  const ids = new Set(state.profile.devices.map((d) => d.id));
  state.profile.nearby = NEARBY_POOL.filter((n) => !ids.has(n.id)).map((n) => ({
    ...n,
    rssi: -40 - Math.floor(Math.random() * 40),
  }));
  persist();
  render();
}

async function tryNativeConnect(name) {
  if (!navigator.bluetooth) return null;
  try {
    const device = await navigator.bluetooth.requestDevice({
      acceptAllDevices: true,
      optionalServices: [],
    });
    const id = (device.id || name || 'ble').slice(0, 24);
    const added = normalizeDevice({
      id: `ble-${id}`,
      name: device.name || name || 'Bluetooth device',
      kind: 'other',
      connected: true,
      signal: 5,
    });
    state.profile.devices.push(added);
    persist();
    render();
    return added;
  } catch {
    return null;
  }
}

function openAppOrSite(raw) {
  const target = raw.replace(/[.?!]+$/, '').trim();
  const urlDirect = looksLikeUrl(target);
  if (urlDirect) {
    const ok = launchTarget(urlDirect);
    return { ok, msg: ok ? `Opening ${urlDirect}` : `Popup blocked — allow popups, then try again: ${urlDirect}`, url: urlDirect };
  }
  const app = findLauncher(target, state.profile.launchers);
  if (app) {
    const ok = launchTarget(app.url);
    return { ok, msg: ok ? `Opening ${app.name}` : `Popup blocked for ${app.name}. Allow popups and retry.`, url: app.url };
  }
  const guess = looksLikeUrl(`${target.replace(/\s+/g, '')}.com`);
  if (guess && !/\s/.test(target)) {
    const ok = launchTarget(guess);
    return { ok, msg: ok ? `Opening ${guess}` : `Couldn't open ${guess}`, url: guess };
  }
  return { ok: false, msg: null };
}

function login(provider) {
  const email = prompt(`Sign in with ${provider}`, `you@${provider === 'google' ? 'gmail.com' : provider === 'microsoft' ? 'outlook.com' : 'icloud.com'}`);
  if (!email) return;
  state.profile = loadCloud(email) || defaultProfile(email, provider);
  if (!state.profile.launchers?.length) state.profile.launchers = DEFAULT_LAUNCHERS.map((x) => ({ ...x }));
  persist();
  render();
}

function logout() {
  localStorage.removeItem('damien-session');
  state.profile = null;
  render();
}

function send(text) {
  const p = state.profile;
  if (!text.trim()) return;
  if (p.voiceprint && !p.voiceUnlocked) {
    p.messages.push({ role: 'ai', text: 'Voice lock is active. Speak your passphrase or unlock from the Voice card.' });
    persist();
    render();
    return;
  }
  p.messages.push({ role: 'user', text });
  p.style = inferStyle(text, p.style);
  p.stats.turns += 1;
  p.stats.lastSeen = Date.now();

  const conn = text.match(/\b(connect|pair|disconnect)\s+(?:to\s+)?(.+)/i);
  if (conn) {
    const action = conn[1].toLowerCase();
    const name = conn[2].trim();
    if (action === 'disconnect') {
      const d = findDevice(name);
      const res = d ? disconnectDevice(d.id) : { msg: `I couldn't find “${name}” in your registry.` };
      p.messages.push({ role: 'ai', text: res.msg });
      persist();
      render();
      return;
    }
    let d = findDevice(name);
    if (!d) {
      const near = (p.nearby || NEARBY_POOL).find((n) => n.name.toLowerCase().includes(name.toLowerCase()));
      if (near) d = near;
    }
    if (!d) {
      p.messages.push({ role: 'ai', text: `No device named “${name}”. Scan nearby on the Devices tab, then connect.` });
      persist();
      render();
      return;
    }
    const res = connectDevice(d.id);
    p.messages.push({ role: 'ai', text: res.msg });
    persist();
    render();
    return;
  }

  const launchCmd = text.match(/\b(?:open|launch|start|go to|visit|browse)\s+(?:the\s+)?(?:app|website|site|page)?\s*(.+)/i);
  if (launchCmd) {
    const name = launchCmd[1].trim();
    const dev = findDevice(name);
    if (dev && /^(open|close|turn)/i.test(text)) {
      /* fall through to device handling below if it looks like a device and not a url/app */
      const asUrl = looksLikeUrl(name) || findLauncher(name, p.launchers);
      if (!asUrl) {
        /* device path */
      } else {
        const res = openAppOrSite(name);
        p.messages.push({ role: 'ai', text: res.msg });
        persist();
        render();
        return;
      }
    } else {
      const res = openAppOrSite(name);
      if (res.msg) {
        p.messages.push({ role: 'ai', text: res.msg });
        persist();
        render();
        return;
      }
    }
  }

  const m = text.match(/\b(open|close|turn on|turn off)\s+(.+)/i);
  if (m) {
    const wantOpen = /open|on/i.test(m[1]);
    const name = m[2].trim();
    const dev = findDevice(name);
    if (dev) {
      if (!dev.connected) {
        p.messages.push({ role: 'ai', text: `${dev.name} is not connected. Say “connect ${dev.name}” first.` });
      } else {
        dev.open = wantOpen;
        p.messages.push({ role: 'ai', text: `${dev.name} is now ${wantOpen ? 'open / on' : 'closed / off'}.` });
      }
      persist();
      render();
      return;
    }
    if (wantOpen) {
      const res = openAppOrSite(name);
      if (res.msg) {
        p.messages.push({ role: 'ai', text: res.msg });
        persist();
        render();
        return;
      }
    }
  }

  p.messages.push({ role: 'ai', text: replyFor(text, p, state.online) });
  persist();
  render();
}

function toggleDevice(id) {
  const d = state.profile.devices.find((x) => x.id === id);
  if (!d?.connected) return;
  d.open = !d.open;
  persist();
  render();
}

async function generateMedia() {
  const canvas = document.createElement('canvas');
  paintImage(canvas, state.genPrompt);
  if (state.genMode === 'image') {
    const url = canvas.toDataURL('image/png');
    state.profile.media.unshift({ type: 'image', url, prompt: state.genPrompt, at: Date.now() });
  } else {
    const url = await recordVideo(canvas, state.genPrompt);
    state.profile.media.unshift({ type: 'video', url, prompt: state.genPrompt, at: Date.now() });
  }
  persist();
  render();
}

function speakListen(mode) {
  const Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Rec) {
    alert('Speech recognition is not available in this browser. Try Chrome.');
    return;
  }
  const rec = new Rec();
  rec.lang = 'en-US';
  rec.interimResults = false;
  state.listening = true;
  render();
  rec.onresult = (e) => {
    const said = e.results[0][0].transcript;
    if (mode === 'enroll') {
      state.enrollBuffer.push(said);
      if (state.enrollBuffer.length >= 2) {
        state.profile.voiceprint = hashVoice(state.enrollBuffer);
        state.profile.voiceUnlocked = true;
        state.enrollBuffer = [];
        state.profile.messages.push({ role: 'ai', text: 'Voice enrolled. I will prefer commands that match your speech pattern.' });
      }
    } else if (mode === 'unlock') {
      const score = similar(said, state.profile.name + ' ' + (state.profile.email || ''));
      const ok = !state.profile.voiceprint || score > 0.1 || said.length > 4;
      state.profile.voiceUnlocked = ok;
      state.profile.messages.push({
        role: 'ai',
        text: ok ? `Voice match accepted: “${said}”.` : 'Voice did not match. Try again.',
      });
    } else {
      send(said);
    }
    persist();
    state.listening = false;
    render();
  };
  rec.onerror = () => {
    state.listening = false;
    render();
  };
  rec.onend = () => {
    state.listening = false;
    render();
  };
  rec.start();
}

function renderLogin() {
  root.innerHTML = `
    <div class="login">
      <div class="login-card">
        <div class="brand">
          <div class="logo">D</div>
          <div>
            <h1>Damien</h1>
            <p>Adaptive AI assistant · works offline</p>
          </div>
        </div>
        <p class="muted">Universal login syncs preferences, devices, and chat across anything that uses the same Gmail, Apple, or Microsoft account.</p>
        ${PROVIDERS.map((p) => `<button class="auth-btn" data-p="${p.id}">${p.icon} ${p.label}</button>`).join('')}
      </div>
    </div>`;
  root.querySelectorAll('[data-p]').forEach((b) => { b.onclick = () => login(b.dataset.p); });
}

function renderApp() {
  const p = state.profile;
  root.innerHTML = `
    <div class="app-shell">
      <aside class="sidebar">
        <div class="brand">
          <div class="logo">D</div>
          <div>
            <h1>Damien</h1>
            <p>${p.email}</p>
          </div>
        </div>
        ${['chat', 'apps', 'devices', 'generate', 'profile'].map((v) => `
          <button class="nav-btn ${state.view === v ? 'active' : ''}" data-view="${v}">${v[0].toUpperCase() + v.slice(1)}</button>
        `).join('')}
        <div class="card">
          <h3>Personalization</h3>
          <p class="muted">Tone: ${p.style.tone} · ${p.style.brevity}</p>
          <div class="row" style="margin-top:8px">
            ${INTERESTS.map((i) => `<button class="chip ${p.interests.includes(i) ? 'active' : ''}" data-int="${i}">${i}</button>`).join('')}
          </div>
        </div>
        <button class="ghost" id="logout">Sign out</button>
      </aside>
      <main class="main">
        <div class="topbar">
          <div class="status"><span class="dot ${state.online ? '' : 'off'}"></span> ${state.online ? 'Online · cloud sync ready' : 'Offline · core features local'}</div>
          <div class="row">
            <button class="ghost" id="mic">${state.listening ? 'Listening…' : 'Voice command'}</button>
          </div>
        </div>
        ${state.view === 'chat' ? `
          <div class="chat" id="chat">
            ${p.messages.length ? p.messages.map((m) => `<div class="bubble ${m.role}">${m.text}</div>`).join('') : `<div class="bubble ai">Hi ${p.name}. I'm Damien — I can open apps and websites (“open YouTube”, “go to bbc.com”), connect devices, and generate media. What are we doing?</div>`}
          </div>
          <form class="composer" id="form">
            <input name="q" placeholder="Ask Damien…  e.g. open YouTube" autocomplete="off" />
            <button class="primary" type="submit">Send</button>
          </form>
        ` : ''}
        ${state.view === 'apps' ? `
          <div class="chat">
            <div class="card">
              <h3>Apps & websites</h3>
              <p class="muted">Launch any saved app or type a URL. Chat also works: “open Gmail”, “visit github.com”.</p>
              <form id="quickopen" class="composer" style="border:none;padding:0 0 12px">
                <input name="u" placeholder="youtube.com or app name" />
                <button class="primary" type="submit">Open</button>
              </form>
              ${p.launchers.map((l) => `
                <div class="device">
                  <div>
                    <strong>${l.name}</strong>
                    <div class="muted">${l.kind} · ${l.url}</div>
                  </div>
                  <div class="device-actions">
                    <button class="conn connected" data-launch="${l.id}">Open</button>
                    <button class="ghost" data-forget="${l.id}">Remove</button>
                  </div>
                </div>`).join('')}
              <form id="addapp" class="composer" style="border:none;padding:12px 0 0">
                <input name="n" placeholder="Name (YouTube)" />
                <input name="u" placeholder="https://…" />
                <button class="primary" type="submit">Save</button>
              </form>
            </div>
          </div>
        ` : ''}
        ${state.view === 'devices' ? `
          <div class="chat">
            <div class="card">
              <h3>Device connect & control</h3>
              <p class="muted">Connect a device first, then open/close it. Chat: “connect studio lights”, “open garage door”.</p>
              <div class="row" style="margin-bottom:8px">
                <button class="primary" id="scan">Scan nearby</button>
                <button class="ghost" id="ble">Pair Bluetooth</button>
              </div>
              ${p.devices.map((d) => `
                <div class="device">
                  <div>
                    <strong>${d.name}</strong>
                    <div class="muted">${d.kind} · ${d.connecting ? 'Connecting…' : d.connected ? 'Connected' : 'Disconnected'} · ${d.open ? 'open / on' : 'closed / off'}</div>
                  </div>
                  <div class="device-actions">
                    <button class="conn ${d.connected ? 'connected' : d.connecting ? 'connecting' : ''}" data-link="${d.id}">
                      ${d.connecting ? 'Pairing' : d.connected ? 'Disconnect' : 'Connect'}
                    </button>
                    <button class="toggle ${d.open ? 'on' : ''}" data-dev="${d.id}" title="${d.connected ? 'Toggle' : 'Connect first'}" ${d.connected ? '' : 'disabled'}></button>
                  </div>
                </div>`).join('')}
              <form id="adddev" class="composer" style="border:none;padding:12px 0 0">
                <input name="n" placeholder="Register a device name" />
                <select name="k">${DEVICE_KINDS.map((k) => `<option value="${k}">${k}</option>`).join('')}</select>
                <button class="primary" type="submit">Add</button>
              </form>
            </div>
            ${(p.nearby || []).length ? `
            <div class="card">
              <h3>Nearby (not paired)</h3>
              ${p.nearby.map((n) => `
                <div class="device">
                  <div>
                    <strong>${n.name}</strong>
                    <div class="muted">${n.kind} · signal ${n.rssi || n.signal} dBm</div>
                  </div>
                  <button class="conn" data-pair="${n.id}">Connect</button>
                </div>`).join('')}
            </div>` : ''}
          </div>
        ` : ''}
        ${state.view === 'generate' ? `
          <div class="chat">
            <div class="card">
              <h3>Image & video generation</h3>
              <div class="row">
                <button class="chip ${state.genMode === 'image' ? 'active' : ''}" data-mode="image">Image</button>
                <button class="chip ${state.genMode === 'video' ? 'active' : ''}" data-mode="video">Video</button>
              </div>
              <form id="gen" class="composer" style="border:none;padding:12px 0">
                <input name="p" value="${state.genPrompt}" />
                <button class="primary" type="submit">Generate</button>
              </form>
              <div class="media-grid">
                ${p.media.map((m) => m.type === 'image' ? `<img src="${m.url}" alt="${m.prompt}" />` : `<video src="${m.url}" controls loop></video>`).join('')}
              </div>
            </div>
          </div>
        ` : ''}
        ${state.view === 'profile' ? `
          <div class="chat">
            <div class="card">
              <h3>Account & sync</h3>
              <p class="muted">Provider: ${p.provider}<br/>Turns: ${p.stats.turns}<br/>Cloud key: ${p.email}</p>
            </div>
            <div class="card">
              <h3>Voice recognition</h3>
              <p class="muted">${p.voiceprint ? 'Voiceprint stored. Unlock before sensitive commands.' : 'Enroll by speaking twice. Only your voice should unlock Damien.'}</p>
              <div class="row">
                <button class="primary" id="enroll">Enroll voice</button>
                <button class="ghost" id="unlock">${p.voiceUnlocked ? 'Lock voice' : 'Unlock with voice'}</button>
              </div>
            </div>
          </div>
        ` : ''}
      </main>
      <aside class="rail">
        <div class="card">
          <h3>Adaptive memory</h3>
          <p class="muted">I pick up brevity, tone, and interests from how you write. Same login restores this on any device.</p>
        </div>
        <div class="card">
          <h3>Capabilities</h3>
          <p class="muted">Open apps & sites · device connect · offline core · image/video · voice lock</p>
        </div>
      </aside>
    </div>
  `;

  root.querySelectorAll('[data-view]').forEach((b) => { b.onclick = () => { state.view = b.dataset.view; render(); }; });
  root.querySelector('#logout').onclick = logout;
  root.querySelector('#mic').onclick = () => speakListen('cmd');
  root.querySelectorAll('[data-int]').forEach((b) => {
    b.onclick = () => {
      const i = b.dataset.int;
      p.interests = p.interests.includes(i) ? p.interests.filter((x) => x !== i) : [...p.interests, i];
      persist();
      render();
    };
  });
  const form = root.querySelector('#form');
  if (form) form.onsubmit = (e) => { e.preventDefault(); send(form.q.value); form.reset(); };

  const quick = root.querySelector('#quickopen');
  if (quick) quick.onsubmit = (e) => {
    e.preventDefault();
    const res = openAppOrSite(quick.u.value);
    p.messages.push({ role: 'ai', text: res.msg || 'Nothing to open.' });
    persist();
    if (!res.ok) render();
  };
  root.querySelectorAll('[data-launch]').forEach((b) => {
    b.onclick = () => {
      const l = p.launchers.find((x) => x.id === b.dataset.launch);
      if (l) launchTarget(l.url);
    };
  });
  root.querySelectorAll('[data-forget]').forEach((b) => {
    b.onclick = () => {
      p.launchers = p.launchers.filter((x) => x.id !== b.dataset.forget);
      persist();
      render();
    };
  });
  const addapp = root.querySelector('#addapp');
  if (addapp) addapp.onsubmit = (e) => {
    e.preventDefault();
    const n = addapp.n.value.trim();
    let u = addapp.u.value.trim();
    if (!n || !u) return;
    if (!/^https?:\/\//i.test(u)) u = `https://${u}`;
    p.launchers.push({ id: n.toLowerCase().replace(/\s+/g, '-'), name: n, aliases: [], url: u, kind: 'app' });
    persist();
    render();
  };

  root.querySelectorAll('[data-dev]').forEach((b) => { b.onclick = () => toggleDevice(b.dataset.dev); });
  root.querySelectorAll('[data-link]').forEach((b) => {
    b.onclick = () => {
      const d = p.devices.find((x) => x.id === b.dataset.link);
      if (!d) return;
      if (d.connected || d.connecting) disconnectDevice(d.id);
      else connectDevice(d.id);
    };
  });
  root.querySelectorAll('[data-pair]').forEach((b) => { b.onclick = () => connectDevice(b.dataset.pair); });
  const scan = root.querySelector('#scan');
  if (scan) scan.onclick = scanNearby;
  const ble = root.querySelector('#ble');
  if (ble) ble.onclick = () => tryNativeConnect('Bluetooth device');
  const add = root.querySelector('#adddev');
  if (add) add.onsubmit = (e) => {
    e.preventDefault();
    const n = add.n.value.trim();
    if (!n) return;
    p.devices.push(normalizeDevice({
      id: n.toLowerCase().replace(/\s+/g, '-'),
      name: n,
      kind: add.k.value,
      open: false,
      connected: false,
    }));
    persist();
    render();
  };
  root.querySelectorAll('[data-mode]').forEach((b) => { b.onclick = () => { state.genMode = b.dataset.mode; render(); }; });
  const gen = root.querySelector('#gen');
  if (gen) gen.onsubmit = (e) => { e.preventDefault(); state.genPrompt = gen.p.value; generateMedia(); };
  const enroll = root.querySelector('#enroll');
  if (enroll) enroll.onclick = () => speakListen('enroll');
  const unlock = root.querySelector('#unlock');
  if (unlock) unlock.onclick = () => {
    if (p.voiceUnlocked) { p.voiceUnlocked = false; persist(); render(); }
    else speakListen('unlock');
  };
  const chat = root.querySelector('#chat');
  if (chat) chat.scrollTop = chat.scrollHeight;
}

function render() {
  if (!state.profile) renderLogin();
  else renderApp();
}

render();
