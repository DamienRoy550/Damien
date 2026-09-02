// Damien — voice AI client.
// Hears you via the Web Speech API (SpeechRecognition),
// sends the text to the server "brain", and speaks the reply (speechSynthesis).

(() => {
  "use strict";

  const els = {
    micBtn: document.getElementById("micBtn"),
    status: document.getElementById("status"),
    interim: document.getElementById("interim"),
    log: document.getElementById("log"),
    orb: document.getElementById("orb"),
    continuous: document.getElementById("continuous"),
    voiceSelect: document.getElementById("voiceSelect"),
    unsupported: document.getElementById("unsupported"),
    typeInput: document.getElementById("typeInput"),
  };

  const SpeechRecognition =
    window.SpeechRecognition || window.webkitSpeechRecognition;
  const synth = window.speechSynthesis;

  // Platform detection
  const ua = navigator.userAgent;
  const isIOS =
    /iPad|iPhone|iPod/.test(ua) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  const isSafari = /^((?!chrome|android|crios|fxios|edgios).)*safari/i.test(ua);
  const iosNonSafari = isIOS && !isSafari;

  const history = [];
  let recognition = null;
  let listening = false;
  let speaking = false;
  let wantContinuous = false;
  let voices = [];

  // ---------- UI helpers ----------
  function setStatus(msg) {
    els.status.textContent = msg;
  }

  function addBubble(who, text) {
    const div = document.createElement("div");
    div.className = `bubble ${who}`;
    const label = document.createElement("div");
    label.className = "who";
    label.textContent = who === "user" ? "You" : "Damien";
    const body = document.createElement("div");
    body.textContent = text;
    div.appendChild(label);
    div.appendChild(body);
    els.log.appendChild(div);
    els.log.scrollTop = els.log.scrollHeight;
  }

  function setOrb(state) {
    els.orb.classList.toggle("listening", state === "listening");
    els.orb.classList.toggle("speaking", state === "speaking");
  }

  // ---------- Text to speech ----------
  function loadVoices() {
    voices = synth ? synth.getVoices() : [];
    if (!voices.length) return;
    const prev = els.voiceSelect.value;
    els.voiceSelect.innerHTML = "";
    // Prefer English voices at the top.
    const sorted = [...voices].sort((a, b) => {
      const ae = a.lang.startsWith("en") ? 0 : 1;
      const be = b.lang.startsWith("en") ? 0 : 1;
      return ae - be || a.name.localeCompare(b.name);
    });
    for (const v of sorted) {
      const opt = document.createElement("option");
      opt.value = v.name;
      opt.textContent = `${v.name} (${v.lang})`;
      els.voiceSelect.appendChild(opt);
    }
    // Restore or pick a sensible default.
    if (prev && sorted.some((v) => v.name === prev)) {
      els.voiceSelect.value = prev;
    } else {
      const preferred =
        sorted.find((v) => /Google US English|Samantha|Zira|Aria/i.test(v.name)) ||
        sorted.find((v) => v.lang.startsWith("en")) ||
        sorted[0];
      if (preferred) els.voiceSelect.value = preferred.name;
    }
  }

  function speak(text) {
    return new Promise((resolve) => {
      if (!synth) {
        resolve();
        return;
      }
      synth.cancel();
      const u = new SpeechSynthesisUtterance(text);
      const chosen = voices.find((v) => v.name === els.voiceSelect.value);
      if (chosen) u.voice = chosen;
      u.rate = 1.02;
      u.pitch = 1.0;
      u.onstart = () => {
        speaking = true;
        setOrb("speaking");
        setStatus("Damien is speaking…");
      };
      const done = () => {
        speaking = false;
        setOrb("idle");
        resolve();
      };
      u.onend = done;
      u.onerror = done;
      synth.speak(u);
    });
  }

  // ---------- The AI brain call ----------
  async function getReply(text) {
    try {
      const res = await fetch("/api/reply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, history: history.slice(-10) }),
      });
      if (!res.ok) throw new Error("bad status");
      const data = await res.json();
      return data.reply || "Sorry, I didn't catch that.";
    } catch (e) {
      return "I'm having trouble reaching my brain right now. Please try again.";
    }
  }

  async function handleUserText(text) {
    if (!text || !text.trim()) return;
    text = text.trim();
    addBubble("user", text);
    history.push({ role: "user", text });
    setStatus("Thinking…");
    setOrb("idle");

    const reply = await getReply(text);
    addBubble("ai", reply);
    history.push({ role: "ai", text: reply });

    await speak(reply);

    if (wantContinuous && recognition) {
      // Resume listening after speaking, hands-free.
      setStatus("Listening… (hands-free)");
      try {
        recognition.start();
      } catch (_) {}
    } else {
      setStatus("Tap the mic and start talking");
    }
  }

  // ---------- Speech recognition ----------
  function buildRecognition() {
    const rec = new SpeechRecognition();
    rec.lang = "en-US";
    rec.interimResults = true;
    rec.continuous = false;
    rec.maxAlternatives = 1;

    rec.onstart = () => {
      listening = true;
      els.micBtn.classList.add("active");
      els.micBtn.setAttribute("aria-label", "Stop listening");
      setOrb("listening");
      setStatus("Listening…");
    };

    rec.onresult = (event) => {
      let interim = "";
      let final = "";
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const transcript = event.results[i][0].transcript;
        if (event.results[i].isFinal) final += transcript;
        else interim += transcript;
      }
      els.interim.textContent = interim;
      if (final) {
        els.interim.textContent = "";
        handleUserText(final);
      }
    };

    rec.onerror = (e) => {
      if (e.error === "no-speech") {
        setStatus("I didn't hear anything. Tap the mic to try again.");
      } else if (e.error === "not-allowed" || e.error === "service-not-allowed") {
        setStatus("Microphone access was blocked. Please allow it in your browser.");
        wantContinuous = false;
        els.continuous.checked = false;
      } else if (e.error !== "aborted") {
        setStatus("Something went wrong hearing you. Tap the mic to retry.");
      }
    };

    rec.onend = () => {
      listening = false;
      els.micBtn.classList.remove("active");
      els.micBtn.setAttribute("aria-label", "Start listening");
      if (!speaking) setOrb("idle");
    };

    return rec;
  }

  function startListening() {
    if (!recognition) recognition = buildRecognition();
    if (listening) return;
    if (synth) synth.cancel(); // stop any current speech so we can hear
    try {
      recognition.start();
    } catch (_) {
      // start() can throw if already started; ignore.
    }
  }

  function stopListening() {
    wantContinuous = false;
    els.continuous.checked = false;
    if (recognition && listening) recognition.stop();
  }

  // ---------- Wire up ----------
  function initVoiceUI() {
    els.micBtn.addEventListener("click", () => {
      if (listening) {
        stopListening();
        setStatus("Stopped. Tap the mic to talk again.");
      } else {
        wantContinuous = els.continuous.checked;
        startListening();
      }
    });

    els.continuous.addEventListener("change", () => {
      wantContinuous = els.continuous.checked;
      if (wantContinuous && !listening && !speaking) startListening();
    });

    if (synth) {
      loadVoices();
      synth.onvoiceschanged = loadVoices;
    }
  }

  function initTypeFallback() {
    els.typeInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && els.typeInput.value.trim()) {
        const v = els.typeInput.value;
        els.typeInput.value = "";
        handleUserText(v);
      }
    });
  }

  function addCopyLinkButton() {
    const box = els.unsupported.querySelector(".typebox");
    if (!box) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "copylink";
    btn.textContent = "📋 Copy link for Safari";
    btn.addEventListener("click", async () => {
      const url = location.href;
      try {
        await navigator.clipboard.writeText(url);
        btn.textContent = "✓ Copied — now paste it in Safari";
      } catch (_) {
        btn.textContent = url;
        const r = document.createRange();
        r.selectNodeContents(btn);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(r);
      }
    });
    box.parentNode.insertBefore(btn, box);
  }

  function showUnsupported() {
    els.unsupported.classList.remove("hidden");
    els.micBtn.disabled = true;
    els.micBtn.style.opacity = "0.5";
    els.micBtn.style.cursor = "not-allowed";
    if (synth) {
      loadVoices();
      synth.onvoiceschanged = loadVoices;
    }
    initTypeFallback();
  }

  // ---------- Boot ----------
  if (iosNonSafari) {
    // iOS blocks speech recognition in every browser except Safari.
    const title = document.getElementById("unsupportedTitle");
    const body = document.getElementById("unsupportedBody");
    if (title) title.textContent = "On iPhone, voice needs Safari 🧭";
    if (body) {
      body.innerHTML =
        "Apple only lets microphone speech recognition work in <strong>Safari</strong> on iPhone — " +
        "Chrome and other iOS browsers are blocked from it by iOS itself. " +
        "To talk to Damien, open this page in <strong>Safari</strong>. " +
        "The button below copies the link so you can paste it into Safari.";
    }
    setStatus("Open in Safari to talk — or type to Damien below.");
    addCopyLinkButton();
    showUnsupported();
  } else if (!SpeechRecognition) {
    setStatus("Voice input isn't supported in this browser — you can type instead.");
    showUnsupported();
  } else {
    initVoiceUI();
    initTypeFallback(); // typing always works as a bonus
    if (isIOS && isSafari) {
      setStatus("Tap the mic and allow microphone access to talk.");
    }
  }

  // Greet on first interaction so the browser allows audio.
  let greeted = false;
  const greet = () => {
    if (greeted) return;
    greeted = true;
    const hello = "Hi! I'm Damien. Tap the microphone and talk to me — I'll reply out loud.";
    addBubble("ai", hello);
    speak(hello);
    document.removeEventListener("click", greet);
  };
  document.addEventListener("click", greet, { once: false });
})();
