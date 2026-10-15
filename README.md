# assay

**Offline-first API test generation and requirement traceability.**

assay is a learning-focused portfolio project for AI/ML engineering and QA automation. Given an OpenAPI specification and acceptance criteria, the planned pipeline will generate runnable pytest tests, execute them against a local demo API, and report requirement coverage and mutation-testing results.

The MVP will be deterministic and fully offline. No API key is required. Optional LLM support will remain a disabled interface stub.

## Status

Milestone 0 — repository skeleton and CI. See [PLAN.md](PLAN.md) for architecture, milestones, evaluation metrics, and open decisions.

## Development

Requires Python 3.11 or newer.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
pytest
ruff check .
```

The demo API and end-to-end workflow will be added in later milestones.
