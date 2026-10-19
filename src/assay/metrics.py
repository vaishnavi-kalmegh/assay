"""Evaluation metrics for assay. Pure functions: no files, no pytest, no network.

Three different questions get three different metrics, and they are never merged
into one number:

* mapping quality   - does the mapper link criteria to the right tests?
* requirement coverage - does each criterion have a passing, tagged test?
* mutation score    - do the tests notice when the API's behaviour changes?

A ratio with an empty denominator is *undefined*, not 0 and not 1. Functions return
None in that case so a report can say "undefined" instead of a misleading number.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

# A pair is (requirement_id, case_id).
Pair = tuple[str, str]


def ratio(numerator: int, denominator: int) -> float | None:
    """numerator / denominator, or None when the denominator is zero."""
    if denominator < 0 or numerator < 0:
        raise ValueError("counts must not be negative")
    if numerator > denominator:
        raise ValueError("numerator cannot exceed denominator")
    return None if denominator == 0 else numerator / denominator


@dataclass(frozen=True)
class MappingQuality:
    """Mapper precision and recall measured on a labelled sample of pairs.

    Only pairs that carry a human label can be scored. Predictions on unlabelled
    pairs are counted in ``unlabelled_predictions`` and left out of the ratios.
    The numbers describe this sample; if the sample is stratified rather than
    random they are not estimates for every possible pair.
    """

    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int
    unlabelled_predictions: int
    precision: float | None
    recall: float | None


def mapping_quality(
    predicted: Collection[Pair], labels: Mapping[Pair, bool]
) -> MappingQuality:
    """Precision and recall of ``predicted`` pairs against ``labels``.

    ``labels`` maps a pair to True (a test that verifies the criterion) or False.
    precision = TP / (TP + FP)   undefined when nothing labelled was predicted
    recall    = TP / (TP + FN)   undefined when no labelled pair is positive
    """
    predicted_set = set(predicted)
    true_positives = sum(
        1 for pair, ok in labels.items() if ok and pair in predicted_set
    )
    false_positives = sum(
        1 for pair, ok in labels.items() if not ok and pair in predicted_set
    )
    false_negatives = sum(
        1 for pair, ok in labels.items() if ok and pair not in predicted_set
    )
    true_negatives = sum(
        1 for pair, ok in labels.items() if not ok and pair not in predicted_set
    )
    return MappingQuality(
        true_positives=true_positives,
        false_positives=false_positives,
        false_negatives=false_negatives,
        true_negatives=true_negatives,
        unlabelled_predictions=len(predicted_set - set(labels)),
        precision=ratio(true_positives, true_positives + false_positives),
        recall=ratio(true_positives, true_positives + false_negatives),
    )


@dataclass(frozen=True)
class Coverage:
    """Requirement coverage. Mapped-but-failing criteria are reported apart."""

    criteria: int
    covered: tuple[str, ...]
    mapped_not_passing: tuple[str, ...]
    not_mapped: tuple[str, ...]
    coverage: float | None


def requirement_coverage(
    criteria_ids: Iterable[str], results: Iterable[Mapping[str, Any]]
) -> Coverage:
    """Share of criteria with at least one passing test tagged with the criterion.

    ``results`` are the runner's result rows: each has ``outcome`` and
    ``requirements``. A criterion that has tagged tests but none passing is listed
    in ``mapped_not_passing`` and does not count as covered.
    """
    ids = sorted(set(criteria_ids))
    passing: set[str] = set()
    tagged: set[str] = set()
    for result in results:
        for requirement in result.get("requirements", []):
            tagged.add(requirement)
            if result.get("outcome") == "passed":
                passing.add(requirement)
    covered = tuple(item for item in ids if item in passing)
    mapped_not_passing = tuple(
        item for item in ids if item in tagged and item not in passing
    )
    not_mapped = tuple(item for item in ids if item not in tagged)
    return Coverage(
        criteria=len(ids),
        covered=covered,
        mapped_not_passing=mapped_not_passing,
        not_mapped=not_mapped,
        coverage=ratio(len(covered), len(ids)),
    )


@dataclass(frozen=True)
class MutationScore:
    """Mutation score over valid, non-equivalent mutants."""

    executed: int
    killed: int
    survived: tuple[str, ...]
    excluded_equivalent: tuple[str, ...]
    score: float | None


def mutation_score(
    killed_by_mutant: Mapping[str, bool], equivalent: Collection[str] = ()
) -> MutationScore:
    """killed / executed, ignoring mutants declared equivalent to the real API.

    ``killed_by_mutant`` maps each executed mutant to whether at least one test
    detected it. An equivalent mutant changes no observable behaviour, so no test
    can kill it and it must not count against the suite.
    """
    unknown = sorted(set(equivalent) - set(killed_by_mutant))
    if unknown:
        raise ValueError(f"equivalent mutants were not executed: {unknown}")
    valid = {
        name: killed
        for name, killed in killed_by_mutant.items()
        if name not in set(equivalent)
    }
    killed = sum(1 for was_killed in valid.values() if was_killed)
    return MutationScore(
        executed=len(valid),
        killed=killed,
        survived=tuple(sorted(name for name, k in valid.items() if not k)),
        excluded_equivalent=tuple(sorted(set(equivalent))),
        score=ratio(killed, len(valid)),
    )


def cohens_kappa(
    first: Mapping[Pair, bool], second: Mapping[Pair, bool]
) -> float | None:
    """Agreement between two labellings of the same pairs, corrected for chance.

    Used for self-agreement: the same person labels a subset twice, days apart.
    Returns None when the pairs differ, when there are none, or when agreement by
    chance is already 1 (kappa is undefined then).
    """
    if set(first) != set(second) or not first:
        return None
    total = len(first)
    observed = sum(1 for pair in first if first[pair] == second[pair]) / total
    first_yes = sum(first.values()) / total
    second_yes = sum(second.values()) / total
    expected = first_yes * second_yes + (1 - first_yes) * (1 - second_yes)
    if expected == 1:
        return None
    return (observed - expected) / (1 - expected)
