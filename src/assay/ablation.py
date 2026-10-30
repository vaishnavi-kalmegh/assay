"""Ablation: what do the hard rules and what does TF-IDF each contribute?

Three ways of predicting which tests verify which criterion are scored against the
same hand labels:

* **rules only**: every test that passes the hard eligibility rules is a match, with
  no similarity score and no threshold (the explicit-status rule is one of those
  rules, so it is kept);
* **rules + TF-IDF**: the configured mapper (rules, then TF-IDF above the threshold,
  top-k);
* **TF-IDF only**: cosine similarity over all tests, no eligibility rules, same
  threshold and top-k.

Limits that apply to every number here: 40 labelled pairs, 5 positives, one
labeller, a sample stratified on the mapper's own eligibility (so it is not a random
draw of all pairs), and rules written after seeing labelled errors. Pairs a variant
predicts that were never labelled are counted but cannot be scored, so precision is
over labelled pairs only. The result is a description of this sample, not an estimate
for other APIs.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from assay import metrics
from assay.mapping import (
    Criterion,
    MappableTest,
    _similarity_matrix,
    map_requirements,
)

VARIANTS = ("rules_only", "rules_tfidf", "tfidf_only")
LABELS = {
    "rules_only": "Rules only",
    "rules_tfidf": "Rules + TF-IDF (configured)",
    "tfidf_only": "TF-IDF only",
}
SPLITS = ("dev", "test")


def _rules_only(
    criteria: Sequence[Criterion], tests: Sequence[MappableTest]
) -> set[metrics.Pair]:
    result = map_requirements(criteria, tests, threshold=0.0, top_k=max(len(tests), 1))
    return {
        (row.requirement_id, candidate.case_id)
        for row in result.rows
        for candidate in row.candidates
    }


def _rules_tfidf(
    criteria: Sequence[Criterion],
    tests: Sequence[MappableTest],
    threshold: float,
    top_k: int,
) -> set[metrics.Pair]:
    result = map_requirements(criteria, tests, threshold=threshold, top_k=top_k)
    return {
        (row.requirement_id, match.case_id)
        for row in result.rows
        for match in row.matches
    }


def _tfidf_only(
    criteria: Sequence[Criterion],
    tests: Sequence[MappableTest],
    threshold: float,
    top_k: int,
) -> set[metrics.Pair]:
    ordered_criteria = sorted(criteria, key=lambda item: item.id)
    ordered_tests = sorted(tests, key=lambda item: item.case_id)
    scores = _similarity_matrix(ordered_criteria, ordered_tests)
    predicted: set[metrics.Pair] = set()
    for row_index, criterion in enumerate(ordered_criteria):
        ranked = sorted(
            (
                (round(float(scores[row_index, column]), 4), test.case_id)
                for column, test in enumerate(ordered_tests)
            ),
            key=lambda item: (-item[0], item[1]),
        )
        above = [case_id for score, case_id in ranked if score >= threshold]
        predicted.update((criterion.id, case_id) for case_id in above[:top_k])
    return predicted


def predictions(
    variant: str,
    criteria: Sequence[Criterion],
    tests: Sequence[MappableTest],
    *,
    threshold: float,
    top_k: int,
) -> set[metrics.Pair]:
    """The set of (criterion, test) pairs a variant predicts over all pairs."""
    if variant == "rules_only":
        return _rules_only(criteria, tests)
    if variant == "rules_tfidf":
        return _rules_tfidf(criteria, tests, threshold, top_k)
    if variant == "tfidf_only":
        return _tfidf_only(criteria, tests, threshold, top_k)
    raise ValueError(f"unknown variant {variant!r}")


def _f1(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None or precision + recall == 0:
        return None
    return 2 * precision * recall / (precision + recall)


def _scored(
    predicted: set[metrics.Pair], labels: dict[metrics.Pair, bool]
) -> dict[str, Any]:
    quality = metrics.mapping_quality(predicted, labels)
    return {
        "true_positives": quality.true_positives,
        "false_positives": quality.false_positives,
        "false_negatives": quality.false_negatives,
        "true_negatives": quality.true_negatives,
        "unlabelled_predictions": quality.unlabelled_predictions,
        "precision": quality.precision,
        "recall": quality.recall,
        "f1": _f1(quality.precision, quality.recall),
    }


def evaluate_ablation(
    label_rows: Sequence[dict[str, str]],
    criteria: Sequence[Criterion],
    tests: Sequence[MappableTest],
    *,
    threshold: float,
    top_k: int,
) -> dict[str, Any]:
    """Score the three variants on all labelled pairs and on each split."""
    by_split: dict[str, dict[metrics.Pair, bool]] = {name: {} for name in SPLITS}
    for row in label_rows:
        by_split[row["split"]][(row["requirement_id"], row["case_id"])] = (
            row["label"] == "1"
        )
    everything = {**by_split["dev"], **by_split["test"]}
    report: dict[str, Any] = {
        "configured": {"threshold": threshold, "top_k": top_k},
        "labelled_pairs": len(everything),
        "positives": sum(everything.values()),
        "variants": {},
    }
    for variant in VARIANTS:
        predicted = predictions(
            variant, criteria, tests, threshold=threshold, top_k=top_k
        )
        report["variants"][variant] = {
            "predicted_pairs_overall": len(predicted),
            "all": _scored(predicted, everything),
            "dev": _scored(predicted, by_split["dev"]),
            "test": _scored(predicted, by_split["test"]),
        }
    return report


def _fmt(value: float | None) -> str:
    return "undefined" if value is None else f"{value:.3f}"


def ablation_markdown(report: dict[str, Any]) -> str:
    cfg = report["configured"]
    lines = [
        "# Ablation: rules versus TF-IDF",
        "",
        f"{report['labelled_pairs']} labelled pairs, {report['positives']} positive. "
        f"Threshold {cfg['threshold']}, top-k {cfg['top_k']} where they apply.",
        "",
        "| variant | TP | FP | FN | TN | precision | recall | F1 | "
        "pairs predicted over all criteria x tests |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for variant in VARIANTS:
        item = report["variants"][variant]
        s = item["all"]
        lines.append(
            f"| {LABELS[variant]} | {s['true_positives']} | {s['false_positives']} | "
            f"{s['false_negatives']} | {s['true_negatives']} | "
            f"{_fmt(s['precision'])} | {_fmt(s['recall'])} | {_fmt(s['f1'])} | "
            f"{item['predicted_pairs_overall']} |"
        )
    lines += [
        "",
        "## By split",
        "",
        "| variant | split | precision | recall |",
        "|---|---|---|---|",
    ]
    for variant in VARIANTS:
        for split in SPLITS:
            s = report["variants"][variant][split]
            lines.append(
                f"| {LABELS[variant]} | {split} | {_fmt(s['precision'])} | "
                f"{_fmt(s['recall'])} |"
            )
    lines += [
        "",
        "## Limits",
        "",
        "- 40 labelled pairs, 5 positives, one labeller. One positive moves recall "
        "by 0.2.",
        "- The sample is stratified on the mapper's own eligibility, so it is not a "
        "random draw of all pairs and precision here is not the precision over every "
        "criterion-test pair. Predicted pairs that were never labelled cannot be "
        "scored (the last column shows how many each variant predicts in total).",
        "- The rules were written after seeing labelled errors, which favours the "
        "rule-based variants on these labels.",
        "- Read the contrast between variants, not any single number.",
        "",
    ]
    return "\n".join(lines)
