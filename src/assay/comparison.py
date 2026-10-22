"""M6: compare the three suites on requirement coverage and mutation score.

A. baseline  - tests derived from the OpenAPI document only (never reads criteria),
               tagged with requirement IDs by the offline mapper.
B. criteria  - one explicit test per structured criterion (`assay acceptance`),
               tagged by construction because it was generated from the criteria.
C. combined  - A and B together. Every test builds its own fresh app, so tests do
               not influence each other, which makes running A and B separately
               and taking the union identical to one combined run. That identity
               was checked once by running both directories in one pytest session;
               it is not re-run here.

Coverage and mutation score answer different questions and are never merged into
one number. Coverage of A depends on the mapper's choices; coverage of B and C is
partly circular because B is generated from the criteria being counted.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from assay import metrics
from assay.acceptance import export_acceptance_tests, run_exported_acceptance
from assay.demo_api import MUTANTS
from assay.evaluation import EvaluationError, evaluate_mutants
from assay.mapping import (
    DEFAULT_THRESHOLD,
    DEFAULT_TOP_K,
    baseline_tests_from_spec,
    get_mapper,
    load_criteria,
    write_reports,
)
from assay.runner import load_spec

SUITES = ("baseline", "criteria", "combined")


def _coverage_dict(
    criteria_ids: Sequence[str], rows: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    coverage = metrics.requirement_coverage(criteria_ids, rows)
    return {
        "criteria": coverage.criteria,
        "covered": list(coverage.covered),
        "mapped_not_passing": list(coverage.mapped_not_passing),
        "not_mapped": list(coverage.not_mapped),
        "coverage": coverage.coverage,
    }


def _mutation_dict(killed_by_mutant: dict[str, bool]) -> dict[str, Any]:
    score = metrics.mutation_score(killed_by_mutant)
    return {
        "executed": score.executed,
        "killed": score.killed,
        "survived": list(score.survived),
        "mutation_score": score.score,
    }


def build_comparison(
    criteria_ids: Sequence[str],
    *,
    baseline_rows: Sequence[dict[str, Any]],
    criteria_rows: Sequence[dict[str, Any]],
    baseline_killed: dict[str, bool],
    criteria_killed: dict[str, bool],
    tests: dict[str, int],
    descriptions: dict[str, str],
) -> dict[str, Any]:
    """Pure assembly of the comparison from already measured results.

    ``*_rows`` are result rows with ``outcome`` and ``requirements`` (control runs
    on the real API). ``*_killed`` map every executed mutant to whether the suite
    detected it. The combined suite is the union of both.
    """
    if set(baseline_killed) != set(criteria_killed):
        raise EvaluationError("both suites must be run against the same mutants")
    combined_killed = {
        name: baseline_killed[name] or criteria_killed[name]
        for name in baseline_killed
    }
    killed = {
        "baseline": baseline_killed,
        "criteria": criteria_killed,
        "combined": combined_killed,
    }
    rows = {
        "baseline": list(baseline_rows),
        "criteria": list(criteria_rows),
        "combined": [*baseline_rows, *criteria_rows],
    }
    return {
        "sample_sizes": {
            "criteria": len(set(criteria_ids)),
            "mutants": len(baseline_killed),
            "tests": dict(tests),
        },
        "suites": {
            name: {
                "tests": tests[name],
                "requirement_coverage": _coverage_dict(criteria_ids, rows[name]),
                "mutation": _mutation_dict(killed[name]),
            }
            for name in SUITES
        },
        "mutants": [
            {
                "mutant": name,
                "description": descriptions.get(name, ""),
                "killed_by": {suite: killed[suite][name] for suite in SUITES},
            }
            for name in sorted(baseline_killed)
        ],
    }


def _result_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Baseline runner rows reduced to what coverage needs."""
    return [
        {"outcome": row["outcome"], "requirements": row["requirements"]}
        for row in report["results"]
    ]


def _criteria_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"outcome": row["outcome"], "requirements": [row["requirement_id"]]}
        for row in report["results"]
    ]


def run_comparison(
    spec_path: Path,
    criteria_path: Path,
    work_dir: Path,
    output_dir: Path,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    """Run suites A and B on the real API and on every mutant, then compare.

    Each suite's control run on the unmutated API must pass completely; otherwise a
    "kill" could just be a broken test and the comparison is rejected.
    """
    work_dir = Path(work_dir)
    output_dir = Path(output_dir)
    criteria = load_criteria(criteria_path)
    criteria_ids = [item.id for item in criteria]
    spec = load_spec(spec_path)

    # A. baseline, tagged by the mapper.
    mapping = get_mapper("tfidf", threshold, top_k).map(
        criteria, baseline_tests_from_spec(spec)
    )
    paths = write_reports(mapping, work_dir / "mapping")
    baseline_dir = work_dir / "baseline-results"
    baseline = evaluate_mutants(
        spec_path,
        work_dir / "baseline",
        baseline_dir,
        mapping_path=paths["mapping.json"],
    )
    baseline_control = json.loads(
        (baseline_dir / "control.json").read_text(encoding="utf-8")
    )

    # B. criteria-driven suite.
    acceptance_dir = work_dir / "acceptance"
    manifest = export_acceptance_tests(spec_path, criteria_path, acceptance_dir)
    results_dir = work_dir / "acceptance-results"
    control = run_exported_acceptance(
        acceptance_dir, manifest, results_dir / "control.json"
    )
    if control["summary"].get("passed", 0) != control["summary"]["total"]:
        raise EvaluationError(
            "the criteria-driven control run on the unmutated API did not pass "
            f"completely ({control['summary']}); mutation results would be "
            "meaningless"
        )
    criteria_killed: dict[str, bool] = {}
    for name in sorted(MUTANTS):
        report = run_exported_acceptance(
            acceptance_dir, manifest, results_dir / f"mutant-{name}.json", mutant=name
        )
        criteria_killed[name] = any(
            row["outcome"] == "failed" for row in report["results"]
        )

    comparison = build_comparison(
        criteria_ids,
        baseline_rows=_result_rows(baseline_control),
        criteria_rows=_criteria_rows(control),
        baseline_killed={
            row["mutant"]: row["killed"] for row in baseline["mutants"]
        },
        criteria_killed=criteria_killed,
        tests={
            "baseline": baseline_control["summary"]["total"],
            "criteria": control["summary"]["total"],
            "combined": baseline_control["summary"]["total"]
            + control["summary"]["total"],
        },
        descriptions=MUTANTS,
    )
    comparison["configured"] = {"threshold": threshold, "top_k": top_k}
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "comparison.json").write_text(
        json.dumps(comparison, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_dir / "comparison.md").write_text(
        comparison_markdown(comparison), encoding="utf-8", newline="\n"
    )
    return comparison


def _fmt(value: float | None) -> str:
    return "undefined" if value is None else f"{value:.3f}"


_LABELS = {
    "baseline": "A. Baseline (schema only)",
    "criteria": "B. Criteria-driven",
    "combined": "C. Combined (A + B)",
}


def comparison_markdown(report: dict[str, Any]) -> str:
    """A human-readable comparison with sample sizes and limitations."""
    sizes = report["sample_sizes"]
    lines = [
        "# Suite comparison",
        "",
        f"Sample sizes: {sizes['criteria']} criteria, {sizes['mutants']} mutants, "
        f"{sizes['tests']['baseline']} baseline tests, "
        f"{sizes['tests']['criteria']} criteria-driven tests. Mapper threshold "
        f"{report['configured']['threshold']}, top-k {report['configured']['top_k']}.",
        "",
        "Requirement coverage and mutation score answer different questions. They "
        "are shown side by side and are never combined into one number.",
        "",
        "## Requirement coverage",
        "",
        "Share of criteria with at least one passing test of the suite tagged to "
        "them (control run on the real API).",
        "",
        "| suite | tests | criteria covered | coverage |",
        "|---|---|---|---|",
    ]
    for name in SUITES:
        suite = report["suites"][name]
        cov = suite["requirement_coverage"]
        lines.append(
            f"| {_LABELS[name]} | {suite['tests']} | "
            f"{len(cov['covered'])} of {cov['criteria']} | {_fmt(cov['coverage'])} |"
        )
    lines += ["", "Criteria with no passing tagged test:", ""]
    for name in SUITES:
        cov = report["suites"][name]["requirement_coverage"]
        missing = [*cov["mapped_not_passing"], *cov["not_mapped"]]
        lines.append(f"- {_LABELS[name]}: {', '.join(missing) if missing else 'none'}")
    lines += [
        "",
        "## Mutation score",
        "",
        "Killed means at least one test of the suite failed on the mutant. A passing "
        "control run is required first. Setup errors never count as kills.",
        "",
        "| suite | mutants killed | mutation score | survived |",
        "|---|---|---|---|",
    ]
    for name in SUITES:
        mutation = report["suites"][name]["mutation"]
        survived = ", ".join(mutation["survived"]) or "none"
        lines.append(
            f"| {_LABELS[name]} | {mutation['killed']} of {mutation['executed']} | "
            f"{_fmt(mutation['mutation_score'])} | {survived} |"
        )
    lines += ["", "### Per mutant", "", "| mutant | A | B | C |", "|---|---|---|---|"]
    for row in report["mutants"]:
        marks = ["killed" if row["killed_by"][name] else "survived" for name in SUITES]
        lines.append(f"| {row['mutant']} | " + " | ".join(marks) + " |")
    step = (
        f"One extra kill moves the score by {1 / sizes['mutants']:.1f}."
        if sizes["mutants"]
        else "No mutants were executed, so the score is undefined."
    )
    lines += [
        "",
        "## Limitations",
        "",
        "- **Coverage is not comparable across suites.** For A the tags come from "
        "the mapper, so its coverage inherits the mapper's errors. For B and C the "
        "tests were generated from the criteria being counted, so their coverage is "
        "partly circular. Coverage is a traceability measure, not proof that the "
        "tests are good.",
        "- **A passing test is not a verified requirement.** Mapped is not verified.",
        "- **Mutation score is the independent check, but it is small.** The "
        f"{sizes['mutants']} mutants were written by hand inside this project, "
        "not by an independent party, for this one demo API, and some target "
        "behaviours the criteria describe, so "
        "B's kills partly reflect that the same hands wrote both. " + step,
        "- **No equivalent mutants were declared.** A survivor may be a real gap or "
        "a mutant no test could distinguish; each survivor should be read, not "
        "counted blindly.",
        "- **C is the union of A and B**, taken from separate runs. See the module "
        "docstring for why that equals one combined run.",
        "- **Results describe this demo API and these criteria.** They are not "
        "estimates for other APIs.",
        "",
    ]
    return "\n".join(lines)


def register_commands(commands: Any) -> None:
    command = commands.add_parser(
        "compare",
        help="compare baseline, criteria-driven and combined suites",
    )
    command.add_argument("--spec", type=Path, default=Path("specs/openapi.json"))
    command.add_argument("--criteria", type=Path, default=Path("criteria.yaml"))
    command.add_argument(
        "--work-dir", type=Path, default=Path("generated_comparison")
    )
    command.add_argument("--output", type=Path, default=Path("reports/comparison"))
    command.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    command.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)


def handle(arguments: argparse.Namespace) -> int:
    try:
        report = run_comparison(
            arguments.spec,
            arguments.criteria,
            arguments.work_dir,
            arguments.output,
            threshold=arguments.threshold,
            top_k=arguments.top_k,
        )
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    for name in SUITES:
        suite = report["suites"][name]
        cov = suite["requirement_coverage"]
        mutation = suite["mutation"]
        print(
            f"{_LABELS[name]}: covers {len(cov['covered'])} of {cov['criteria']} "
            f"criteria; kills {mutation['killed']} of {mutation['executed']} mutants"
        )
    print(f"Reports written to {arguments.output}")
    return 0
