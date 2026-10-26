"""Nearest past tasks and how each executor tier did on them.

The kNN router idea from LLMRouter, without embeddings or torch: TF-IDF over
prefix-stemmed words of routed task prompts, cosine similarity, top-k vote.
It only sees one tier per task (bandit feedback), so it reports evidence per
tier instead of predicting the best model.
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

WORD_RE = re.compile(r"\w+", re.UNICODE)
# Cheap stemming for Russian/English: "исправь"/"исправить" -> "испр".
STEM_CHARS = 5
MIN_WORD_CHARS = 3
TOP_K = 8
MIN_SIMILARITY = 0.2
REFRESH_SECONDS = 300
MAX_PROMPT_CHARS = 4000
OK_PHASES = {"completed", "agent_completed"}


@dataclass(frozen=True)
class PastRun:
    task_id: str
    tier: str
    success: bool


@dataclass(frozen=True)
class SimilarRuns:
    runs: tuple[tuple[float, PastRun], ...] = ()

    def per_tier(self) -> dict[str, tuple[int, int]]:
        """Tier -> (successes, runs) over the neighbours."""
        result: dict[str, list[int]] = {}
        for _, run in self.runs:
            counts = result.setdefault(run.tier, [0, 0])
            counts[0] += int(run.success)
            counts[1] += 1
        return {tier: (ok, total) for tier, (ok, total) in result.items()}

    def render(self) -> str:
        per_tier = self.per_tier()
        if not per_tier:
            return "no similar past tasks"
        return "; ".join(f"{tier} {ok}/{total} ok" for tier, (ok, total) in sorted(per_tier.items()))


@dataclass
class _Index:
    built_at: float = 0.0
    docs: list[tuple[PastRun, dict[str, float]]] = field(default_factory=list)


def tokens(text: str) -> list[str]:
    return [
        word[:STEM_CHARS]
        for word in WORD_RE.findall(text.lower())
        if len(word) >= MIN_WORD_CHARS and not word.isdigit()
    ]


def past_run(payload: dict[str, Any]) -> PastRun | None:
    """Outcome of a routed task, or None when it says nothing about a tier."""
    routing = payload.get("model_routing") or {}
    tier = routing.get("selected_tier")
    if not tier or routing.get("manual_tier", "auto") != "auto":
        return None
    feedback = routing.get("feedback") or {}
    if feedback.get("applied"):
        return PastRun(str(payload.get("id") or ""), str(tier), False)
    outcome = routing.get("outcome")
    if outcome in {"success", "failure"}:
        return PastRun(str(payload.get("id") or ""), str(tier), outcome == "success")
    phase = payload.get("phase")
    if phase in OK_PHASES:
        return PastRun(str(payload.get("id") or ""), str(tier), True)
    if phase == "failed":
        return PastRun(str(payload.get("id") or ""), str(tier), False)
    return None


class TaskSimilarity:
    def __init__(
        self,
        load_payloads: Callable[[], Iterable[dict[str, Any]]],
        refresh_seconds: int = REFRESH_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.load_payloads = load_payloads
        self.refresh_seconds = refresh_seconds
        self.clock = clock
        self._lock = threading.Lock()
        self._index = _Index()
        self._idf: dict[str, float] = {}

    def similar(self, prompt: str, exclude_task_id: str = "", k: int = TOP_K) -> SimilarRuns:
        with self._lock:
            self._refresh_locked()
            query = self._vector(tokens(prompt[:MAX_PROMPT_CHARS]))
            if not query:
                return SimilarRuns()
            scored = [
                (cosine(query, vector), run)
                for run, vector in self._index.docs
                if run.task_id != exclude_task_id
            ]
        scored = [item for item in scored if item[0] >= MIN_SIMILARITY]
        scored.sort(key=lambda item: item[0], reverse=True)
        return SimilarRuns(tuple(scored[:k]))

    def _refresh_locked(self) -> None:
        now = self.clock()
        if self._index.built_at and now - self._index.built_at < self.refresh_seconds:
            return
        runs: list[tuple[PastRun, list[str]]] = []
        for payload in self.load_payloads():
            run = past_run(payload)
            if run is None:
                continue
            words = tokens(str(payload.get("prompt") or "")[:MAX_PROMPT_CHARS])
            if words:
                runs.append((run, words))
        document_frequency: Counter[str] = Counter()
        for _, words in runs:
            document_frequency.update(set(words))
        total = len(runs)
        self._idf = {
            word: math.log((1 + total) / (1 + count)) + 1.0
            for word, count in document_frequency.items()
        }
        self._index = _Index(
            built_at=now,
            docs=[(run, self._vector(words)) for run, words in runs],
        )

    def _vector(self, words: list[str]) -> dict[str, float]:
        counts = Counter(words)
        # Unknown words carry no evidence: no past task shares them.
        vector = {word: count * self._idf[word] for word, count in counts.items() if word in self._idf}
        norm = math.sqrt(sum(value * value for value in vector.values()))
        return {word: value / norm for word, value in vector.items()} if norm else {}


def cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(word, 0.0) for word, value in left.items())
