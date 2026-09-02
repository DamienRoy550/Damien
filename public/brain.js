// A dependency-free conversational "brain".
// It understands intents and generates a spoken-friendly reply.
// This runs on the server so it can be swapped for a real LLM later
// (just replace generateReply with an API call).

const pick = (arr) => arr[Math.floor(Math.random() * arr.length)];

function tryMath(text) {
  // Handle "what is 2 + 2", "12 times 7", "square root of 81", etc.
  let t = text.toLowerCase();
  const sqrt = t.match(/square root of\s+(-?\d+(?:\.\d+)?)/);
  if (sqrt) {
    const n = parseFloat(sqrt[1]);
    if (n < 0) return "You can't take the square root of a negative number, at least not a real one.";
    return `The square root of ${n} is ${round(Math.sqrt(n))}.`;
  }
  t = t
    .replace(/what(?:'s| is)|calculate|compute|how much is|equals?|please|\?/g, " ")
    .replace(/\bplus\b|\band\b/g, "+")
    .replace(/\bminus\b/g, "-")
    .replace(/\btimes\b|\bmultiplied by\b|\bx\b/g, "*")
    .replace(/\bdivided by\b|\bover\b/g, "/")
    .replace(/\bpercent of\b/g, "% of");

  const pctOf = t.match(/(-?\d+(?:\.\d+)?)\s*%\s*of\s*(-?\d+(?:\.\d+)?)/);
  if (pctOf) {
    const r = (parseFloat(pctOf[1]) / 100) * parseFloat(pctOf[2]);
    return `That's ${round(r)}.`;
  }

  const expr = t.match(/-?\d+(?:\.\d+)?(?:\s*[+\-*/]\s*-?\d+(?:\.\d+)?)+/);
  if (expr && /[+\-*/]/.test(expr[0])) {
    const cleaned = expr[0].replace(/[^0-9.+\-*/() ]/g, "");
    try {
      // Safe: string contains only digits and math operators.
      // eslint-disable-next-line no-new-func
      const result = Function(`"use strict"; return (${cleaned});`)();
      if (typeof result === "number" && isFinite(result)) {
        return `That equals ${round(result)}.`;
      }
    } catch (_) {}
  }
  return null;
}

const round = (n) => Math.round(n * 1e6) / 1e6;

const JOKES = [
  "Why did the scarecrow win an award? Because he was outstanding in his field.",
  "I told my computer I needed a break, and now it won't stop sending me KitKat ads.",
  "Why don't scientists trust atoms? Because they make up everything.",
  "I would tell you a UDP joke, but you might not get it.",
  "Why did the developer go broke? Because he used up all his cache.",
];

const FACTS = [
  "Honey never spoils. Archaeologists have found edible honey in ancient Egyptian tombs.",
  "Octopuses have three hearts and blue blood.",
  "A day on Venus is longer than its year.",
  "Bananas are berries, but strawberries aren't.",
  "There are more possible chess games than atoms in the observable universe.",
];

function generateReply(text, history = []) {
  const t = text.toLowerCase().trim();

  // Greetings
  if (/^(hi|hey|hello|yo|howdy|hiya|good (morning|afternoon|evening))\b/.test(t)) {
    return pick([
      "Hey there! I'm listening. What's on your mind?",
      "Hi! Great to hear you. How can I help?",
      "Hello! I'm all ears. What would you like to talk about?",
    ]);
  }

  // How are you
  if (/how are you|how'?s it going|how do you do|what'?s up/.test(t)) {
    return pick([
      "I'm doing great, thanks for asking! How about you?",
      "Running smoothly and happy to chat. What's up with you?",
    ]);
  }

  // Name / identity
  if (/your name|who are you|what are you/.test(t)) {
    return "I'm Damien, a voice AI. You talk, I listen, and I reply out loud.";
  }
  if (/who (made|created|built) you|who'?s your (maker|creator)/.test(t)) {
    return "I was built as a voice assistant that runs right in your browser.";
  }

  // Math (checked before time so "12 times 8" isn't mistaken for the time)
  const math = tryMath(t);
  if (math) return math;

  // Time & date
  if (/\btime\b/.test(t) && !/\btimes\b/.test(t)) {
    const now = new Date();
    return `It's ${now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })} right now.`;
  }
  if (/\b(date|day)\b/.test(t) && /\b(what|today|which)\b/.test(t)) {
    const now = new Date();
    return `Today is ${now.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric", year: "numeric" })}.`;
  }

  // Jokes & facts
  if (/joke|make me laugh|something funny/.test(t)) return pick(JOKES);
  if (/fun fact|tell me (a fact|something)|interesting/.test(t)) return pick(FACTS);

  // Gratitude
  if (/thank(s| you)|appreciate/.test(t)) {
    return pick(["You're very welcome!", "Anytime! Happy to help.", "My pleasure."]);
  }

  // Farewell
  if (/\b(bye|goodbye|see you|good night|later)\b/.test(t)) {
    return pick(["Goodbye! Talk to you soon.", "See you later! Take care.", "Bye for now!"]);
  }

  // Yes / no acknowledgements
  if (/^(yes|yeah|yep|sure|ok|okay)\b/.test(t)) {
    return "Got it. What would you like to do next?";
  }
  if (/^(no|nope|nah)\b/.test(t)) {
    return "No problem. Is there anything else I can help with?";
  }

  // Help / capabilities
  if (/what can you do|help me|your capabilities|commands/.test(t)) {
    return "You can ask me the time or date, do quick math, ask for a joke or a fun fact, or just chat. Try saying: what's twelve times eight?";
  }

  // Questions we don't specifically handle
  if (t.endsWith("?") || /^(what|why|how|when|where|who|can you|do you|is|are|will)\b/.test(t)) {
    return pick([
      `That's a good question about "${text}". I'm a lightweight voice assistant, so I might not have every answer, but I'm happy to keep talking about it.`,
      `Interesting question. I don't have a full knowledge base wired up yet, but I heard you clearly: you asked "${text}".`,
    ]);
  }

  // Default: reflective, so the user knows they were heard.
  return pick([
    `I heard you say: "${text}". Tell me more!`,
    `Got it. You said "${text}". What would you like me to do with that?`,
    `Interesting. Say more about "${text}".`,
  ]);
}

window.generateReply = generateReply;
