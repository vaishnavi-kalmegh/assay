# assay

**Offline-first API test generation and requirement traceability.**

assay is a learning-focused portfolio project for AI/ML engineering and QA automation. Given an OpenAPI specification and structured acceptance criteria, the planned pipeline will generate runnable pytest tests, execute them against a local demo API, and report requirement coverage and mutation-testing results.

The MVP is deterministic and fully offline. No API key is required. Optional LLM support will remain a disabled interface stub.

## Status

Milestone 1 — local demo API and structured criteria. See PLAN.md for architecture, milestones, evaluation metrics, and open decisions.

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

The OpenAPI export is sorted and deterministic. CI runs on Ubuntu 24.04. Criteria use structured fields (id, method, path, field_rule, expected_status, and description) so future offline generation does not need to interpret free-form prose.
