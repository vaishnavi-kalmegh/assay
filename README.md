# assay

**Offline-first API test generation and requirement traceability.**

assay is a learning-focused portfolio project for AI/ML engineering and QA automation. Given an OpenAPI specification and structured acceptance criteria, the planned pipeline will generate runnable pytest tests, execute them against a local demo API, and report requirement coverage and mutation-testing results.

The MVP is deterministic and fully offline. No API key is required. Optional LLM support will remain a disabled interface stub.

## Status

Milestone 5 — evaluation harness (metrics, hand-written API mutants, a blind labelling sheet) on top of offline requirement mapping, the pytest exporter and runner and the schema baseline. Labelling is still to be done, so no mapping precision or recall exists yet. See docs/SCHEMA_BASELINE.md, docs/RUNNER.md, docs/MAPPING.md, docs/METRICS.md and docs/LABELLING.md, and PLAN.md for the project roadmap.

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
    assay map  # map criteria.yaml to those tests and report the gaps
    assay evaluate mutants  # run the tests against 10 hand-written API mutants

The OpenAPI export is sorted and deterministic. CI runs on Ubuntu 24.04. The schema-only baseline uses only the OpenAPI document and never reads criteria.yaml. It generates normalized valid, boundary, invalid-type, missing-required, and invalid-enum cases; unsupported schema constructs fail with UnsupportedSchemaError.
