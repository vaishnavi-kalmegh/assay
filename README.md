# assay

**Offline-first API test generation and requirement traceability.**

assay is a learning-focused portfolio project for AI/ML engineering and QA automation. Given an OpenAPI specification and structured acceptance criteria, the planned pipeline will generate runnable pytest tests, execute them against a local demo API, and report requirement coverage and mutation-testing results.

The MVP is deterministic and fully offline. No API key is required. Optional LLM support will remain a disabled interface stub.

## Status

Milestone 5 — evaluation harness (metrics, hand-written API mutants, a blind labelling sheet) on top of offline requirement mapping, the pytest exporter and runner and the schema baseline. The 40-pair sheet is labelled (5 positives). At the default threshold 0.4 (frozen, not tuned, because the dev half has only 1 positive) the mapper scores 1 of 1 on the development half and 4 of 4 on the test half with no false positives, but the rules were written after seeing labelled errors, one person labelled, and 5 positives is too few for a quality claim; the second labelling pass is still pending. See docs/SCHEMA_BASELINE.md, docs/RUNNER.md, docs/MAPPING.md, docs/METRICS.md and docs/LABELLING.md, and PLAN.md for the project roadmap.

## Development

Requires Python 3.11 or newer.

    python -m venv .venv
    # Windows: .venv\Scripts\activate
    # macOS/Linux: source .venv/bin/activate
    python -m pip install --upgrade pip
    python -m pip install -e ".[dev]"
    pytest
    ruff check .
    python scripts/export_openapi.py
    assay run  # export, run and report the generated baseline tests
    assay acceptance  # generate and run directly asserted tests from criteria.yaml
    assay map  # map criteria.yaml to baseline tests and report the gaps
    assay evaluate mutants  # run the tests against 10 hand-written API mutants
    assay evaluate mapping  # score the mapper on a filled-in label sheet
    assay evaluate relabel  # blank subset of the pairs to label again later
    assay evaluate agreement  # compare the two labelling passes

The OpenAPI export is sorted and deterministic. CI runs on Ubuntu 24.04. The schema-only baseline uses only the OpenAPI document and never reads criteria.yaml. It generates normalized valid, boundary, invalid-type, missing-required, and invalid-enum cases; unsupported schema constructs fail with UnsupportedSchemaError. The separate `assay acceptance` command turns the supported structured criteria into one executable pytest test per requirement, including response-body assertions and unknown-resource checks. It writes generated files to `generated_acceptance/` and a deterministic result report to `reports/acceptance-results.json`.
