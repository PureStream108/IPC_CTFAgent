from __future__ import annotations

from backend.memory.memory_store import Memory, MemoryStore, _tokenize


def _query_terms(query: str) -> list[str]:
    return [t for t in _tokenize(query.lower()) if len(t) >= 2]


def score_memory(mem: Memory, terms: list[str]) -> float:
    if not terms:
        return 0.0
    title_terms = set(_tokenize(mem.title.lower()))
    tag_terms = {t.lower() for t in mem.tags}
    body_terms = set(_tokenize(mem.content.lower()))
    score = 0.0
    for term in terms:
        if term in title_terms:
            score += 3.0
        if term in tag_terms:
            score += 2.5
        if term in body_terms:
            score += 1.0
    return score


def search(
    store: MemoryStore,
    query: str,
    category: str | None = None,
    limit: int = 5,
) -> list[tuple[Memory, float]]:
    terms = _query_terms(query)
    candidates = store.list(category)
    scored = [(m, score_memory(m, terms)) for m in candidates]
    scored = [(m, s) for (m, s) in scored if s > 0]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:limit]
