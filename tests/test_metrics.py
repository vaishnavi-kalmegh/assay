"""Metric formulas, checked against small examples worked out by hand."""

from __future__ import annotations

import pytest

from assay import metrics
from assay.evaluation import choose_threshold, f1


def test_ratio_basic_and_undefined():
    assert metrics.ratio(1, 4) == 0.25
    assert metrics.ratio(0, 5) == 0.0
    assert metrics.ratio(0, 0) is None  # undefined, not 0 and not 1


@pytest.mark.parametrize("numerator,denominator", [(-1, 3), (1, -3), (4, 3)])
def test_ratio_rejects_impossible_counts(numerator, denominator):
    with pytest.raises(ValueError):
        metrics.ratio(numerator, denominator)


# --- mapping quality -----------------------------------------------------------


def test_mapping_quality_hand_worked_example():
    # Predicted: A, B, C.  Labelled: A yes, B no, D yes, E no.
    # TP = A, FP = B, FN = D, TN = E; C was predicted but never labelled.
    predicted = {("R", "A"), ("R", "B"), ("R", "C")}
    labels = {
        ("R", "A"): True,
        ("R", "B"): False,
        ("R", "D"): True,
        ("R", "E"): False,
    }
    quality = metrics.mapping_quality(predicted, labels)
    assert (quality.true_positives, quality.false_positives) == (1, 1)
    assert (quality.false_negatives, quality.true_negatives) == (1, 1)
    assert quality.unlabelled_predictions == 1
    assert quality.precision == 0.5
    assert quality.recall == 0.5


def test_mapping_quality_perfect_and_worst():
    labels = {("R", "A"): True, ("R", "B"): False}
    perfect = metrics.mapping_quality({("R", "A")}, labels)
    assert (perfect.precision, perfect.recall) == (1.0, 1.0)
    worst = metrics.mapping_quality({("R", "B")}, labels)
    assert (worst.precision, worst.recall) == (0.0, 0.0)


def test_mapping_quality_empty_sets_are_undefined_not_zero():
    labels = {("R", "A"): True, ("R", "B"): False}
    nothing_predicted = metrics.mapping_quality(set(), labels)
    assert nothing_predicted.precision is None
    assert nothing_predicted.recall == 0.0
    no_positives = metrics.mapping_quality({("R", "B")}, {("R", "B"): False})
    assert no_positives.precision == 0.0
    assert no_positives.recall is None
    empty = metrics.mapping_quality(set(), {})
    assert (empty.precision, empty.recall) == (None, None)


def test_mapping_quality_ignores_unlabelled_predictions():
    quality = metrics.mapping_quality({("R", "X")}, {("R", "A"): True})
    assert quality.unlabelled_predictions == 1
    assert quality.precision is None  # the only prediction cannot be judged
    assert quality.recall == 0.0


# --- requirement coverage ------------------------------------------------------


def _result(outcome, *requirements):
    return {"outcome": outcome, "requirements": list(requirements)}


def test_requirement_coverage_hand_worked_example():
    # R1 has a passing test, R2 only a failing one, R3 none, R4 passes via a test
    # that is also tagged R1. Covered = {R1, R4}: 2 of 4.
    results = [
        _result("passed", "R1", "R4"),
        _result("failed", "R2"),
        _result("passed"),  # an untagged passing test proves nothing
    ]
    coverage = metrics.requirement_coverage(["R1", "R2", "R3", "R4"], results)
    assert coverage.covered == ("R1", "R4")
    assert coverage.mapped_not_passing == ("R2",)
    assert coverage.not_mapped == ("R3",)
    assert coverage.criteria == 4
    assert coverage.coverage == 0.5


def test_one_passing_test_is_enough_even_if_another_fails():
    results = [_result("failed", "R1"), _result("passed", "R1")]
    coverage = metrics.requirement_coverage(["R1"], results)
    assert coverage.covered == ("R1",)
    assert coverage.mapped_not_passing == ()


def test_error_and_not_run_do_not_cover():
    results = [_result("error", "R1"), _result("not_run", "R2")]
    coverage = metrics.requirement_coverage(["R1", "R2"], results)
    assert coverage.covered == ()
    assert coverage.coverage == 0.0


def test_coverage_ignores_tags_outside_the_scope_and_dedupes_ids():
    coverage = metrics.requirement_coverage(["R1", "R1"], [_result("passed", "R9")])
    assert coverage.criteria == 1
    assert coverage.covered == ()


def test_coverage_with_no_criteria_is_undefined():
    assert metrics.requirement_coverage([], []).coverage is None


# --- mutation score ------------------------------------------------------------


def test_mutation_score_hand_worked_example():
    killed = {"a": True, "b": False, "c": True, "d": True}
    plain = metrics.mutation_score(killed)
    assert (plain.executed, plain.killed, plain.score) == (4, 3, 0.75)
    assert plain.survived == ("b",)
    # Declaring the survivor equivalent removes it from the denominator.
    excluded = metrics.mutation_score(killed, equivalent={"b"})
    assert (excluded.executed, excluded.killed, excluded.score) == (3, 3, 1.0)
    assert excluded.excluded_equivalent == ("b",)


def test_mutation_score_edge_cases():
    assert metrics.mutation_score({}).score is None
    assert metrics.mutation_score({"a": True}, equivalent={"a"}).score is None
    assert metrics.mutation_score({"a": False}).score == 0.0
    with pytest.raises(ValueError):
        metrics.mutation_score({"a": True}, equivalent={"ghost"})


# --- agreement and threshold choice -------------------------------------------


def test_cohens_kappa_hand_worked_example():
    # 10 pairs; labeller says yes to 5 both times but flips two pairs.
    first = {("R", str(i)): i < 5 for i in range(10)}
    second = dict(first)
    second[("R", "0")] = False  # yes -> no
    second[("R", "9")] = True  # no -> yes
    # observed 0.8; each labelling is 50% yes so chance agreement is 0.5.
    assert metrics.cohens_kappa(first, second) == pytest.approx(0.6)
    assert metrics.cohens_kappa(first, first) == 1.0


def test_cohens_kappa_undefined_cases():
    assert metrics.cohens_kappa({}, {}) is None
    assert metrics.cohens_kappa({("R", "A"): True}, {("R", "B"): True}) is None
    same = {("R", "A"): True, ("R", "B"): True}
    assert metrics.cohens_kappa(same, same) is None  # chance agreement is 1


def test_f1():
    assert f1(0.5, 0.5) == 0.5
    assert f1(1.0, 0.5) == pytest.approx(2 / 3)
    assert f1(0.0, 0.0) == 0.0
    assert f1(None, 0.5) is None
    assert f1(0.5, None) is None


def test_choose_threshold_prefers_best_f1_then_higher_threshold():
    sweep = [
        {"threshold": 0.2, "f1": 0.6},
        {"threshold": 0.4, "f1": 0.8},
        {"threshold": 0.6, "f1": 0.8},
        {"threshold": 0.8, "f1": None},  # undefined F1 never wins
    ]
    assert choose_threshold(sweep) == 0.6
    assert choose_threshold([{"threshold": 0.5, "f1": None}]) is None
    assert choose_threshold([]) is None
