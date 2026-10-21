"""Evaluation harness: labelled-pair sampling, mapping evaluation, mutant runs.

Three jobs, kept separate because they answer different questions:

* ``sample``  - draw pairs for a human to label, blind to the mapper's scores.
* ``mapping`` - score the mapper against the human labels (precision / recall).
* ``mutants`` - run the generated suite against switchable demo API mutants.

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
        "pairs": len(rows),
        "seed": seed,
        "splits": {
            split: sum(1 for row in rows if row["split"] == split) for split in SPLITS
        },
        "tests": len(tests),
    }
    Path(meta_path).write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


# --- reading labels -----------------------------------------------------------


def load_labels(
    path: Path,
    criteria: Sequence[Criterion],
    tests: Sequence[MappableTest],
    meta_path: Path | None = None,
) -> list[dict[str, str]]:
    """Read and validate a filled-in label sheet. Every problem is an error."""
    try:
        with Path(path).open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = [
                name for name in LABEL_COLUMNS if name not in (reader.fieldnames or [])
            ]
            if missing:
                raise EvaluationError(f"{path} is missing columns: {missing}")
            rows = list(reader)
    except OSError as error:
        raise EvaluationError(f"cannot read {path}: {error}") from error
    if not rows:
        raise EvaluationError(f"{path} contains no labelled pairs")
    if meta_path is not None and Path(meta_path).exists():
        meta = json.loads(Path(meta_path).read_text(encoding="utf-8"))
        if meta.get("case_id_fingerprint") != case_id_fingerprint(tests):
            raise EvaluationError(
                "the generated test IDs changed after these labels were made; the "
                "labels no longer describe the current tests. Version the change "
                "and relabel before evaluating."
            )
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
    chosen = choose_threshold(sweep)

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
        "dev_chosen_threshold": chosen,
    }
    if chosen is not None:
        report["dev_chosen_threshold_scores"] = {
            name: score(labels_by_split[name], chosen) for name in SPLITS
        }
    return report


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
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
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
    lines += ["", f"Dev-chosen threshold: {chosen if chosen is not None else 'none'}"]
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
    """Add `assay evaluate sample|mapping|mutants`."""
    evaluate = commands.add_parser(
        "evaluate", help="measure mapping quality and mutants"
    )
    parts = evaluate.add_subparsers(dest="evaluation", required=True)
    for name, help_text in (
        ("sample", "write a blank, score-blind sheet of pairs to label"),
        ("mapping", "score the mapper against a filled-in label sheet"),
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
        if name == "mapping":
            part.add_argument("--threshold", type=float, default=0.4)
            part.add_argument("--top-k", type=int, default=3)
            part.add_argument("--output", type=Path, default=Path("reports/evaluation"))
        if name == "mutants":
            part.add_argument("--work-dir", type=Path, default=Path("generated_tests"))
            part.add_argument("--output", type=Path, default=Path("reports/mutation"))
            part.add_argument("--mutant", action="append", dest="mutants", default=None)
            part.add_argument("--mapping", type=Path, default=None)


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
                mutation_markdown(result), encoding="utf-8"
            )
            score = result["score"]
            print(f"Killed {score['killed']} of {score['executed']} mutants")
            print(f"Survived: {', '.join(score['survived']) or 'none'}")
            print(f"Reports written to {arguments.output}")
            return 0
        criteria = load_criteria(arguments.criteria)
        tests = baseline_tests_from_spec(load_spec(arguments.spec))
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
        report = evaluate_mapping(
            label_rows,
            criteria,
            tests,
            threshold=arguments.threshold,
            top_k=arguments.top_k,
        )
        arguments.output.mkdir(parents=True, exist_ok=True)
        (arguments.output / "mapping.json").write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (arguments.output / "mapping.md").write_text(
            mapping_markdown(report), encoding="utf-8"
        )
        print(f"Evaluated {report['labelled_pairs']} labelled pairs")
        print(f"Reports written to {arguments.output}")
        return 0
    except (OSError, ValueError, RuntimeError, MappingError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
