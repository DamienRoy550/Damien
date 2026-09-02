import express from "express";
import { fileURLToPath } from "url";
import { dirname, join } from "path";
import { generateReply } from "./brain.js";

const __dirname = dirname(fileURLToPath(import.meta.url));

const app = express();
const PORT = process.env.PORT || 3000;

app.use(express.json());
app.use(express.static(join(__dirname, "public")));

// The "brain": receives what the user said, returns a reply.
app.post("/api/reply", (req, res) => {
  const { text, history } = req.body || {};
  if (typeof text !== "string" || !text.trim()) {
    return res.status(400).json({ error: "No speech text provided." });
  }
  try {
    const reply = generateReply(text.trim(), Array.isArray(history) ? history : []);
    res.json({ reply });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: "The AI had trouble thinking of a reply." });
  }
});

app.get("/api/health", (_req, res) => res.json({ ok: true }));

app.listen(PORT, "0.0.0.0", () => {
  console.log(`Damien voice AI listening on http://0.0.0.0:${PORT}`);
});
