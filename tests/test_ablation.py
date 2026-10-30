"""The ablation of rules versus TF-IDF, on the committed labelled pairs."""

from __future__ import annotations

from pathlib import Path

import pytest

from assay import ablation, cli, evaluation
from assay.mapping import baseline_tests_from_spec, load_criteria
from assay.runner import load_spec

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def data():
    criteria = load_criteria(ROOT / "criteria.yaml")
    tests = baseline_tests_from_spec(load_spec(ROOT / "specs" / "openapi.json"))
    rows = evaluation.load_labels(
        ROOT / "data" / "labelled_pairs.csv",
        criteria,
        tests,
        ROOT / "data" / "labelled_pairs.meta.json",
    )
    return criteria, tests, rows


@pytest.fixture(scope="module")
def report(data):
    criteria, tests, rows = data
    return ablation.evaluate_ablation(rows, criteria, tests, threshold=0.4, top_k=3)


def test_the_report_covers_all_labelled_pairs(report):
    assert report["labelled_pairs"] == 40
    assert report["positives"] == 5
    for variant in ablation.VARIANTS:
        s = report["variants"][variant]["all"]
        assert sum(s[k] for k in ("true_positives", "false_positives",
                                  "false_negatives", "true_negatives")) == 40


def test_rules_alone_find_every_positive_but_with_many_false_positives(report):
    s = report["variants"]["rules_only"]["all"]
    counts = (s["true_positives"], s["false_positives"], s["false_negatives"])
    assert counts == (5, 9, 0)


def test_adding_tfidf_removes_the_false_positives_on_this_sample(report):
    s = report["variants"]["rules_tfidf"]["all"]
    counts = (s["true_positives"], s["false_positives"], s["false_negatives"])
    assert counts == (5, 0, 0)


def test_tfidf_alone_is_worse_than_the_combination(report):
    alone = report["variants"]["tfidf_only"]["all"]
    both = report["variants"]["rules_tfidf"]["all"]
    assert alone["precision"] < both["precision"]
    assert alone["recall"] < both["recall"]


def test_the_configured_variant_matches_the_evaluation_report(data, report):
    criteria, tests, rows = data
    plain = evaluation.evaluate_mapping(rows, criteria, tests, threshold=0.4, top_k=3)
    for split in ("dev", "test"):
        assert (
            report["variants"]["rules_tfidf"][split]["precision"]
            == plain["configured_threshold"][split]["precision"]
        )
        assert (
            report["variants"]["rules_tfidf"][split]["recall"]
            == plain["configured_threshold"][split]["recall"]
        )


def test_tfidf_only_respects_top_k(data):
    criteria, tests, _ = data
    predicted = ablation.predictions(
        "tfidf_only", criteria, tests, threshold=0.0, top_k=2
    )
    per_criterion: dict[str, int] = {}
    for requirement, _case in predicted:
        per_criterion[requirement] = per_criterion.get(requirement, 0) + 1
    assert max(per_criterion.values()) == 2


def test_unknown_variant_is_rejected(data):
    criteria, tests, _ = data
    with pytest.raises(ValueError):
        ablation.predictions("nope", criteria, tests, threshold=0.4, top_k=3)


def test_the_cli_writes_reports_identical_to_the_committed_ones(tmp_path, capsys):
    code = cli.main(["evaluate", "ablation", "--output", str(tmp_path)])

    assert code == 0
    assert "Rules only: precision" in capsys.readouterr().out
    for name in ("ablation.json", "ablation.md"):
        assert (tmp_path / name).read_bytes() == (
            ROOT / "docs" / "results" / name
        ).read_bytes()
