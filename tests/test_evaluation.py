"""Evaluation harness: sampling, label validation, mapping scores, mutants."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from assay import cli, evaluation, runner
from assay.demo_api import MUTANTS, create_app
from assay.mapping import (
    baseline_tests_from_spec,
    load_criteria,
    map_requirements,
)

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "specs" / "openapi.json"
CRITERIA = ROOT / "criteria.yaml"

# Labels are only valid for these exact generated tests. If this fails, the case
# IDs changed: version the change and relabel rather than editing this value.
FROZEN_CASE_ID_FINGERPRINT = (
    "6b044adc502881259b729313410abd702b43603890fad93ed891afdabf879422"
)


@pytest.fixture(scope="module")
def tests_and_criteria():
    spec = runner.load_spec(SPEC)
    return baseline_tests_from_spec(spec), load_criteria(CRITERIA)


# --- the freeze ----------------------------------------------------------------


def test_case_ids_are_frozen(tests_and_criteria):
    tests, _ = tests_and_criteria
    assert evaluation.case_id_fingerprint(tests) == FROZEN_CASE_ID_FINGERPRINT


def test_fingerprint_changes_when_an_id_changes(tests_and_criteria):
    tests, _ = tests_and_criteria
    altered = [tests[0].model_copy(update={"case_id": "BASE-000000000000"})]
    altered += tests[1:]
    assert evaluation.case_id_fingerprint(altered) != FROZEN_CASE_ID_FINGERPRINT


def test_committed_sheet_matches_a_fresh_sample(tests_and_criteria):
    """The committed sheet is reproducible and tied to the current tests.

    Labels and notes are the human's; every other column must be unchanged.
    """
    tests, criteria = tests_and_criteria
    fresh = evaluation.sample_pairs(criteria, tests)
    with (ROOT / "data" / "labelled_pairs.csv").open(encoding="utf-8") as handle:
        committed = list(csv.DictReader(handle))
    keep = [c for c in evaluation.LABEL_COLUMNS if c not in ("label", "notes")]
    assert [{k: r[k] for k in keep} for r in committed] == [
        {k: r[k] for k in keep} for r in fresh
    ]
    meta = json.loads((ROOT / "data" / "labelled_pairs.meta.json").read_text())
    assert meta["case_id_fingerprint"] == FROZEN_CASE_ID_FINGERPRINT


# --- sampling ------------------------------------------------------------------


def test_sample_is_deterministic_and_seed_dependent(tests_and_criteria):
    tests, criteria = tests_and_criteria
    first = evaluation.sample_pairs(criteria, tests, seed=1)
    assert first == evaluation.sample_pairs(criteria, tests, seed=1)
    other = evaluation.sample_pairs(criteria, tests, seed=2)
    assert [r["pair_id"] for r in first] != [r["pair_id"] for r in other]


def test_sample_size_strata_and_splits(tests_and_criteria):
    tests, criteria = tests_and_criteria
    rows = evaluation.sample_pairs(criteria, tests)
    assert len(rows) == sum(evaluation.DEFAULT_COUNTS.values()) == 40
    assert len({r["pair_id"] for r in rows}) == 40
    result = map_requirements(criteria, tests)
    strata = evaluation.classify_pairs(criteria, tests, result)
    counted: dict[str, int] = {}
    for row in rows:
        name = strata[(row["requirement_id"], row["case_id"])]
        counted[name] = counted.get(name, 0) + 1
    assert counted == evaluation.DEFAULT_COUNTS
    splits = [r["split"] for r in rows]
    assert abs(splits.count("dev") - splits.count("test")) <= 3
    for stratum in evaluation.DEFAULT_COUNTS:
        halves = {
            split: sum(
                1
                for r in rows
                if r["split"] == split
                and strata[(r["requirement_id"], r["case_id"])] == stratum
            )
            for split in evaluation.SPLITS
        }
        assert abs(halves["dev"] - halves["test"]) <= 1


def test_sample_spreads_across_criteria(tests_and_criteria):
    tests, criteria = tests_and_criteria
    rows = evaluation.sample_pairs(criteria, tests)
    assert len({r["requirement_id"] for r in rows}) >= 15


def test_sheet_is_blind_to_the_mapper(tests_and_criteria):
    """No score, rank, eligibility or stratum column may reach the labeller."""
    for name in evaluation.LABEL_COLUMNS:
        for banned in ("score", "rank", "eligible", "stratum", "mapped", "predicted"):
            assert banned not in name
    tests, criteria = tests_and_criteria
    for row in evaluation.sample_pairs(criteria, tests):
        assert row["label"] == "" and row["notes"] == ""


def test_sample_rejects_unknown_stratum(tests_and_criteria):
    tests, criteria = tests_and_criteria
    with pytest.raises(evaluation.EvaluationError):
        evaluation.sample_pairs(criteria, tests, counts={"nonsense": 3})


def test_classify_pairs_on_real_data(tests_and_criteria):
    tests, criteria = tests_and_criteria
    result = map_requirements(criteria, tests)
    strata = evaluation.classify_pairs(criteria, tests, result)
    assert len(strata) == len(tests) * len(criteria)
    health = next(t for t in tests if t.path == "/health")
    assert strata[("REQ-001", health.case_id)] == "eligible"
    assert strata[("REQ-003", health.case_id)] == "other_operation"
    # REQ-002 has same-operation tests, but the rules ruled them all out.
    assert strata[("REQ-002", health.case_id)] == "same_operation"


# --- label sheets --------------------------------------------------------------


def _filled_sheet(tmp_path, tests, criteria, label_for):
    rows = evaluation.sample_pairs(criteria, tests)
    for row in rows:
        row["label"] = label_for(row)
    sheet, meta = tmp_path / "labels.csv", tmp_path / "meta.json"
    evaluation.write_label_sheet(rows, sheet, meta, tests=tests, seed=1)
    return sheet, meta, rows


def _rewrite(sheet, edit):
    with sheet.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        names = reader.fieldnames
    edit(rows)
    with sheet.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=names, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def test_filled_sheet_loads(tmp_path, tests_and_criteria):
    tests, criteria = tests_and_criteria
    sheet, meta, rows = _filled_sheet(tmp_path, tests, criteria, lambda r: "0")
    loaded = evaluation.load_labels(sheet, criteria, tests, meta)
    assert len(loaded) == len(rows)


@pytest.mark.parametrize(
    "edit,message",
    [
        (lambda rows: rows[0].update(label=""), "label must be 1 or 0"),
        (lambda rows: rows[0].update(label="yes"), "label must be 1 or 0"),
        (lambda rows: rows[0].update(split="train"), "split"),
        (lambda rows: rows[0].update(requirement_id="REQ-999"), "unknown requirement"),
        (lambda rows: rows[0].update(case_id="BASE-FFFFFFFFFFFF"), "unknown case"),
        (lambda rows: rows[0].update(pair_id="x|y"), "pair_id"),
        (lambda rows: rows.append(dict(rows[0])), "duplicate"),
    ],
)
def test_bad_label_sheets_are_rejected(tmp_path, tests_and_criteria, edit, message):
    tests, criteria = tests_and_criteria
    sheet, meta, _ = _filled_sheet(tmp_path, tests, criteria, lambda r: "0")
    _rewrite(sheet, edit)
    with pytest.raises(evaluation.EvaluationError, match=message):
        evaluation.load_labels(sheet, criteria, tests, meta)


def test_sheet_with_missing_column_or_no_rows_is_rejected(tmp_path, tests_and_criteria):
    tests, criteria = tests_and_criteria
    sheet = tmp_path / "bad.csv"
    sheet.write_text("pair_id,label\n", encoding="utf-8")
    with pytest.raises(evaluation.EvaluationError, match="missing columns"):
        evaluation.load_labels(sheet, criteria, tests)
    sheet.write_text(",".join(evaluation.LABEL_COLUMNS) + "\n", encoding="utf-8")
    with pytest.raises(evaluation.EvaluationError, match="no labelled pairs"):
        evaluation.load_labels(sheet, criteria, tests)


def test_labels_made_for_different_test_ids_are_rejected(tmp_path, tests_and_criteria):
    tests, criteria = tests_and_criteria
    sheet, meta, _ = _filled_sheet(tmp_path, tests, criteria, lambda r: "0")
    data = json.loads(meta.read_text())
    data["case_id_fingerprint"] = "0" * 64
    meta.write_text(json.dumps(data))
    with pytest.raises(evaluation.EvaluationError, match="test IDs changed"):
        evaluation.load_labels(sheet, criteria, tests, meta)


# --- mapping evaluation --------------------------------------------------------


def _oracle(tests, criteria, threshold=0.4):
    result = map_requirements(criteria, tests, threshold=threshold)
    predicted = {(r.requirement_id, m.case_id) for r in result.rows for m in r.matches}
    return (
        lambda row: "1" if (row["requirement_id"], row["case_id"]) in predicted else "0"
    )


def test_a_perfect_labeller_gives_perfect_scores(tmp_path, tests_and_criteria):
    tests, criteria = tests_and_criteria
    sheet, meta, _ = _filled_sheet(tmp_path, tests, criteria, _oracle(tests, criteria))
    rows = evaluation.load_labels(sheet, criteria, tests, meta)
    report = evaluation.evaluate_mapping(rows, criteria, tests, threshold=0.4, top_k=3)
    for split in evaluation.SPLITS:
        scores = report["configured_threshold"][split]
        assert scores["precision"] in (1.0, None)
        assert scores["recall"] in (1.0, None)
        assert scores["false_positives"] == scores["false_negatives"] == 0
    assert report["labelled_pairs"] == 40


def test_an_opposite_labeller_gives_zero_scores(tmp_path, tests_and_criteria):
    tests, criteria = tests_and_criteria
    oracle = _oracle(tests, criteria)
    sheet, meta, _ = _filled_sheet(
        tmp_path, tests, criteria, lambda r: "0" if oracle(r) == "1" else "1"
    )
    rows = evaluation.load_labels(sheet, criteria, tests, meta)
    report = evaluation.evaluate_mapping(rows, criteria, tests, threshold=0.4, top_k=3)
    for split in evaluation.SPLITS:
        scores = report["configured_threshold"][split]
        assert scores["true_positives"] == 0
        assert scores["precision"] in (0.0, None)


def test_sweep_uses_dev_labels_only(tmp_path, tests_and_criteria):
    tests, criteria = tests_and_criteria
    oracle = _oracle(tests, criteria)
    sheet, meta, _ = _filled_sheet(tmp_path, tests, criteria, oracle)
    rows = evaluation.load_labels(sheet, criteria, tests, meta)
    base = evaluation.evaluate_mapping(rows, criteria, tests, threshold=0.4, top_k=3)
    flipped = [
        {**r, "label": "1" if r["label"] == "0" else "0"} if r["split"] == "test" else r
        for r in rows
    ]
    again = evaluation.evaluate_mapping(
        flipped, criteria, tests, threshold=0.4, top_k=3
    )
    assert again["dev_sweep"] == base["dev_sweep"]
    assert again["dev_chosen_threshold"] == base["dev_chosen_threshold"]
    assert again["configured_threshold"]["test"] != base["configured_threshold"]["test"]


def test_report_shape_and_markdown(tmp_path, tests_and_criteria):
    tests, criteria = tests_and_criteria
    sheet, meta, _ = _filled_sheet(tmp_path, tests, criteria, _oracle(tests, criteria))
    rows = evaluation.load_labels(sheet, criteria, tests, meta)
    report = evaluation.evaluate_mapping(rows, criteria, tests, threshold=0.4, top_k=3)
    assert [e["threshold"] for e in report["dev_sweep"]] == list(
        evaluation.SWEEP_THRESHOLDS
    )
    assert set(report["by_stratum"]) <= set(evaluation.DEFAULT_COUNTS)
    text = evaluation.mapping_markdown(report)
    assert "stratified sample" in text and "Dev sweep" in text


# --- mutants in the demo API ---------------------------------------------------

_PRODUCT = {"name": "Notebook", "price": 5.5, "category": "book"}

# mutant -> (method, path, json body, status on the real API, status on the mutant)
_MUTANT_PROBES = {
    "name_min_length_2": ("POST", "/products", {**_PRODUCT, "name": "ab"}, 422, 201),
    "name_max_length_41": (
        "POST",
        "/products",
        {**_PRODUCT, "name": "x" * 41},
        422,
        201,
    ),
    "price_allows_zero": ("POST", "/products", {**_PRODUCT, "price": 0}, 422, 201),
    "price_max_exclusive": (
        "POST",
        "/products",
        {**_PRODUCT, "price": 10000},
        201,
        422,
    ),
    "category_accepts_toy": (
        "POST",
        "/products",
        {**_PRODUCT, "category": "toy"},
        422,
        201,
    ),
    "stock_allows_negative_one": (
        "PATCH",
        "/products/1/stock",
        {"quantity": -1},
        422,
        200,
    ),
    "order_quantity_max_11": (
        "POST",
        "/orders",
        {"product_id": 1, "quantity": 11},
        422,
        201,
    ),
    "get_unknown_product_200": ("GET", "/products/99999", None, 404, 200),
    "order_unknown_product_201": (
        "POST",
        "/orders",
        {"product_id": 99999, "quantity": 1},
        404,
        201,
    ),
}


def _call(client, method, path, body):
    return client.request(method, path, json=body).status_code


def test_mutant_registry_size_and_coverage_of_probes():
    assert 5 <= len(MUTANTS) <= 10
    assert set(_MUTANT_PROBES) | {"health_body_wrong"} == set(MUTANTS)


@pytest.mark.parametrize("name", sorted(_MUTANT_PROBES))
def test_each_mutant_changes_behaviour_and_the_real_api_does_not(name):
    method, path, body, real, mutated = _MUTANT_PROBES[name]
    assert _call(TestClient(create_app()), method, path, body) == real
    assert _call(TestClient(create_app(mutant=name)), method, path, body) == mutated


def test_health_body_mutant():
    assert TestClient(create_app()).get("/health").json() == {"status": "ok"}
    mutated = TestClient(create_app(mutant="health_body_wrong"))
    assert mutated.get("/health").json() == {"status": "up"}


def test_mutants_are_off_by_default():
    client = TestClient(create_app())
    assert _call(client, "POST", "/products", {**_PRODUCT, "name": "ab"}) == 422
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/products/99999").status_code == 404


def test_unknown_mutant_is_rejected_with_the_known_names():
    with pytest.raises(ValueError, match="name_min_length_2"):
        create_app(mutant="nope")


def test_runner_clears_a_stray_mutant_variable(tmp_path, monkeypatch):
    """A leftover ASSAY_MUTANT in the shell must not turn a mutant on silently."""
    monkeypatch.setenv("ASSAY_MUTANT", "name_min_length_2")
    entries = runner.export_tests(SPEC, tmp_path / "suite")
    report = runner.run_tests(tmp_path / "suite", entries, tmp_path / "r.json")
    assert report["summary"]["passed"] == report["summary"]["total"]


# --- mutation evaluation -------------------------------------------------------

# Characterisation of today's baseline suite on these ten hand-written mutants.
# These pin the *current* generated tests, not a claim about test quality in general.
_KILLED_BY_BASELINE = {
    "name_max_length_41",
    "name_min_length_2",
    "order_quantity_max_11",
    "price_allows_zero",
    "price_max_exclusive",
    "stock_allows_negative_one",
}


@pytest.fixture(scope="module")
def full_mutation_run(tmp_path_factory):
    base = tmp_path_factory.mktemp("mutation")
    return evaluation.evaluate_mutants(SPEC, base / "suite", base / "out")


def test_known_mutants_are_killed_and_known_survivors_survive(full_mutation_run):
    killed = {m["mutant"] for m in full_mutation_run["mutants"] if m["killed"]}
    assert killed == _KILLED_BY_BASELINE
    assert set(full_mutation_run["score"]["survived"]) == set(MUTANTS) - killed


def test_mutation_result_shape(full_mutation_run):
    assert (
        full_mutation_run["control"]["passed"] == full_mutation_run["control"]["total"]
    )
    score = full_mutation_run["score"]
    assert score["executed"] == len(MUTANTS)
    assert score["killed"] == len(_KILLED_BY_BASELINE)
    assert score["mutation_score"] == len(_KILLED_BY_BASELINE) / len(MUTANTS)
    names = [m["mutant"] for m in full_mutation_run["mutants"]]
    assert names == sorted(names)
    for record in full_mutation_run["mutants"]:
        assert record["killed"] == bool(record["detecting_cases"])
        assert record["detecting_cases"] == sorted(record["detecting_cases"])


def test_selecting_one_mutant_runs_only_that_one(tmp_path):
    result = evaluation.evaluate_mutants(
        SPEC, tmp_path / "suite", tmp_path / "out", selected=["price_allows_zero"]
    )
    assert [m["mutant"] for m in result["mutants"]] == ["price_allows_zero"]
    assert result["score"]["executed"] == 1
    assert not (tmp_path / "out" / "mutant-name_min_length_2.json").exists()


def test_mutation_output_is_byte_identical_across_runs(tmp_path):
    outputs = []
    for run in ("a", "b"):
        evaluation.evaluate_mutants(
            SPEC,
            tmp_path / run / "suite",
            tmp_path / run / "out",
            selected=["name_min_length_2"],
        )
        outputs.append((tmp_path / run / "out" / "mutation.json").read_bytes())
    assert outputs[0] == outputs[1]


def test_unknown_or_duplicate_mutants_are_rejected(tmp_path):
    with pytest.raises(evaluation.EvaluationError, match="unknown mutants"):
        evaluation.evaluate_mutants(
            SPEC, tmp_path / "s", tmp_path / "o", selected=["ghost"]
        )
    with pytest.raises(evaluation.EvaluationError, match="unique"):
        evaluation.evaluate_mutants(
            SPEC,
            tmp_path / "s",
            tmp_path / "o",
            selected=["price_allows_zero", "price_allows_zero"],
        )


def test_a_failing_control_run_invalidates_the_evaluation(tmp_path, monkeypatch):
    def broken(output_dir, entries, results_path, mutant=None):
        return {"summary": {"total": 43, "passed": 42, "failed": 1}, "results": []}

    monkeypatch.setattr(evaluation, "run_tests", broken)
    with pytest.raises(evaluation.EvaluationError, match="control run"):
        evaluation.evaluate_mutants(SPEC, tmp_path / "s", tmp_path / "o")


def test_mutation_markdown_lists_survivors(full_mutation_run):
    text = evaluation.mutation_markdown(full_mutation_run)
    assert "6 of 10" in text and "not exhaustive" in text
    assert text.count("| NO |") == len(MUTANTS) - len(_KILLED_BY_BASELINE)


# --- command line ---------------------------------------------------------------


def test_cli_sample_refuses_to_overwrite_labels(tmp_path, capsys):
    labels, meta = tmp_path / "l.csv", tmp_path / "m.json"
    base = ["evaluate", "sample", "--spec", str(SPEC), "--criteria", str(CRITERIA)]
    base += ["--labels", str(labels), "--meta", str(meta)]
    assert cli.main(base) == 0
    original = labels.read_text()
    assert cli.main(base) == 2
    assert "refusing to overwrite" in capsys.readouterr().err
    assert labels.read_text() == original
    assert cli.main([*base, "--force"]) == 0


def test_cli_mapping_rejects_blank_labels_and_accepts_filled(tmp_path, capsys):
    labels, meta = tmp_path / "l.csv", tmp_path / "m.json"
    common = ["--spec", str(SPEC), "--criteria", str(CRITERIA)]
    common += ["--labels", str(labels), "--meta", str(meta)]
    assert cli.main(["evaluate", "sample", *common]) == 0
    out = tmp_path / "reports"
    run = ["evaluate", "mapping", *common, "--output", str(out)]
    assert cli.main(run) == 2
    assert "label must be 1 or 0" in capsys.readouterr().err
    _rewrite(labels, lambda rows: [r.update(label="0") for r in rows])
    assert cli.main(run) == 0
    assert (out / "mapping.json").exists() and (out / "mapping.md").exists()


def test_cli_mapping_with_missing_labels_is_an_error(tmp_path, capsys):
    missing = tmp_path / "nope.csv"
    code = cli.main(
        ["evaluate", "mapping", "--spec", str(SPEC), "--criteria", str(CRITERIA)]
        + ["--labels", str(missing), "--output", str(tmp_path / "o")]
    )
    assert code == 2
    assert "cannot read" in capsys.readouterr().err


def test_cli_mutants_end_to_end(tmp_path, capsys):
    code = cli.main(
        ["evaluate", "mutants", "--spec", str(SPEC)]
        + ["--work-dir", str(tmp_path / "suite"), "--output", str(tmp_path / "out")]
        + ["--mutant", "order_quantity_max_11"]
    )
    assert code == 0
    assert "Killed 1 of 1 mutants" in capsys.readouterr().out
    assert (tmp_path / "out" / "mutation.md").exists()
    code = cli.main(
        ["evaluate", "mutants", "--spec", str(SPEC), "--mutant", "ghost"]
        + ["--work-dir", str(tmp_path / "s2"), "--output", str(tmp_path / "o2")]
    )
    assert code == 2


def test_only_failed_tests_kill_a_mutant_not_setup_errors():
    rows = [
        {"case_id": "B", "outcome": "failed"},
        {"case_id": "A", "outcome": "failed"},
        {"case_id": "C", "outcome": "error"},
        {"case_id": "D", "outcome": "not_run"},
        {"case_id": "E", "outcome": "passed"},
    ]
    assert evaluation.detecting_cases(rows) == ["A", "B"]
    assert evaluation.detecting_cases([{"case_id": "C", "outcome": "error"}]) == []
