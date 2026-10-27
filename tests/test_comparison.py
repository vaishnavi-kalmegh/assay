"""M6: the three-suite comparison."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assay import cli, comparison
from assay.evaluation import EvaluationError

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "specs" / "openapi.json"
CRITERIA = ROOT / "criteria.yaml"


# --- pure assembly, checked against a hand-worked example -------------------------


def _worked_example():
    """Three criteria, three mutants.

    Baseline: REQ-001 passes, REQ-002 is tagged but its test fails, REQ-003 has no
    tagged test. It kills m1 only.
    Criteria-driven: REQ-002 and REQ-003 pass. It kills m2 only.
    Nothing kills m3.
    """
    return comparison.build_comparison(
        ["REQ-001", "REQ-002", "REQ-003"],
        baseline_rows=[
            {"outcome": "passed", "requirements": ["REQ-001"]},
            {"outcome": "failed", "requirements": ["REQ-002"]},
            {"outcome": "passed", "requirements": []},
        ],
        criteria_rows=[
            {"outcome": "passed", "requirements": ["REQ-002"]},
            {"outcome": "passed", "requirements": ["REQ-003"]},
        ],
        baseline_killed={"m1": True, "m2": False, "m3": False},
        criteria_killed={"m1": False, "m2": True, "m3": False},
        tests={"baseline": 3, "criteria": 2, "combined": 5},
        descriptions={"m1": "one", "m2": "two", "m3": "three"},
    )


def test_coverage_is_counted_per_suite_and_combined_takes_the_union():
    suites = _worked_example()["suites"]

    baseline = suites["baseline"]["requirement_coverage"]
    assert baseline["covered"] == ["REQ-001"]
    assert baseline["mapped_not_passing"] == ["REQ-002"]
    assert baseline["not_mapped"] == ["REQ-003"]
    assert baseline["coverage"] == pytest.approx(1 / 3)

    criteria = suites["criteria"]["requirement_coverage"]
    assert criteria["covered"] == ["REQ-002", "REQ-003"]
    assert criteria["coverage"] == pytest.approx(2 / 3)

    # REQ-002 has a failing baseline test and a passing criteria test: covered.
    combined = suites["combined"]["requirement_coverage"]
    assert combined["covered"] == ["REQ-001", "REQ-002", "REQ-003"]
    assert combined["coverage"] == 1.0


def test_mutation_score_is_counted_per_suite_and_combined_takes_the_union():
    suites = _worked_example()["suites"]

    assert suites["baseline"]["mutation"]["killed"] == 1
    assert suites["baseline"]["mutation"]["survived"] == ["m2", "m3"]
    assert suites["criteria"]["mutation"]["killed"] == 1
    assert suites["criteria"]["mutation"]["survived"] == ["m1", "m3"]
    combined = suites["combined"]["mutation"]
    assert (combined["killed"], combined["executed"]) == (2, 3)
    assert combined["survived"] == ["m3"]
    assert combined["mutation_score"] == pytest.approx(2 / 3)


def test_each_mutant_row_says_which_suites_killed_it():
    rows = {row["mutant"]: row for row in _worked_example()["mutants"]}

    assert rows["m1"]["killed_by"] == {
        "baseline": True,
        "criteria": False,
        "combined": True,
    }
    assert rows["m3"]["killed_by"] == {
        "baseline": False,
        "criteria": False,
        "combined": False,
    }


def test_coverage_and_mutation_are_never_merged_into_one_number():
    report = _worked_example()

    assert set(report["suites"]) == {"baseline", "criteria", "combined"}
    for suite in report["suites"].values():
        assert set(suite) == {"tests", "requirement_coverage", "mutation"}
    assert "score" not in report
    assert "overall" not in json.dumps(report).lower()


def test_no_mutants_means_undefined_not_zero():
    report = comparison.build_comparison(
        ["REQ-001"],
        baseline_rows=[],
        criteria_rows=[],
        baseline_killed={},
        criteria_killed={},
        tests={"baseline": 0, "criteria": 0, "combined": 0},
        descriptions={},
    )

    for suite in report["suites"].values():
        assert suite["mutation"]["mutation_score"] is None
        assert suite["requirement_coverage"]["coverage"] == 0.0
    assert "undefined" in comparison.comparison_markdown(
        {**report, "configured": {"threshold": 0.4, "top_k": 3}}
    )


def test_suites_run_against_different_mutants_are_rejected():
    with pytest.raises(EvaluationError, match="same mutants"):
        comparison.build_comparison(
            ["REQ-001"],
            baseline_rows=[],
            criteria_rows=[],
            baseline_killed={"m1": True},
            criteria_killed={"m2": True},
            tests={"baseline": 0, "criteria": 0, "combined": 0},
            descriptions={},
        )


def test_the_markdown_states_sample_sizes_and_limitations():
    report = {
        **_worked_example(),
        "configured": {"threshold": 0.4, "top_k": 3},
    }

    text = comparison.comparison_markdown(report)

    assert "3 criteria, 3 mutants, 3 baseline tests, 2 criteria-driven tests" in text
    assert "never combined into one number" in text
    assert "not comparable across suites" in text
    assert "partly circular" in text
    assert "Mapped is not verified" in text
    assert "No equivalent mutants were declared" in text
    assert "One kill is tuned to its mutant" in text
    assert "| A. Baseline (schema only) | 3 | 1 of 3 | 0.333 |" in text
    assert "| C. Combined (A + B) | 5 | 3 of 3 | 1.000 |" in text


# --- the real comparison on the committed spec and criteria -------------------------


@pytest.fixture(scope="module")
def real_run(tmp_path_factory):
    base = tmp_path_factory.mktemp("compare")
    report = comparison.run_comparison(
        SPEC, CRITERIA, base / "work", base / "reports"
    )
    return report, base


def test_the_real_comparison_matches_the_measured_numbers(real_run):
    """A characterisation of today's suites; change it only with the suites."""
    report, _ = real_run
    suites = report["suites"]

    assert report["sample_sizes"] == {
        "criteria": 18,
        "mutants": 10,
        "tests": {"baseline": 43, "criteria": 18, "combined": 61},
    }
    assert len(suites["baseline"]["requirement_coverage"]["covered"]) == 13
    assert suites["baseline"]["mutation"]["killed"] == 6
    assert len(suites["criteria"]["requirement_coverage"]["covered"]) == 18
    assert suites["criteria"]["mutation"]["killed"] == 8
    assert len(suites["combined"]["requirement_coverage"]["covered"]) == 18
    assert suites["combined"]["mutation"]["killed"] == 10
    assert suites["combined"]["mutation"]["survived"] == []


def test_combined_never_does_worse_than_either_suite(real_run):
    suites = real_run[0]["suites"]

    for key in ("killed",):
        assert suites["combined"]["mutation"][key] >= max(
            suites["baseline"]["mutation"][key], suites["criteria"]["mutation"][key]
        )
    assert len(suites["combined"]["requirement_coverage"]["covered"]) >= max(
        len(suites["baseline"]["requirement_coverage"]["covered"]),
        len(suites["criteria"]["requirement_coverage"]["covered"]),
    )


def test_toy_is_caught_only_by_the_criteria_driven_suite(real_run):
    rows = {row["mutant"]: row["killed_by"] for row in real_run[0]["mutants"]}

    assert rows["category_accepts_toy"] == {
        "baseline": False,
        "criteria": True,
        "combined": True,
    }


def test_the_baseline_misses_what_the_criteria_driven_suite_catches(real_run):
    rows = {row["mutant"]: row["killed_by"] for row in real_run[0]["mutants"]}

    for mutant in (
        "get_unknown_product_200",
        "order_unknown_product_201",
        "health_body_wrong",
    ):
        assert rows[mutant] == {
            "baseline": False,
            "criteria": True,
            "combined": True,
        }


def test_reports_are_written_and_hold_no_machine_specific_paths(real_run):
    _, base = real_run
    text = (base / "reports" / "comparison.json").read_text(encoding="utf-8")
    markdown = (base / "reports" / "comparison.md").read_text(encoding="utf-8")

    assert json.loads(text)["suites"]["combined"]["tests"] == 61
    assert str(base) not in text + markdown
    assert "/tmp" not in text + markdown


def test_the_command_reproduces_the_same_bytes(real_run, tmp_path, capsys):
    _, base = real_run

    code = cli.main(
        [
            "compare",
            "--spec",
            str(SPEC),
            "--criteria",
            str(CRITERIA),
            "--work-dir",
            str(tmp_path / "work"),
            "--output",
            str(tmp_path / "out"),
        ]
    )

    assert code == 0
    for name in ("comparison.json", "comparison.md"):
        assert (tmp_path / "out" / name).read_bytes() == (
            base / "reports" / name
        ).read_bytes()
    printed = capsys.readouterr().out
    assert "B. Criteria-driven: covers 18 of 18 criteria; kills 8 of 10" in printed


def test_a_failing_control_run_rejects_the_whole_comparison(tmp_path, monkeypatch):
    real = comparison.run_exported_acceptance

    def broken_control(output_dir, manifest, results_path, mutant=None):
        report = real(output_dir, manifest, results_path, mutant)
        if mutant is None:
            report["results"][0]["outcome"] = "failed"
            report["summary"] = {"total": 18, "failed": 1, "passed": 17}
        return report

    monkeypatch.setattr(comparison, "run_exported_acceptance", broken_control)

    with pytest.raises(EvaluationError, match="control run"):
        comparison.run_comparison(
            SPEC, CRITERIA, tmp_path / "work", tmp_path / "out"
        )
    assert not (tmp_path / "out" / "comparison.json").exists()


def test_the_committed_example_results_are_current(real_run):
    """docs/results is what a reader sees without running anything; keep it true."""
    _, base = real_run
    for name in ("comparison.json", "comparison.md"):
        committed = (ROOT / "docs" / "results" / name).read_bytes()
        assert committed == (base / "reports" / name).read_bytes(), (
            f"docs/results/{name} is stale; regenerate with "
            "`assay compare --output docs/results`"
        )
