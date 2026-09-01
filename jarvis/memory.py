"""Long-term memory with offline retrieval.

A dependency-free TF-IDF + cosine retriever over the user's notes, preferences and
past exchanges. It runs entirely against local SQLite, so "remember that ..." works
on a plane, and the corpus replicates through the op-log so every device holds the
same searchable history.

Ranking blends three terms so recall stays useful as the corpus grows:

    score = cosine(tfidf) * strength * recency_boost
"""

from __future__ import annotations

import math
import re
import time
import uuid
from typing import Any

TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’\-]*")
STOP = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "did", "do", "does",
    "for", "from", "had", "has", "have", "how", "i", "if", "in", "is", "it", "its", "me",
    "my", "no", "not", "of", "on", "or", "so", "that", "the", "their", "them", "then",
    "there", "these", "they", "this", "to", "too", "up", "us", "was", "we", "were", "what",
    "when", "which", "who", "will", "with", "you", "your", "just", "like", "get", "got",
}


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in TOKEN.findall(text) if t.lower() not in STOP and len(t) > 1]


def _stem(word: str) -> str:
    """Very light suffix stripping so "meetings" matches "meeting"."""
    for suffix in ("ies", "ing", "edly", "ers", "est", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            base = word[: -len(suffix)]
            if suffix == "ies":
                return base + "y"
            if suffix in ("ing", "ed") and base.endswith(("b", "d", "g", "m", "n", "p", "r", "s", "t")) and len(base) > 3:
                return base + base[-1] if len(base) < 4 else base
            return base
    return word


class MemoryStore:
    def __init__(self, db, settings, user_id: str):
        self.db = db
        self.settings = settings
        self.user_id = user_id

    # ------------------------------------------------------------------ write
    def remember(self, body: str, *, tags: list[str] | None = None, source: str = "user", strength: float = 1.0) -> str:
        mid = f"mem_{uuid.uuid4().hex[:12]}"
        tags = sorted(set([*(tags or []), *self._auto_tags(body)]))
        self.db.append_op(
            device_id=self.settings.device_id,
            user_id=self.user_id,
            entity="memory",
            entity_key=mid,
            field=None,
            kind="set",
            payload={"body": body.strip(), "tags": tags, "source": source, "strength": strength},
        )
        return mid

    def update(self, memory_id: str, body: str) -> bool:
        row = self.db.one("SELECT id FROM memories WHERE user_id=? AND id=? AND deleted_at IS NULL", (self.user_id, memory_id))
        if row is None:
            return False
        self.db.append_op(
            device_id=self.settings.device_id,
            user_id=self.user_id,
            entity="memory",
            entity_key=memory_id,
            field=None,
            kind="set",
            payload={"body": body.strip(), "tags": self._tags_of(memory_id), "source": "user"},
        )
        return True

    def forget(self, memory_id: str) -> bool:
        """Tombstoned, not erased locally-only: a delete must beat stale copies on other devices."""
        row = self.db.one("SELECT id FROM memories WHERE user_id=? AND id=?", (self.user_id, memory_id))
        if row is None:
            return False
        self.db.append_op(
            device_id=self.settings.device_id,
            user_id=self.user_id,
            entity="memory",
            entity_key=memory_id,
            field=None,
            kind="delete",
            payload={},
        )
        return True

    def _tags_of(self, memory_id: str) -> list[str]:
        raw = self.db.scalar("SELECT tags FROM memories WHERE id=?", (memory_id,), "[]")
        return list(self.db.jloads(raw, []))

    @staticmethod
    def _auto_tags(text: str) -> list[str]:
        out = []
        for w in tokenize(text):
            if re.match(r"^[A-Z]", w) or w.isdigit() or len(w) >= 9:
                out.append(w)
        return out[:6]

    # ------------------------------------------------------------------- read
    def all(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.query(
            """SELECT id, body, tags, source, created_at, strength, recall_count, last_recall
               FROM memories WHERE user_id=? AND deleted_at IS NULL ORDER BY created_at DESC LIMIT ?""",
            (self.user_id, limit),
        )
        return [
            {
                "id": r["id"],
                "body": r["body"],
                "tags": list(self.db.jloads(r["tags"], [])),
                "source": r["source"],
                "created_at": r["created_at"],
                "age_days": round((time.time() - float(r["created_at"])) / 86400.0, 2),
                "strength": float(r["strength"]),
                "recall_count": int(r["recall_count"]),
            }
            for r in rows
        ]

    def search(self, query: str, *, limit: int = 6, min_score: float = 0.02) -> list[dict[str, Any]]:
        items = self.all(limit=5000)
        if not items or not query.strip():
            return []
        docs = [tokenize(i["body"]) for i in items]
        stemmed = [[_stem(t) for t in d] for d in docs]
        vocab: dict[str, int] = {}
        for d in stemmed:
            for t in set(d):
                vocab[t] = vocab.get(t, 0) + 1
        n = len(stemmed)
        idf = {t: math.log((1.0 + n) / (1.0 + c)) + 1.0 for t, c in vocab.items()}

        # project documents into a shared sparse space via the query's vocabulary plus
        # their own; using a dict-based dot product keeps this exact without materialising
        # an n x |V| matrix (fine for a personal corpus, cheap up to tens of thousands)
        q_tokens = [_stem(t) for t in tokenize(query)]
        if not q_tokens:
            return []
        q_map: dict[str, float] = {}
        for t in set(q_tokens):
            q_map[t] = q_map.get(t, 0.0) + (1.0 + math.log(q_tokens.count(t))) * idf.get(t, math.log(1.0 + n) + 1.0)
        q_norm = math.sqrt(sum(v * v for v in q_map.values())) or 1.0
        q_map = {k: v / q_norm for k, v in q_map.items()}

        scored = []
        now = time.time()
        for item, tokens in zip(items, stemmed, strict=False):
            d_map: dict[str, float] = {}
            counts: dict[str, int] = {}
            for t in tokens:
                counts[t] = counts.get(t, 0) + 1
            for t, c in counts.items():
                d_map[t] = d_map.get(t, 0.0) + (1.0 + math.log(c)) * idf.get(t, math.log(1.0 + n) + 1.0)
            d_norm = math.sqrt(sum(v * v for v in d_map.values())) or 1.0
            dot = sum(v * q_map.get(t, 0.0) for t, v in d_map.items()) / d_norm
            if dot <= min_score:
                continue
            age_days = max(0.0, (now - float(item["created_at"])) / 86400.0)
            recency = 1.0 + 0.35 * math.exp(-age_days / 120.0)
            reuse = 1.0 + 0.1 * min(4, int(item["recall_count"]))
            score = dot * float(item["strength"]) * recency * reuse
            scored.append({**item, "score": round(float(score), 4), "match_terms": sorted(set(q_map) & set(d_map))[:8]})
        scored.sort(key=lambda d: -d["score"])
        hits = scored[:limit]
        if hits:
            with self.db.write() as conn:
                for h in hits:
                    conn.execute(
                        "UPDATE memories SET recall_count=recall_count+1, last_recall=? WHERE id=?",
                        (time.time(), h["id"]),
                    )
        return hits

    def context_block(self, query: str, *, max_chars: int = 1200) -> tuple[str, list[dict[str, Any]]]:
        """Retrieved memory formatted for prompt assembly."""
        hits = self.search(query, limit=6)
        if not hits:
            return "", hits
        lines, used = [], 0
        for h in hits:
            line = f"- ({h['age_days']}d ago) {h['body'][:220]}"
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines), hits


