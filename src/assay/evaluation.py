"""Evaluation harness: labelled-pair sampling, mapping evaluation, mutant runs.

Jobs, kept separate because they answer different questions:

* ``sample``    - draw pairs for a human to label, blind to the mapper's scores.
* ``mapping``   - score the mapper against the human labels (precision / recall).
* ``relabel``   - write a blank sheet of a subset of the pairs, to label again later.
* ``agreement`` - compare the two labellings (self-agreement, Cohen's kappa).
* ``mutants``   - run the generated suite against switchable demo API mutants.

Nothing here calls a network service or a language model.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from assay import metrics
from assay.ablation import LABELS as ABLATION_LABELS
from assay.ablation import VARIANTS, ablation_markdown, evaluate_ablation
from assay.demo_api import MUTANTS
from assay.mapping import (
    Criterion,
    MappableTest,
    MappingError,
    MappingResult,
    baseline_tests_from_spec,
    load_criteria,
    map_requirements,
)
from assay.runner import export_tests, load_spec, run_tests

DEFAULT_SEED = 1
DEFAULT_RELABEL_SIZE = 20
# How many pairs to draw from each stratum. Strata use only structural facts (does
# the pair share an operation, did it pass the mapper's eligibility rules) and
# never the similarity score, so the sample is blind to the thing being measured.
DEFAULT_COUNTS = {"eligible": 20, "same_operation": 12, "other_operation": 8}
SPLITS = ("dev", "test")
SWEEP_THRESHOLDS = tuple(round(step * 0.05, 2) for step in range(0, 21))

LABEL_COLUMNS = [
    "pair_id",
    "split",
    "requirement_id",
    "criterion_description",
    "criterion_field",
    "criterion_constraint",
    "criterion_value",
    "criterion_expected_status",
    "case_id",
    "test_name",
    "test_kind",
    "test_field",
    "test_expected_status",
    "test_checks",
    "label",
    "notes",
]


class EvaluationError(ValueError):
    """Raised for unusable labels, mutants, or an invalid control run."""


# --- fingerprint --------------------------------------------------------------


def case_id_fingerprint(tests: Sequence[MappableTest]) -> str:
    """A hash of the sorted case IDs. Labels are only valid for this exact set."""
    joined = "\n".join(sorted(test.case_id for test in tests))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _frozen_columns(row: dict[str, str]) -> dict[str, str]:
    """Every column except the two a person fills in."""
    return {k: row[k] for k in LABEL_COLUMNS if k not in ("label", "notes")}


def pair_fingerprints(rows: Sequence[dict[str, str]]) -> dict[str, str]:
    """pair ID -> hash of that pair's frozen columns, including its dev/test split.

    Stored in the sheet metadata so the loader can refuse a sheet in which a pair
    moved between halves or a descriptive column was edited.
    """
    return {
        row["pair_id"]: hashlib.sha256(
            json.dumps(_frozen_columns(row), sort_keys=True).encode("utf-8")
        ).hexdigest()
        for row in rows
    }


# --- sampling -----------------------------------------------------------------


def _pair_id(requirement_id: str, case_id: str) -> str:
    return f"{requirement_id}|{case_id}"


def _stable_key(seed: int, pair_id: str) -> str:
    """A seed-dependent but Python-version-independent ordering key."""
    return hashlib.sha256(f"{seed}:{pair_id}".encode()).hexdigest()


def classify_pairs(
    criteria: Sequence[Criterion],
    tests: Sequence[MappableTest],
    result: MappingResult,
) -> dict[tuple[str, str], str]:
    """Stratum of every (criterion, test) pair.

    eligible        - the mapper's rules let this test compete for the criterion
    same_operation  - same method and path, but the rules ruled the test out
    other_operation - a different endpoint
    """
    # Keep the evaluation sample independent of constraint-aware filtering.
    # Human labels need both plausible and misleading same-operation pairs;
    # otherwise the new filter silently removes the very false matches we want
    # the evaluation set to measure. "unspecified" preserves structural gates.
    structural_criteria = [
        criterion.model_copy(update={"constraint": "unspecified"})
        for criterion in criteria
    ]
    structural_result = map_requirements(
        structural_criteria, tests, threshold=0.0
    )
    candidates = {
        (row.requirement_id, match.case_id)
        for row in structural_result.rows
        for match in row.candidates
    }
    strata: dict[tuple[str, str], str] = {}
    for criterion in criteria:
        for test in tests:
            pair = (criterion.id, test.case_id)
            if pair in candidates:
                strata[pair] = "eligible"
            elif (test.method.upper(), test.path) == (
                criterion.method.upper(),
                criterion.path,
            ):
                strata[pair] = "same_operation"
            else:
                strata[pair] = "other_operation"
    return strata


def sample_pairs(
    criteria: Sequence[Criterion],
    tests: Sequence[MappableTest],
    *,
    seed: int = DEFAULT_SEED,
    counts: dict[str, int] | None = None,
) -> list[dict[str, str]]:
    """Draw a stratified, score-blind sample of pairs and assign dev/test splits.

    Within a stratum, pairs are ordered by a seeded hash and then taken round-robin
    across criteria, so no single criterion dominates. Dev and test alternate down
    each stratum's list so both halves see every stratum.
    """
    counts = dict(DEFAULT_COUNTS if counts is None else counts)
    unknown = set(counts) - set(DEFAULT_COUNTS)
    if unknown:
        raise EvaluationError(f"unknown strata: {sorted(unknown)}")
    result = map_requirements(criteria, tests)
    strata = classify_pairs(criteria, tests, result)
    criterion_by_id = {item.id: item for item in criteria}
    test_by_id = {item.case_id: item for item in tests}

    chosen: list[tuple[str, tuple[str, str], str]] = []
    for stratum in DEFAULT_COUNTS:
        want = counts.get(stratum, 0)
        pool = sorted(
            (pair for pair, name in strata.items() if name == stratum),
            key=lambda pair: _stable_key(seed, _pair_id(*pair)),
        )
        by_requirement: dict[str, list[tuple[str, str]]] = {}
        for pair in pool:
            by_requirement.setdefault(pair[0], []).append(pair)
        order = sorted(by_requirement)
        taken: list[tuple[str, str]] = []
        depth = 0
        while len(taken) < want and any(
            depth < len(by_requirement[name]) for name in order
        ):
            for name in order:
                if depth < len(by_requirement[name]) and len(taken) < want:
                    taken.append(by_requirement[name][depth])
            depth += 1
        for index, pair in enumerate(taken):
            chosen.append((SPLITS[index % 2], pair, stratum))

    rows: list[dict[str, str]] = []
    for split, (requirement_id, case_id), _stratum in chosen:
        criterion = criterion_by_id[requirement_id]
        test = test_by_id[case_id]
        rows.append(
            {
                "pair_id": _pair_id(requirement_id, case_id),
                "split": split,
                "requirement_id": requirement_id,
                "criterion_description": criterion.description,
                "criterion_field": criterion.field,
                "criterion_constraint": criterion.constraint,
                "criterion_value": json.dumps(criterion.value, sort_keys=True),
                "criterion_expected_status": str(criterion.expected_status),
                "case_id": case_id,
                "test_name": test.name,
                "test_kind": test.kind,
                "test_field": test.field,
                "test_expected_status": str(test.expected_status),
                "test_checks": "+".join(test.checks),
                "label": "",
                "notes": "",
            }
        )
    # Shuffle so neighbouring rows do not reveal the stratum or the criterion.
    rows.sort(key=lambda row: _stable_key(seed, "order:" + row["pair_id"]))
    return rows


def write_label_sheet(
    rows: Sequence[dict[str, str]],
    path: Path,
    meta_path: Path,
    *,
    tests: Sequence[MappableTest],
    seed: int,
) -> None:
    """Write the blank sheet and the metadata that ties labels to these tests."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=LABEL_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    meta = {
        "case_id_fingerprint": case_id_fingerprint(tests),
        "pair_fingerprints": pair_fingerprints(rows),
        "pairs": len(rows),
        "seed": seed,
        "splits": {
            split: sum(1 for row in rows if row["split"] == split) for split in SPLITS
        },
        "tests": len(tests),
    }
    Path(meta_path).write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8",
            newline="\n")


# --- reading labels -----------------------------------------------------------


def _read_sheet(path: Path) -> list[dict[str, str]]:
    """Read a label sheet's rows.

    A UTF-8 byte-order mark is accepted (spreadsheet programs add one when saving
    as "CSV UTF-8"). Any other encoding is rejected with a message that says what
    to do, instead of a raw decoding error.
    """
    try:
        with Path(path).open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = [
                name for name in LABEL_COLUMNS if name not in (reader.fieldnames or [])
            ]
            if missing:
                raise EvaluationError(f"{path} is missing columns: {missing}")
            return list(reader)
    except UnicodeDecodeError as error:
        raise EvaluationError(
            f"{path} is not valid UTF-8 ({error.reason} at byte {error.start}); "
            "save it as 'CSV UTF-8' so the dashes in test names survive"
        ) from error
    except OSError as error:
        raise EvaluationError(f"cannot read {path}: {error}") from error


def _read_meta(meta_path: Path, tests: Sequence[MappableTest]) -> dict[str, Any]:
    """Load the sheet metadata (it must exist) and check the test-ID fingerprint."""
    meta_path = Path(meta_path)
    if not meta_path.exists():
        raise EvaluationError(
            f"{meta_path} is missing; it records which pairs and splits are frozen, "
            "so the sheet cannot be checked without it"
        )
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if meta.get("case_id_fingerprint") != case_id_fingerprint(tests):
        raise EvaluationError(
            "the generated test IDs changed after these labels were made; the "
            "labels no longer describe the current tests. Version the change "
            "and relabel before evaluating."
        )
    return meta  # type: ignore[no-any-return]


def _check_frozen_pairs(
    rows: Sequence[dict[str, str]],
    meta: dict[str, Any],
    meta_path: Path,
    *,
    subset: bool,
) -> None:
    """The sheet must be the frozen one: same pairs, same splits, same columns."""
    frozen = meta.get("pair_fingerprints")
    if not isinstance(frozen, dict) or not frozen:
        raise EvaluationError(
            f"{meta_path} has no pair_fingerprints, so the dev/test split cannot be "
            "verified"
        )
    current = pair_fingerprints(rows)
    if not subset and set(current) != set(frozen):
        raise EvaluationError(
            "the sheet's pairs differ from the frozen set: "
            f"{len(set(frozen) - set(current))} missing, "
            f"{len(set(current) - set(frozen))} extra"
        )
    for pair_id, digest in current.items():
        if pair_id not in frozen:
            raise EvaluationError(f"pair {pair_id} is not in the frozen set")
        if digest != frozen[pair_id]:
            raise EvaluationError(
                f"pair {pair_id}: a frozen column (including its dev/test split) "
                "differs from the sheet that was drawn; only label and notes may "
                "change"
            )
    if not subset:
        if meta.get("pairs") != len(rows):
            raise EvaluationError("the sheet's pair count differs from the metadata")
        counts = {name: sum(1 for r in rows if r["split"] == name) for name in SPLITS}
        if meta.get("splits") != counts:
            raise EvaluationError("the sheet's split counts differ from the metadata")


def load_labels(
    path: Path,
    criteria: Sequence[Criterion],
    tests: Sequence[MappableTest],
    meta_path: Path | None = None,
    *,
    subset: bool = False,
) -> list[dict[str, str]]:
    """Read and validate a filled-in label sheet. Every problem is an error.

    With ``meta_path`` the sheet is also checked against the frozen metadata, which
    must exist. ``subset=True`` is for a re-labelling sheet that holds only some of
    the pairs; each of its pairs must still match the frozen original.
    """
    rows = _read_sheet(path)
    if not rows:
        raise EvaluationError(f"{path} contains no labelled pairs")
    meta = _read_meta(meta_path, tests) if meta_path is not None else None
    criteria_ids = {item.id for item in criteria}
    case_ids = {item.case_id for item in tests}
    seen: set[str] = set()
    for number, row in enumerate(rows, start=2):
        where = f"{path} line {number}"
        if row["label"] not in ("0", "1"):
            raise EvaluationError(
                f"{where}: label must be 1 or 0, got {row['label']!r}"
            )
        if row["split"] not in SPLITS:
            raise EvaluationError(f"{where}: split must be dev or test")
        if row["requirement_id"] not in criteria_ids:
            raise EvaluationError(
                f"{where}: unknown requirement {row['requirement_id']}"
            )
        if row["case_id"] not in case_ids:
            raise EvaluationError(f"{where}: unknown case {row['case_id']}")
        expected = _pair_id(row["requirement_id"], row["case_id"])
        if row["pair_id"] != expected:
            raise EvaluationError(f"{where}: pair_id does not match its columns")
        if expected in seen:
            raise EvaluationError(f"{where}: duplicate pair {expected}")
        seen.add(expected)
    if meta is not None and meta_path is not None:
        _check_frozen_pairs(rows, meta, meta_path, subset=subset)
    return rows


# --- mapping evaluation -------------------------------------------------------


def _predicted_pairs(result: MappingResult) -> set[metrics.Pair]:
    return {
        (row.requirement_id, match.case_id)
        for row in result.rows
        for match in row.matches
    }


def _quality_dict(quality: metrics.MappingQuality) -> dict[str, Any]:
    return {
        "true_positives": quality.true_positives,
        "false_positives": quality.false_positives,
        "false_negatives": quality.false_negatives,
        "true_negatives": quality.true_negatives,
        "unlabelled_predictions": quality.unlabelled_predictions,
        "precision": quality.precision,
        "recall": quality.recall,
    }


def f1(precision: float | None, recall: float | None) -> float | None:
    """Harmonic mean of precision and recall; undefined if either is undefined."""
    if precision is None or recall is None:
        return None
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


# A dev half with fewer positives than this cannot rank thresholds: with one
# positive, every threshold that finds it ties at F1 1.0 and the "higher threshold
# wins ties" rule then picks the top of the range, which fits nothing. Five is a
# floor below which the sweep is declared uninformative, not a sign that five is
# enough to trust it.
MIN_DEV_POSITIVES = 5


def choose_threshold(sweep: Sequence[dict[str, Any]]) -> float | None:
    """The rule fixed in advance: highest dev F1; on a tie, the higher threshold.

    A higher threshold is the more conservative choice (fewer false matches), so it
    wins ties. Thresholds whose F1 is undefined are skipped. None if none qualify.
    """
    best: dict[str, Any] | None = None
    for entry in sweep:
        if entry["f1"] is None:
            continue
        if (
            best is None
            or entry["f1"] > best["f1"]
            or (entry["f1"] == best["f1"] and entry["threshold"] > best["threshold"])
        ):
            best = entry
    return None if best is None else float(best["threshold"])


def evaluate_mapping(
    label_rows: Sequence[dict[str, str]],
    criteria: Sequence[Criterion],
    tests: Sequence[MappableTest],
    *,
    threshold: float,
    top_k: int,
) -> dict[str, Any]:
    """Score the mapper on labelled pairs, with a dev/test split.

    The threshold sweep uses dev pairs only. The configured threshold is reported
    on both halves; ``dev_chosen_threshold`` is the sweep's choice, also scored on
    the test half it never saw.
    """
    labels_by_split: dict[str, dict[metrics.Pair, bool]] = {name: {} for name in SPLITS}
    for row in label_rows:
        pair = (row["requirement_id"], row["case_id"])
        labels_by_split[row["split"]][pair] = row["label"] == "1"
    all_labels = {**labels_by_split["dev"], **labels_by_split["test"]}

    def score(split_labels: dict[metrics.Pair, bool], level: float) -> dict[str, Any]:
        result = map_requirements(criteria, tests, threshold=level, top_k=top_k)
        return _quality_dict(
            metrics.mapping_quality(_predicted_pairs(result), split_labels)
        )

    sweep = []
    for level in SWEEP_THRESHOLDS:
        quality = score(labels_by_split["dev"], level)
        sweep.append(
            {
                "threshold": level,
                "precision": quality["precision"],
                "recall": quality["recall"],
                "f1": f1(quality["precision"], quality["recall"]),
            }
        )
    dev_positives = sum(labels_by_split["dev"].values())
    attempted = dev_positives >= MIN_DEV_POSITIVES
    chosen = choose_threshold(sweep) if attempted else None
    if not attempted:
        reason: str | None = "too_few_dev_positives"
    elif chosen is None:
        reason = "no_defined_f1"
    else:
        reason = None

    reference = map_requirements(criteria, tests, threshold=threshold, top_k=top_k)
    strata = classify_pairs(criteria, tests, reference)
    by_stratum: dict[str, dict[str, int]] = {}
    for pair, positive in all_labels.items():
        bucket = by_stratum.setdefault(strata[pair], {"pairs": 0, "positives": 0})
        bucket["pairs"] += 1
        bucket["positives"] += int(positive)

    report: dict[str, Any] = {
        "configured": {"threshold": threshold, "top_k": top_k},
        "labelled_pairs": len(all_labels),
        "positives": sum(all_labels.values()),
        "pairs_by_split": {name: len(labels_by_split[name]) for name in SPLITS},
        "positives_by_split": {
            name: sum(labels_by_split[name].values()) for name in SPLITS
        },
        "by_stratum": {name: by_stratum[name] for name in sorted(by_stratum)},
        "configured_threshold": {
            name: score(labels_by_split[name], threshold) for name in SPLITS
        },
        "dev_sweep": sweep,
        "threshold_tuning": {
            "attempted": attempted,
            "tuned": chosen is not None,
            "reason": reason,
            "dev_positives": dev_positives,
            "minimum_dev_positives": MIN_DEV_POSITIVES,
            "kept_threshold": threshold if chosen is None else chosen,
        },
        "dev_chosen_threshold": chosen,
    }
    if chosen is not None:
        report["dev_chosen_threshold_scores"] = {
            name: score(labels_by_split[name], chosen) for name in SPLITS
        }
    return report


# --- self-agreement -----------------------------------------------------------


def _write_rows(rows: Sequence[dict[str, str]], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=LABEL_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def make_relabel_rows(
    first_rows: Sequence[dict[str, str]],
    *,
    size: int = DEFAULT_RELABEL_SIZE,
    seed: int = DEFAULT_SEED,
) -> list[dict[str, str]]:
    """A blank copy of a seeded subset of the first sheet, in a different order.

    The subset is chosen by a seeded hash of the pair ID alone, so it cannot depend
    on the first labels, and the labels and notes are cleared so the second pass is
    made without consulting the first.
    """
    ids = [row["pair_id"] for row in first_rows]
    if len(set(ids)) != len(ids):
        raise EvaluationError("the first sheet contains duplicate pairs")
    if not 1 <= size <= len(ids):
        raise EvaluationError(f"size must be between 1 and {len(ids)}, got {size}")
    by_id = {row["pair_id"]: row for row in first_rows}
    chosen = sorted(ids, key=lambda pair: _stable_key(seed, "relabel:" + pair))[:size]
    chosen.sort(key=lambda pair: _stable_key(seed, "relabel-order:" + pair))
    rows = []
    for pair_id in chosen:
        row = {name: by_id[pair_id][name] for name in LABEL_COLUMNS}
        row["label"] = ""
        row["notes"] = ""
        rows.append(row)
    return rows


def _is_unsure(row: dict[str, str]) -> bool:
    return "unsure" in row.get("notes", "").lower()


def evaluate_agreement(
    first_rows: Sequence[dict[str, str]],
    second_rows: Sequence[dict[str, str]],
    *,
    second_rater: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Agreement between two labellings of the same pairs.

    Every pair in the second sheet must be in the first. Only those pairs are
    compared. By default the second pass is the same person, so this measures
    self-consistency. If ``second_rater`` names a different rater, the report says
    so and is not self-agreement; ``note`` is carried into the report as stated.
    """
    first = {(r["requirement_id"], r["case_id"]): r for r in first_rows}
    second = {(r["requirement_id"], r["case_id"]): r for r in second_rows}
    unknown = sorted(_pair_id(*pair) for pair in second if pair not in first)
    if unknown:
        raise EvaluationError(
            f"pairs in the relabel sheet are not in the first sheet: {unknown}"
        )
    pairs = sorted(second)
    if not pairs:
        raise EvaluationError("the relabel sheet has no pairs to compare")
    first_labels = {pair: first[pair]["label"] == "1" for pair in pairs}
    second_labels = {pair: second[pair]["label"] == "1" for pair in pairs}
    disagreements = [
        {
            "pair_id": _pair_id(*pair),
            "split": first[pair]["split"],
            "first_label": int(first_labels[pair]),
            "second_label": int(second_labels[pair]),
        }
        for pair in pairs
        if first_labels[pair] != second_labels[pair]
    ]
    agreed = len(pairs) - len(disagreements)
    return {
        "first_sheet_pairs": len(first),
        "pairs_compared": len(pairs),
        "agreements": agreed,
        "disagreements": disagreements,
        "observed_agreement": metrics.ratio(agreed, len(pairs)),
        "cohens_kappa": metrics.cohens_kappa(first_labels, second_labels),
        "positives_first": sum(first_labels.values()),
        "positives_second": sum(second_labels.values()),
        "unsure_first": sum(_is_unsure(first[pair]) for pair in pairs),
        "unsure_second": sum(_is_unsure(second[pair]) for pair in pairs),
        "second_rater": second_rater,
        "note": note,
    }


def agreement_markdown(report: dict[str, Any]) -> str:
    """A short human-readable summary of ``evaluate_agreement`` output."""
    n = report["pairs_compared"]
    kappa = report["cohens_kappa"]
    other = report.get("second_rater")
    lines = [
        "# Self-agreement" if other is None else "# Agreement with a second rater",
        "",
        f"Pairs compared: {n} of {report['first_sheet_pairs']} in the first sheet.",
        f"Agreed on {report['agreements']} of {n} "
        f"(observed agreement {_fmt(report['observed_agreement'])}).",
        f"Cohen's kappa: {_fmt(kappa)}"
        + (
            " (undefined: chance agreement is already 1, or there is nothing to "
            "compare)"
            if kappa is None
            else ""
        ),
        f"Positive labels: {report['positives_first']} first pass, "
        f"{report['positives_second']} second pass. Rows marked unsure: "
        f"{report['unsure_first']} first pass, {report['unsure_second']} second pass.",
        "",
        (
            "This is one person's consistency with themselves, not agreement "
            "between people."
            if other is None
            else f"The second rater is {other}, a different rater from the first "
            "labeller, so this is not self-consistency and does not replace it."
        )
        + f" With {n} pairs, one changed label moves observed agreement by "
        f"{100 / n:.1f} points, and kappa is unstable when most labels are the same.",
        *([f"Note: {report['note']}", ""] if report.get("note") else [""]),
        "## Disagreements",
        "",
    ]
    if not report["disagreements"]:
        lines.append("None.")
    else:
        lines += ["| pair | split | first | second |", "|---|---|---|---|"]
        for item in report["disagreements"]:
            lines.append(
                f"| {item['pair_id']} | {item['split']} | {item['first_label']} | "
                f"{item['second_label']} |"
            )
    return "\n".join(lines) + "\n"


# --- mutants ------------------------------------------------------------------

# Only a test that ran and failed counts as a kill. A setup error (a broken fixture)
# is not evidence that a test noticed the behaviour change, so it never kills.
_DETECTING_OUTCOMES = ("failed",)


def detecting_cases(results: Sequence[dict[str, Any]]) -> list[str]:
    """Sorted IDs of the cases that detected a mutant (outcome ``failed``)."""
    return sorted(
        row["case_id"] for row in results if row["outcome"] in _DETECTING_OUTCOMES
    )


def evaluate_mutants(
    spec_path: Path,
    work_dir: Path,
    output_dir: Path,
    *,
    selected: Sequence[str] | None = None,
    mapping_path: Path | None = None,
) -> dict[str, Any]:
    """Run the generated suite on the real API, then on each mutant.

    A mutant is killed when at least one test fails on it; setup errors do not
    count. The control run on the unmutated API must pass completely; otherwise a
    "kill" could just be a broken test, and the whole evaluation is rejected.
    """
    names = sorted(MUTANTS) if selected is None else list(selected)
    unknown = sorted(set(names) - set(MUTANTS))
    if unknown:
        raise EvaluationError(f"unknown mutants: {unknown}; known: {sorted(MUTANTS)}")
    if len(set(names)) != len(names):
        raise EvaluationError("mutant names must be unique")
    names = sorted(names)

    work_dir = Path(work_dir)
    output_dir = Path(output_dir)
    entries = export_tests(spec_path, work_dir, mapping_path)
    control = run_tests(work_dir, entries, output_dir / "control.json")
    if control["summary"].get("passed", 0) != control["summary"]["total"]:
        raise EvaluationError(
            "the control run on the unmutated API did not pass completely "
            f"({control['summary']}); mutation results would be meaningless"
        )

    records = []
    killed_by_mutant: dict[str, bool] = {}
    for name in names:
        report = run_tests(
            work_dir, entries, output_dir / f"mutant-{name}.json", mutant=name
        )
        detecting = detecting_cases(report["results"])
        killed_by_mutant[name] = bool(detecting)
        records.append(
            {
                "mutant": name,
                "description": MUTANTS[name],
                "killed": bool(detecting),
                "detecting_cases": detecting,
                "outcomes": {
                    key: value
                    for key, value in report["summary"].items()
                    if key != "total"
                },
            }
        )
    score = metrics.mutation_score(killed_by_mutant)
    result = {
        "control": control["summary"],
        "mutants": records,
        "score": {
            "executed": score.executed,
            "killed": score.killed,
            "survived": list(score.survived),
            "mutation_score": score.score,
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "mutation.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return result


# --- reports ------------------------------------------------------------------


def _fmt(value: float | None) -> str:
    return "undefined" if value is None else f"{value:.3f}"


def mapping_markdown(report: dict[str, Any]) -> str:
    """A short human-readable summary of ``evaluate_mapping`` output."""
    lines = [
        "# Mapping evaluation",
        "",
        f"Labelled pairs: {report['labelled_pairs']} "
        f"({report['positives']} positive). "
        f"Dev {report['pairs_by_split']['dev']} / "
        f"test {report['pairs_by_split']['test']}.",
        "",
        "Precision and recall describe this stratified sample, not every possible "
        "criterion/test pair. Small counts mean wide uncertainty.",
        "",
        f"## Configured threshold {report['configured']['threshold']}",
        "",
        "| split | TP | FP | FN | TN | precision | recall |",
        "|---|---|---|---|---|---|---|",
    ]
    for name in SPLITS:
        row = report["configured_threshold"][name]
        lines.append(
            f"| {name} | {row['true_positives']} | {row['false_positives']} | "
            f"{row['false_negatives']} | {row['true_negatives']} | "
            f"{_fmt(row['precision'])} | {_fmt(row['recall'])} |"
        )
    lines += ["", "## Dev sweep (threshold chosen on dev only)", ""]
    lines += ["| threshold | precision | recall | F1 |", "|---|---|---|---|"]
    for entry in report["dev_sweep"]:
        lines.append(
            f"| {entry['threshold']:.2f} | {_fmt(entry['precision'])} | "
            f"{_fmt(entry['recall'])} | {_fmt(entry['f1'])} |"
        )
    chosen = report["dev_chosen_threshold"]
    tuning = report["threshold_tuning"]
    if tuning["tuned"]:
        lines += ["", f"Dev-chosen threshold: {chosen}"]
    elif tuning["reason"] == "no_defined_f1":
        lines += [
            "",
            "Threshold not tuned: the dev half has enough positives "
            f"({tuning['dev_positives']}), but the mapper predicted no labelled dev "
            "pair at any threshold, so F1 is undefined everywhere. The configured "
            f"threshold {tuning['kept_threshold']} is kept. It is a default, not a "
            "measured optimum.",
        ]
    else:
        lines += [
            "",
            f"Threshold not tuned: the dev half has {tuning['dev_positives']} "
            f"positive(s), fewer than the {tuning['minimum_dev_positives']} needed "
            "for the sweep to rank thresholds. The configured threshold "
            f"{tuning['kept_threshold']} is kept. It is a default, not a measured "
            "optimum.",
        ]
    if chosen is not None:
        lines += ["", "| split | precision | recall |", "|---|---|---|"]
        for name in SPLITS:
            row = report["dev_chosen_threshold_scores"][name]
            lines.append(
                f"| {name} | {_fmt(row['precision'])} | {_fmt(row['recall'])} |"
            )
    return "\n".join(lines) + "\n"


def mutation_markdown(result: dict[str, Any]) -> str:
    """A short human-readable summary of ``evaluate_mutants`` output."""
    score = result["score"]
    lines = [
        "# Mutation evaluation",
        "",
        f"Control run (real API): {result['control']}",
        f"Mutants killed: {score['killed']} of {score['executed']} "
        f"(mutation score {_fmt(score['mutation_score'])}).",
        "",
        "Mutants are hand-written and not exhaustive.",
        "",
        "| mutant | killed | detecting tests | change |",
        "|---|---|---|---|",
    ]
    for record in result["mutants"]:
        lines.append(
            f"| {record['mutant']} | {'yes' if record['killed'] else 'NO'} | "
            f"{len(record['detecting_cases'])} | {record['description']} |"
        )
    return "\n".join(lines) + "\n"


# --- CLI ----------------------------------------------------------------------


def register_commands(commands: Any) -> None:
    """Add `assay evaluate sample|mapping|relabel|agreement|mutants`."""
    evaluate = commands.add_parser(
        "evaluate", help="measure mapping quality and mutants"
    )
    parts = evaluate.add_subparsers(dest="evaluation", required=True)
    for name, help_text in (
        ("sample", "write a blank, score-blind sheet of pairs to label"),
        ("mapping", "score the mapper against a filled-in label sheet"),
        ("ablation", "compare rules only, rules + TF-IDF, and TF-IDF only"),
        ("mutants", "run the generated suite against the demo API mutants"),
    ):
        part = parts.add_parser(name, help=help_text)
        part.add_argument("--spec", type=Path, default=Path("specs/openapi.json"))
        if name != "mutants":
            part.add_argument("--criteria", type=Path, default=Path("criteria.yaml"))
            part.add_argument(
                "--labels", type=Path, default=Path("data/labelled_pairs.csv")
            )
            part.add_argument(
                "--meta", type=Path, default=Path("data/labelled_pairs.meta.json")
            )
        if name == "sample":
            part.add_argument("--seed", type=int, default=DEFAULT_SEED)
            part.add_argument("--force", action="store_true", help="overwrite labels")
        if name in ("mapping", "ablation"):
            part.add_argument("--threshold", type=float, default=0.4)
            part.add_argument("--top-k", type=int, default=3)
            part.add_argument("--output", type=Path, default=Path("reports/evaluation"))
        if name == "mutants":
            part.add_argument("--work-dir", type=Path, default=Path("generated_tests"))
            part.add_argument("--output", type=Path, default=Path("reports/mutation"))
            part.add_argument("--mutant", action="append", dest="mutants", default=None)
            part.add_argument("--mapping", type=Path, default=None)

    relabel = parts.add_parser(
        "relabel", help="write a blank sheet of a subset of the pairs to label again"
    )
    relabel.add_argument(
        "--labels",
        type=Path,
        default=Path("data/labelled_pairs.csv"),
        help="first sheet",
    )
    relabel.add_argument(
        "--output", type=Path, default=Path("data/relabelled_pairs.csv")
    )
    relabel.add_argument("--size", type=int, default=DEFAULT_RELABEL_SIZE)
    relabel.add_argument("--seed", type=int, default=DEFAULT_SEED)
    relabel.add_argument("--force", action="store_true", help="overwrite the output")

    agreement = parts.add_parser(
        "agreement", help="compare the first and second labelling of the same pairs"
    )
    agreement.add_argument("--spec", type=Path, default=Path("specs/openapi.json"))
    agreement.add_argument("--criteria", type=Path, default=Path("criteria.yaml"))
    agreement.add_argument(
        "--labels",
        type=Path,
        default=Path("data/labelled_pairs.csv"),
        help="first sheet",
    )
    agreement.add_argument(
        "--relabels", type=Path, default=Path("data/relabelled_pairs.csv")
    )
    agreement.add_argument(
        "--meta", type=Path, default=Path("data/labelled_pairs.meta.json")
    )
    agreement.add_argument("--output", type=Path, default=Path("reports/evaluation"))
    agreement.add_argument(
        "--second-rater",
        default=None,
        help="name of a different second rater; omit for a self-agreement pass",
    )
    agreement.add_argument(
        "--note", default=None, help="free text carried into the report as stated"
    )


def handle(arguments: argparse.Namespace) -> int:
    """Run an `assay evaluate` subcommand: 0 success, 2 error."""
    try:
        if arguments.evaluation == "mutants":
            result = evaluate_mutants(
                arguments.spec,
                arguments.work_dir,
                arguments.output,
                selected=arguments.mutants,
                mapping_path=arguments.mapping,
            )
            (arguments.output / "mutation.md").write_text(
                mutation_markdown(result), encoding="utf-8", newline="\n")
            score = result["score"]
            print(f"Killed {score['killed']} of {score['executed']} mutants")
            print(f"Survived: {', '.join(score['survived']) or 'none'}")
            print(f"Reports written to {arguments.output}")
            return 0
        if arguments.evaluation == "relabel":
            if Path(arguments.output).resolve() == Path(arguments.labels).resolve():
                raise EvaluationError("--output must not be the first sheet")
            if arguments.output.exists() and not arguments.force:
                raise EvaluationError(
                    f"{arguments.output} already exists; refusing to overwrite it "
                    "(use --force to replace it)"
                )
            rows = make_relabel_rows(
                _read_sheet(arguments.labels), size=arguments.size, seed=arguments.seed
            )
            _write_rows(rows, arguments.output)
            print(f"Wrote {len(rows)} blank pairs to label again in {arguments.output}")
            print("Label them without looking at your first labels.")
            return 0
        criteria = load_criteria(arguments.criteria)
        tests = baseline_tests_from_spec(load_spec(arguments.spec))
        if arguments.evaluation == "agreement":
            first_rows = load_labels(arguments.labels, criteria, tests, arguments.meta)
            second_rows = load_labels(
                arguments.relabels, criteria, tests, arguments.meta, subset=True
            )
            report = evaluate_agreement(
                first_rows,
                second_rows,
                second_rater=arguments.second_rater,
                note=arguments.note,
            )
            arguments.output.mkdir(parents=True, exist_ok=True)
            (arguments.output / "agreement.json").write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
            (arguments.output / "agreement.md").write_text(
                agreement_markdown(report), encoding="utf-8", newline="\n")
            print(
                f"Agreed on {report['agreements']} of {report['pairs_compared']} "
                f"pairs; kappa {_fmt(report['cohens_kappa'])}"
            )
            print(f"Reports written to {arguments.output}")
            return 0
        if arguments.evaluation == "sample":
            if arguments.labels.exists() and not arguments.force:
                raise EvaluationError(
                    f"{arguments.labels} already exists; refusing to overwrite "
                    "labels (use --force to replace it)"
                )
            rows = sample_pairs(criteria, tests, seed=arguments.seed)
            write_label_sheet(
                rows, arguments.labels, arguments.meta, tests=tests, seed=arguments.seed
            )
            print(f"Wrote {len(rows)} pairs to label in {arguments.labels}")
            return 0
        label_rows = load_labels(arguments.labels, criteria, tests, arguments.meta)
        if arguments.evaluation == "ablation":
            ablation_report = evaluate_ablation(
                label_rows,
                criteria,
                tests,
                threshold=arguments.threshold,
                top_k=arguments.top_k,
            )
            arguments.output.mkdir(parents=True, exist_ok=True)
            (arguments.output / "ablation.json").write_text(
                json.dumps(ablation_report, indent=2, sort_keys=True) + "\n",
                encoding="utf-8", newline="\n")
            (arguments.output / "ablation.md").write_text(
                ablation_markdown(ablation_report), encoding="utf-8", newline="\n")
            for variant in VARIANTS:
                scored = ablation_report["variants"][variant]["all"]
                print(
                    f"{ABLATION_LABELS[variant]}: precision "
                    f"{_fmt(scored['precision'])}, recall {_fmt(scored['recall'])}"
                )
            print(f"Reports written to {arguments.output}")
            return 0
        report = evaluate_mapping(
            label_rows,
            criteria,
            tests,
            threshold=arguments.threshold,
            top_k=arguments.top_k,
        )
        arguments.output.mkdir(parents=True, exist_ok=True)
        (arguments.output / "mapping.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
        (arguments.output / "mapping.md").write_text(
            mapping_markdown(report), encoding="utf-8", newline="\n")
        print(f"Evaluated {report['labelled_pairs']} labelled pairs")
        print(f"Reports written to {arguments.output}")
        return 0
    except (OSError, ValueError, RuntimeError, MappingError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
