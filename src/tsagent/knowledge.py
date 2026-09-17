"""Metric catalogue and PromQL query library.

Two pieces of operator knowledge, both plain files that a human maintains:

* a **metric catalogue** — the metric names this Prometheus actually exposes, one per line
* a **query library** — PromQL that operators already trust, keyed by what it measures

Together they replace guesswork. The library gives the agent worked examples in the right metric
namespace, and the catalogue lets a generated query be rejected *before* execution when it names
a metric that does not exist. That check is deterministic: no model call, no retry loop, and a
wrong metric can no longer come back as "empty result" and be mistaken for a healthy system.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from tsagent.tools.prometheus import metric_names_in

logger = logging.getLogger(__name__)

# Underscores are separators, not word characters: `hugepage_utilisation` has to tokenize into
# {hugepage, utilisation} for a goal phrased as "hugepage usage" to reach it.
_WORD = re.compile(r"[a-zA-Z0-9]+")

# Tokens that carry no signal when matching a goal to a library entry.
_STOPWORDS = frozenset(
    {
        "the", "a", "an", "is", "are", "any", "all", "and", "or", "of", "for", "on", "in", "to",
        "by", "check", "verify", "examine", "review", "monitor", "inspect", "current", "across",
        "node", "nodes", "host", "hosts", "usage", "status", "if", "has", "have", "with", "over",
        "time", "there", "issues", "issue", "from", "this", "that", "its",
    }
)


def _tokens(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text or "") if len(w) > 2} - _STOPWORDS


def _overlap(wanted: set[str], available: set[str]) -> int:
    """Count matching tokens, treating one as a prefix of the other as a match.

    Operators write "hugepage usage" and the metric is `hugepages`; "memory" and `mem` mean the
    same thing. Exact set intersection misses both, which is most of the vocabulary that matters.
    """
    score = 0
    for token in wanted:
        for candidate in available:
            if token == candidate or (
                len(token) >= 3
                and len(candidate) >= 3
                and (token.startswith(candidate) or candidate.startswith(token))
            ):
                score += 1
                break
    return score


@dataclass
class LibraryEntry:
    name: str
    promql: str
    keywords: list[str] = field(default_factory=list)

    @property
    def metrics(self) -> list[str]:
        """Real metric names, derived from the PromQL rather than trusted from the file.

        The `metric_names` field in the library is a token dump — it contains things like `by`,
        `on` and `host` — so it is only useful for search, not for validation.
        """
        return metric_names_in(self.promql)

    def score(self, text: str) -> int:
        goal = _tokens(text)
        if not goal:
            return 0
        available = _tokens(self.name) | {t for k in self.keywords for t in _tokens(k)}
        return _overlap(goal, available)


class PromQLLibrary:
    """Known-good PromQL, keyed by what it measures."""

    def __init__(self, entries: list[LibraryEntry] | None = None):
        self.entries = entries or []

    @classmethod
    def load(cls, path: Path | str) -> PromQLLibrary:
        path = Path(path)
        if not path.exists():
            return cls()
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as exc:
            logger.warning("skipping malformed PromQL library %s: %s", path, exc)
            return cls()

        entries: list[LibraryEntry] = []
        for name, body in (data.get("queries") or {}).items():
            if not isinstance(body, dict) or not body.get("promql"):
                logger.warning("library entry %r has no promql, skipping", name)
                continue
            entries.append(
                LibraryEntry(
                    name=str(name),
                    promql=str(body["promql"]).strip(),
                    keywords=[str(k) for k in (body.get("metric_names") or [])],
                )
            )
        logger.info("loaded %d PromQL library entries from %s", len(entries), path)
        return cls(entries)

    def search(self, text: str, limit: int = 6) -> list[LibraryEntry]:
        """Entries most relevant to a plan goal, best first."""
        scored = [(e.score(text), e) for e in self.entries]
        hits = sorted(
            (pair for pair in scored if pair[0] > 0), key=lambda p: (-p[0], p[1].name)
        )
        return [entry for _, entry in hits[:limit]]

    def search_many(self, texts: list[str], limit: int = 8) -> list[LibraryEntry]:
        """Best entries across several goals, round-robin so every goal gets an example.

        Taking a global top-N instead would let one well-covered goal crowd out the rest — a
        plan mentioning CPU pulls in eight cpu_utilization_by_* variants and the hugepage goal,
        which is the one that actually fired, gets no example at all.
        """
        ranked_per_goal = [self.search(text, limit=limit) for text in texts if text.strip()]
        picked: list[LibraryEntry] = []
        seen: set[str] = set()
        for depth in range(limit):
            for candidates in ranked_per_goal:
                if depth >= len(candidates):
                    continue
                entry = candidates[depth]
                if entry.name in seen:
                    continue
                seen.add(entry.name)
                picked.append(entry)
                if len(picked) >= limit:
                    return picked
        return picked

    def render(self, entries: list[LibraryEntry]) -> str:
        """Format entries as worked examples for a prompt."""
        if not entries:
            return ""
        lines = ["Known-good queries from this platform's library — prefer adapting these:"]
        lines.extend(f"- {entry.name}:\n    {entry.promql}" for entry in entries)
        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self.entries)


class MetricCatalogue:
    """The metric names a Prometheus actually exposes.

    Generate with:
        curl -sk -H "Authorization: Bearer $TOKEN" \\
            https://<prom>/api/v1/label/__name__/values | jq -r '.data[]' > metrics.txt
    """

    def __init__(self, names: list[str] | None = None):
        self.names: list[str] = names or []
        self._set = set(self.names)

    @classmethod
    def load(cls, path: Path | str) -> MetricCatalogue:
        path = Path(path)
        if not path.exists():
            return cls()
        names = [
            line.strip()
            for line in path.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
        logger.info("loaded %d metric names from %s", len(names), path)
        return cls(names)

    def __bool__(self) -> bool:
        return bool(self.names)

    def __len__(self) -> int:
        return len(self.names)

    def exists(self, name: str) -> bool:
        return name in self._set

    def unknown_metrics(self, promql: str) -> list[str]:
        """Metric names in ``promql`` that this Prometheus does not expose.

        Empty when the catalogue is empty — an unconfigured catalogue must not block queries.
        """
        if not self.names:
            return []
        return [name for name in metric_names_in(promql) if not self.exists(name)]

    def search(self, text: str, limit: int = 40) -> list[str]:
        """Catalogue entries relevant to some text, best first.

        Never returns the whole catalogue: a real STF Prometheus exposes thousands of names, and
        an 8B model on CPU cannot afford to read them.
        """
        wanted = _tokens(text)
        if not wanted:
            return self.names[:limit]
        scored: list[tuple[int, str]] = []
        for name in self.names:
            overlap = _overlap(wanted, _tokens(name))
            if overlap:
                scored.append((-overlap, name))
        return [name for _, name in sorted(scored)[:limit]]

    def suggest(self, unknown: str, limit: int = 5) -> list[str]:
        """Plausible replacements for a metric name that does not exist."""
        return self.search(unknown.replace("_", " "), limit=limit)


@dataclass
class Knowledge:
    """Everything the PromQL agent knows about one cluster's metrics."""

    catalogue: MetricCatalogue = field(default_factory=MetricCatalogue)
    library: PromQLLibrary = field(default_factory=PromQLLibrary)

    @classmethod
    def load(cls, metrics_file: Path | str | None, library_file: Path | str | None) -> Knowledge:
        return cls(
            catalogue=MetricCatalogue.load(metrics_file) if metrics_file else MetricCatalogue(),
            library=PromQLLibrary.load(library_file) if library_file else PromQLLibrary(),
        )

    def prompt_context(self, goals: list[str], limit_metrics: int = 40) -> str:
        """Library examples plus candidate metric names, scoped to the goals at hand."""
        blocks: list[str] = []
        entries = self.library.search_many(goals)
        if entries:
            blocks.append(self.library.render(entries))
        if self.catalogue:
            candidates = self.catalogue.search(" ".join(goals), limit=limit_metrics)
            if candidates:
                blocks.append(
                    "Metrics that exist on this Prometheus (use ONLY names from this list):\n"
                    + ", ".join(candidates)
                )
        return "\n\n".join(blocks)
