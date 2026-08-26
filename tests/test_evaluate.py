import math

from saipet.evaluate import (
    Finding,
    LabelStore,
    RunRecord,
    compute_metrics,
    duplicate_rate,
    false_positives_per_day,
    precision_at,
    source_failure_rate,
    useful_findings_per_week,
)


def _build_store():
    store = LabelStore()
    # x: relevant, retrieved in both runs, high score both times
    store.label("a", "x", "relevant")
    # y: irrelevant, mid score
    store.label("a", "y", "irrelevant")
    # z: relevant, low score (below every band)
    store.label("a", "z", "relevant")
    # w: already solved, top score
    store.label("a", "w", "already_solved")
    return store


def _build_runs():
    return [
        RunRecord(
            run_id="r1",
            started_utc=0.0,
            attempted_sources=["a", "b"],
            failed_sources=["b"],
            findings=[
                Finding("a", "x", 90.0),
                Finding("a", "y", 70.0),
                Finding("a", "z", 50.0),
            ],
        ),
        RunRecord(
            run_id="r2",
            started_utc=86400.0,  # one day later
            attempted_sources=["a"],
            failed_sources=[],
            findings=[
                Finding("a", "x", 85.0),  # duplicate of x
                Finding("a", "w", 95.0),
            ],
        ),
    ]


def test_precision_at_80_counts_only_distinct_labelled_high_scorers():
    store = _build_store()
    runs = _build_runs()
    # >=80 distinct: x (relevant, tp), w (already_solved, tp); y/z below band.
    assert precision_at(runs, store, 80) == 1.0


def test_precision_at_60_includes_the_irrelevant_mid_scorer():
    store = _build_store()
    runs = _build_runs()
    # >=60 distinct: x (tp), y (irrelevant, fp), w (tp); z below band.
    assert math.isclose(precision_at(runs, store, 60), 2 / 3)


def test_precision_at_undefined_when_no_labelled_finding_in_band():
    store = _build_store()
    runs = _build_runs()
    # No labelled finding scores >= 99, so the band is undefined.
    assert precision_at(runs, store, 99) is None


def test_false_positives_per_day():
    store = _build_store()
    runs = _build_runs()
    # one irrelevant label (y); span is exactly one day.
    assert false_positives_per_day(runs, store) == 1.0


def test_useful_findings_per_week_extrapolates_from_span():
    store = _build_store()
    runs = _build_runs()
    # three positive labels (x, z, w) over a one-day span -> 3 * 7 per week.
    assert math.isclose(useful_findings_per_week(runs, store), 21.0)


def test_source_failure_rate():
    store = _build_store()
    runs = _build_runs()
    # attempted 3 (a,b + a), failed 1 (b).
    assert math.isclose(source_failure_rate(runs), 1 / 3)


def test_duplicate_rate():
    store = _build_store()
    runs = _build_runs()
    # four distinct identities, x appears in both runs -> 1/4 duplicated.
    assert math.isclose(duplicate_rate(runs), 0.25)


def test_compute_metrics_returns_all_six():
    store = _build_store()
    runs = _build_runs()
    metrics = compute_metrics(runs, store)
    assert set(metrics) == {
        "precision@80",
        "precision@60",
        "false_positives_per_day",
        "useful_findings_per_week",
        "source_failure_rate",
        "duplicate_rate",
    }
    assert metrics["precision@80"] == 1.0
    assert math.isclose(metrics["precision@60"], 2 / 3)
    assert metrics["false_positives_per_day"] == 1.0
    assert math.isclose(metrics["useful_findings_per_week"], 21.0)
    assert math.isclose(metrics["source_failure_rate"], 1 / 3)
    assert math.isclose(metrics["duplicate_rate"], 0.25)


def test_label_store_persists_and_reloads(tmp_path):
    path = tmp_path / "labels.json"
    store = LabelStore(path)
    store.label("a", "x", "relevant")
    store.save()

    reloaded = LabelStore(path)
    assert reloaded.get("a", "x") == "relevant"
    # Unknown labels are rejected.
    try:
        reloaded.label("a", "y", "bogus")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for unknown label")
    # Corrupt file is tolerated, not fatal.
    path.write_text("{not json", encoding="utf-8")
    assert LabelStore(path).all() == {}
