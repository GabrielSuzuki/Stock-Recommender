"""Semantic response cache: the cheapest request is the one you never send.

Real workloads repeat themselves -- support questions, doc lookups, retries,
identical prompts from different users. An exact-match cache catches some of
that; a similarity cache catches "what's your refund policy" vs "how do
refunds work", which are the same request wearing different clothes.

Two layers:
  1. Exact hash of the canonicalized request -> instant hit.
  2. Cosine similarity over a vector of the request text. The default
     embedder is a dependency-free hashed word+char-ngram TF vector, which is
     good at catching paraphrase-level near-duplicates. Pass your own
     `embedder` (any callable str -> sequence[float]) for true semantic
     matching if you already run an embedding model.

Safety rails, because a wrong cache hit is worse than a wasted token:
  - off by default for requests with tools, temperature > 0.3, or streaming
  - threshold defaults to a deliberately strict 0.93
  - entries expire (default 24h) and the store is capped
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

import numpy as np

DIM = 512
_WORD = re.compile(r"[a-z0-9']+")

# Spans that carry identity rather than meaning. Two requests that differ only
# in these are NOT the same request -- returning the cached answer for ticket
# #123 when asked about ticket #456 is the worst bug a cache can have, and a
# lexical similarity score will happily wave it through.
_DISCRIMINATORS = re.compile(
    r"""
      [\w.+-]+@[\w-]+\.[\w.]+            # emails
    | https?://\S+                        # urls
    | \b[0-9a-fA-F]{8,}\b                 # hex ids / hashes
    | \d[\d,._/-]*                        # numbers, dates, versions
    | \b[A-Z]{2,}-\d+\b                   # JIRA-123 style keys
    """,
    re.X,
)


def discriminators(text: str) -> frozenset[str]:
    """Identity-bearing spans in a request."""
    return frozenset(m.group(0).strip(".,_-/") for m in _DISCRIMINATORS.finditer(text))


# -- default embedder ------------------------------------------------------
_STOP = frozenset(
    """a an the and or but if then than that this these those is are was were be been being
    do does did doing have has had having i you he she it we they me him her them my your his
    its our their of to in on at by for with from as about into over after before between
    can could should would will shall may might must not no nor so such very just also only
    what which who whom whose when where why how there here get got please thanks""".split()
)


def _stem(w: str) -> str:
    """Crude suffix stripping. Enough to fold refund/refunds/refunding."""
    for suf in ("ing", "edly", "ies", "ers", "er", "ed", "es", "s"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


def hashed_embedding(text: str, dim: int = DIM) -> np.ndarray:
    """Lexical near-duplicate vector: stemmed content words + char trigrams.

    This is deliberately NOT a neural embedding. It catches the same request
    reworded, reordered, re-punctuated, or pluralized -- which is most of the
    repetition in real traffic -- at zero cost and microsecond latency. It does
    NOT know that "automobile" means "car". For true paraphrase matching pass
    a real model via `embedder=` (see `sentence_transformer_embedder` and
    `voyage_embedder` below).
    """
    vec = np.zeros(dim, dtype=np.float32)
    low = text.lower()
    words = [_stem(w) for w in _WORD.findall(low) if w not in _STOP and len(w) > 1]
    for w in words:
        vec[hash_str(w) % dim] += 3.0
    for a, b in zip(words, words[1:]):
        vec[hash_str(a + "_" + b) % dim] += 1.0
    joined = " ".join(words)
    for i in range(len(joined) - 2):
        vec[hash_str(joined[i : i + 3]) % dim] += 0.5
    norm = np.linalg.norm(vec)
    return vec / norm if norm else vec


def sentence_transformer_embedder(model_name: str = "all-MiniLM-L6-v2"):
    """Real local semantic embeddings, if sentence-transformers is installed.

        cache = SemanticCache(embedder=sentence_transformer_embedder())
    """
    from sentence_transformers import SentenceTransformer  # optional dependency

    model = SentenceTransformer(model_name)
    return lambda text: model.encode(text[:8000], normalize_embeddings=True)


def voyage_embedder(model: str = "voyage-3", api_key: str | None = None):
    """Hosted embeddings via Voyage AI (Anthropic's recommended provider).

    Costs a fraction of a cent per lookup -- still far cheaper than the call
    it saves, but budget for it if your hit rate is low.
    """
    import voyageai  # optional dependency

    client = voyageai.Client(api_key=api_key)

    def embed(text: str):
        return client.embed([text[:8000]], model=model, input_type="query").embeddings[0]

    return embed


def hash_str(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "big")


# -- store -----------------------------------------------------------------
@dataclass
class CacheHit:
    text: str
    model: str
    similarity: float
    exact: bool
    saved_input_tokens: int
    saved_output_tokens: int
    age_seconds: float


class SemanticCache:
    def __init__(
        self,
        path: str = ".tokenwise/semantic_cache.db",
        threshold: float = 0.93,
        ttl_seconds: int = 86_400,
        max_entries: int = 20_000,
        embedder: Optional[Callable[[str], Sequence[float]]] = None,
        enabled: bool = True,
        namespace: str = "default",
        max_candidates: int = 5,
    ) -> None:
        self.threshold = threshold
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self.embedder = embedder or hashed_embedding
        self.enabled = enabled
        self.namespace = namespace
        self.max_candidates = max_candidates
        self.path = path
        self._conn: Optional[sqlite3.Connection] = None
        self._keys: list[str] = []
        self._matrix: Optional[np.ndarray] = None
        if enabled:
            self._open()

    # -- lifecycle ---------------------------------------------------------
    def _open(self) -> None:
        import os

        if self.path != ":memory:":
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS entries (
                   key TEXT PRIMARY KEY, ns TEXT, exact_hash TEXT, vector BLOB,
                   response TEXT, model TEXT, input_tokens INT, output_tokens INT,
                   created REAL, hits INT DEFAULT 0, disc TEXT DEFAULT '[]')"""
        )
        try:
            self._conn.execute("ALTER TABLE entries ADD COLUMN disc TEXT DEFAULT '[]'")
        except sqlite3.OperationalError:
            pass  # column already present
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_exact ON entries(ns, exact_hash)")
        self._conn.commit()
        self._reload()

    def _reload(self) -> None:
        assert self._conn
        cutoff = time.time() - self.ttl
        self._conn.execute("DELETE FROM entries WHERE created < ?", (cutoff,))
        self._conn.commit()
        rows = self._conn.execute(
            "SELECT key, vector FROM entries WHERE ns = ?", (self.namespace,)
        ).fetchall()
        self._keys = [r[0] for r in rows]
        if rows:
            self._matrix = np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
        else:
            self._matrix = None

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # -- keys --------------------------------------------------------------
    @staticmethod
    def canonical(messages: list[Any], system: Any = None) -> str:
        parts = []
        if system:
            parts.append("SYSTEM:" + _flatten_one(system))
        for m in messages:
            role = m.get("role") if isinstance(m, dict) else getattr(m, "role", "user")
            parts.append(f"{role}:{_flatten_one(_content_of(m))}")
        return "\n".join(parts).strip()

    def cacheable(self, *, tools: Any = None, temperature: Optional[float] = None, stream: bool = False) -> tuple[bool, str]:
        if not self.enabled:
            return False, "disabled"
        if tools:
            return False, "tools-present"
        if stream:
            return False, "streaming"
        if temperature is not None and temperature > 0.3:
            return False, f"temperature={temperature}"
        return True, ""

    # -- read/write --------------------------------------------------------
    def lookup(self, messages: list[Any], system: Any = None) -> Optional[CacheHit]:
        if not self.enabled or not self._conn:
            return None
        text = self.canonical(messages, system)
        exact = hashlib.sha256(text.encode()).hexdigest()
        now = time.time()

        row = self._conn.execute(
            "SELECT response, model, input_tokens, output_tokens, created, key "
            "FROM entries WHERE ns = ? AND exact_hash = ? LIMIT 1",
            (self.namespace, exact),
        ).fetchone()
        if row and now - row[4] <= self.ttl:
            self._bump(row[5])
            return CacheHit(row[0], row[1], 1.0, True, row[2], row[3], now - row[4])

        if self._matrix is None or not len(self._keys):
            return None
        vec = np.asarray(self.embedder(text), dtype=np.float32)
        n = np.linalg.norm(vec)
        if not n:
            return None
        sims = self._matrix @ (vec / n)
        want = discriminators(text)
        # Walk candidates best-first; the top match can be rejected by the
        # identity guard while a lower one is still a legitimate hit.
        for idx in np.argsort(-sims)[: self.max_candidates]:
            score = float(sims[idx])
            if score < self.threshold:
                break
            row = self._conn.execute(
                "SELECT response, model, input_tokens, output_tokens, created, disc "
                "FROM entries WHERE key = ?",
                (self._keys[int(idx)],),
            ).fetchone()
            if not row or now - row[4] > self.ttl:
                continue
            if frozenset(json.loads(row[5] or "[]")) != want:
                continue  # same words, different subject
            self._bump(self._keys[int(idx)])
            return CacheHit(row[0], row[1], score, False, row[2], row[3], now - row[4])
        return None

    def store(
        self,
        messages: list[Any],
        response_text: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        system: Any = None,
    ) -> None:
        if not self.enabled or not self._conn or not response_text:
            return
        text = self.canonical(messages, system)
        key = hashlib.sha256(f"{self.namespace}|{text}".encode()).hexdigest()
        vec = np.asarray(self.embedder(text), dtype=np.float32)
        norm = np.linalg.norm(vec)
        if norm:
            vec = vec / norm
        self._conn.execute(
            "INSERT OR REPLACE INTO entries "
            "(key, ns, exact_hash, vector, response, model, input_tokens, output_tokens, "
            " created, hits, disc) VALUES (?,?,?,?,?,?,?,?,?,0,?)",
            (
                key,
                self.namespace,
                hashlib.sha256(text.encode()).hexdigest(),
                vec.tobytes(),
                response_text,
                model,
                input_tokens,
                output_tokens,
                time.time(),
                json.dumps(sorted(discriminators(text))),
            ),
        )
        self._conn.execute(
            "DELETE FROM entries WHERE key IN ("
            "  SELECT key FROM entries ORDER BY created DESC LIMIT -1 OFFSET ?)",
            (self.max_entries,),
        )
        self._conn.commit()
        self._reload()

    def _bump(self, key: str) -> None:
        assert self._conn
        self._conn.execute("UPDATE entries SET hits = hits + 1 WHERE key = ?", (key,))
        self._conn.commit()

    def stats(self) -> dict:
        if not self._conn:
            return {"entries": 0, "hits": 0}
        row = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(hits),0) FROM entries WHERE ns = ?", (self.namespace,)
        ).fetchone()
        return {"entries": row[0], "hits": row[1]}


def _content_of(m: Any) -> Any:
    return m.get("content") if isinstance(m, dict) else getattr(m, "content", m)


def _flatten_one(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    out.append(b.get("text", ""))
                else:
                    out.append(json.dumps(b, sort_keys=True, default=str))
            else:
                out.append(str(b))
        return "\n".join(out).strip()
    return str(content)
