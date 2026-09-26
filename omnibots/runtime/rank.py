"""Small BM25 ranking, shared by the skill pool and the playbook lookup."""

from __future__ import annotations

import math
import re
from typing import Sequence, TypeVar

T = TypeVar("T")
_WORD = re.compile(r"[a-z0-9]+")
STOP = {"the", "and", "for", "with", "that", "this", "from", "into", "your", "you", "are", "was", "will", "can",
        "how", "what", "about", "make", "all", "any", "our", "use", "using", "get", "then", "them", "they"}


def words(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if len(w) > 2 and w not in STOP]


def bm25(query: str, docs: Sequence[tuple[T, str]], *, min_terms: int = 1) -> list[tuple[float, T]]:
    """Rank (item, text) pairs against the query. `min_terms`: how many distinct query words
    a document must contain to count (a guard against one-word coincidences)."""
    q = list(dict.fromkeys(words(query)))
    toks = [(item, words(text)) for item, text in docs]
    if not q or not toks:
        return []
    n, avg = len(toks), sum(len(d) for _, d in toks) / max(1, len(toks))
    df = {w: sum(1 for _, d in toks if w in d) for w in q}
    out = []
    for item, d in toks:
        score, hit = 0.0, 0
        for w in q:
            tf = d.count(w)
            if tf:
                hit += 1
                idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
                score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(d) / max(avg, 1)))
        if score and hit >= min_terms:
            out.append((score, item))
    return sorted(out, key=lambda x: -x[0])
