"""Shadow evaluation harness (T-033).

The scout retrieves candidates by substring signal matching; T-033 measures
whether that retrieval is actually worth running. A human labels findings
(relevant / irrelevant / already_solved / promo_inappropriate); the harness
computes retrieval-quality and operational metrics over labelled report runs
so a decision about the scout's value is evidence-based, not vibes.

The harness is a *shadow*: it never changes what the scout does. It only
reads report runs and human labels and reports the numbers.
"""

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from saipet.report import list_complete_reports

LABEL_RELEVANT = "relevant"
LABEL_IRRELEVANT = "irrelevant"
LABEL_ALREADY_SOLVED = "already_solved"
LABEL_PROMO_INAPPROPRIATE = "promo_inappropriate"

_LABELS = frozenset(
    {
        LABEL_RELEVANT,
        LABEL_IRRELEVANT,
        LABEL_ALREADY_SOLVED,
        LABEL_PROMO_INAPPROPRIATE,
    }
)
_POSITIVE = frozenset({LABEL_RELEVANT, LABEL_ALREADY_SOLVED})
_NEGATIVE = frozenset({LABEL_IRRELEVANT, LABEL_PROMO_INAPPROPRIATE})

_DAY_SECONDS = 86400.0


@dataclass(frozen=True)
class Finding:
    source: str
    id: str
    score: float = 0.0

    def identity(self) -> tuple[str, str]:
        return (self.source, self.id)


@dataclass
class RunRecord:
    """One scout run, flattened for evaluation."""

    run_id: str
    started_utc: float
    findings: list = field(default_factory=list)  # list[Finding]
    failed_sources: list = field(default_factory=list)
    attempted_sources: list = field(default_factory=list)


class LabelStore:
    """Persists human labels keyed by canonical (source, id) identity.

    The same finding can be retrieved by many runs; a human labels the
    finding once, not once per run, so labels are keyed by identity rather
    than by run+finding.
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else None
        self._labels: dict[tuple[str, str], str] = {}
        if self.path is not None and self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {}
            if isinstance(raw, dict):
                for key, value in raw.items():
                    if value not in _LABELS:
                        continue
                    try:
                        source, candidate_id = json.loads(key)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        continue
                    if isinstance(source, str) and isinstance(candidate_id, str):
                        self._labels[(source, candidate_id)] = value

    def label(self, source: str, candidate_id: str, label: str) -> None:
        if label not in _LABELS:
            raise ValueError(f"unknown label {label!r}")
        self._labels[(source, candidate_id)] = label

    def get(self, source: str, candidate_id: str) -> str | None:
        return self._labels.get((source, candidate_id))

    def all(self) -> dict[tuple[str, str], str]:
        return dict(self._labels)

    def save(self) -> None:
        if self.path is None:
            raise RuntimeError("LabelStore has no path")
        payload = {
            json.dumps([source, candidate_id], ensure_ascii=False): label
            for (source, candidate_id), label in self._labels.items()
        }
        self.path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def _is_positive(label: str) -> bool | None:
    if label in _POSITIVE:
        return True
    if label in _NEGATIVE:
        return False
    return None


def _span_days(runs: list[RunRecord]) -> float:
    if not runs:
        return 1.0
    starts = [r.started_utc for r in runs]
    span = max(starts) - min(starts)
    return max(span / _DAY_SECONDS, 1.0)


def precision_at(runs: list[RunRecord], store: LabelStore, threshold: float) -> float | None:
    """Fraction of DISTINCT retrieved findings scoring >= `threshold` that a
    human labelled positive (relevant or already_solved).

    Returns None when no labelled finding falls in the band (undefined), so a
    caller never mistakes "nobody labelled the high-scorers" for "they were
    all good".
    """
    best: dict[tuple[str, str], float] = {}
    for run in runs:
        for f in run.findings:
            key = f.identity()
            if key not in best or f.score > best[key]:
                best[key] = f.score
    tp = fp = 0
    for identity, score in best.items():
        if score < threshold:
            continue
        label = store.get(*identity)
        if label is None:
            continue
        positive = _is_positive(label)
        if positive is True:
            tp += 1
        elif positive is False:
            fp += 1
    if tp + fp == 0:
        return None
    return tp / (tp + fp)


def _distinct_identities(runs: list[RunRecord]) -> dict[tuple[str, str], float]:
    """Canonical identity -> best (highest) score seen across all runs."""
    best: dict[tuple[str, str], float] = {}
    for run in runs:
        for f in run.findings:
            key = f.identity()
            if key not in best or f.score > best[key]:
                best[key] = f.score
    return best


def false_positives_per_day(runs: list[RunRecord], store: LabelStore) -> float:
    fp = sum(
        1
        for identity in _distinct_identities(runs)
        if store.get(*identity) in _NEGATIVE
    )
    return fp / _span_days(runs)


def useful_findings_per_week(runs: list[RunRecord], store: LabelStore) -> float:
    tp = sum(
        1
        for identity in _distinct_identities(runs)
        if store.get(*identity) in _POSITIVE
    )
    return tp / (_span_days(runs) / 7.0)


def source_failure_rate(runs: list[RunRecord]) -> float:
    attempted = sum(len(r.attempted_sources) for r in runs)
    failed = sum(len(r.failed_sources) for r in runs)
    if attempted == 0:
        return 0.0
    return failed / attempted


def duplicate_rate(runs: list[RunRecord]) -> float:
    """Fraction of DISTINCT retrieved identities that appeared in more than
    one run -- a measure of how much the scout re-surfaces the same thing."""
    seen: dict[tuple[str, str], int] = defaultdict(int)
    for run in runs:
        for f in run.findings:
            seen[f.identity()] += 1
    if not seen:
        return 0.0
    dup = sum(1 for count in seen.values() if count > 1)
    return dup / len(seen)


def compute_metrics(runs: list[RunRecord], store: LabelStore) -> dict:
    """All T-033 metrics in one call, ready for a dashboard or a diff."""
    return {
        "precision@80": precision_at(runs, store, 80),
        "precision@60": precision_at(runs, store, 60),
        "false_positives_per_day": false_positives_per_day(runs, store),
        "useful_findings_per_week": useful_findings_per_week(runs, store),
        "source_failure_rate": source_failure_rate(runs),
        "duplicate_rate": duplicate_rate(runs),
    }


_NOT_FETCHED_RE = re.compile(r"^##\s+Not fetched\s*$", re.MULTILINE)


def runs_from_report_dir(directory: str | Path) -> list[RunRecord]:
    """Build evaluation RunRecords from real report runs on disk.

    Findings (source/id/score) come from each run's JSONL; attempted and
    failed sources are reconstructed from the markdown (the "## Not fetched"
    block names failures; attempted are not recorded there, so attempted
    falls back to the set of sources seen in this run's findings)."""
    runs: list[RunRecord] = []
    for jsonl_path in list_complete_reports(directory):
        run_id = jsonl_path.stem
        findings: list[Finding] = []
        md_path = jsonl_path.with_suffix(".md")
        failed: list[str] = []
        if md_path.exists():
            text = md_path.read_text(encoding="utf-8")
            if _NOT_FETCHED_RE.search(text):
                tail = text.split(_NOT_FETCHED_RE)[-1]
                for line in tail.splitlines():
                    m = re.match(r"^\s*-\s*(.+?)\s*:", line)
                    if m:
                        failed.append(m.group(1).strip())
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            src = record.get("source")
            cid = record.get("id")
            if not isinstance(src, str) or not isinstance(cid, str):
                continue
            score = float(record.get("score", 0.0))
            findings.append(Finding(source=src, id=cid, score=score))
        attempted = sorted({f.source for f in findings})
        runs.append(
            RunRecord(
                run_id=run_id,
                started_utc=jsonl_path.stat().st_mtime,
                findings=findings,
                failed_sources=failed,
                attempted_sources=attempted,
            )
        )
    return runs
