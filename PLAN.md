# assay — Project Plan

## Goal

Build a small, explainable, offline-first tool that accepts an OpenAPI JSON document and acceptance criteria with stable IDs (REQ-001, etc.), generates runnable pytest tests, runs them against a local FastAPI demo service, and reports requirement traceability and mutation-testing results. No API key is ever required. Keep modules small, choices conventional, and generated output deterministic.

## Architecture

1. Read and normalize OpenAPI paths, operations, parameters, request bodies, schemas, and responses into Pydantic models.
2. Generate a schema-only baseline: valid examples, boundaries, invalid types, missing required fields, and invalid enum values. This stage must not read acceptance criteria.
3. Map tests to criteria offline using an inspectable TF-IDF/cosine-similarity or keyword baseline.
4. Identify criteria not adequately represented and conservatively plan extra tests using only evidence from criteria and the supported API schema.
5. Export ordinary pytest files, tagging mapped tests with @pytest.mark.req("REQ-xxx").
6. Execute tests against the in-process/local demo API and write structured JSON results and a traceability matrix.
7. Evaluate mapping quality on hand-labelled pairs, requirement coverage, and mutation score separately.
8. Expose an optional LLM adapter interface with a disabled stub only. No real provider integration or network calls in the MVP.

## Folder layout

- src/assay/ — package and pipeline modules.
- tests/ — unit and integration tests.
- demo_api/ — local FastAPI app (M1).
- specs/openapi.json — reproducible exported API spec (M1).
- criteria.yaml — acceptance criteria with unique IDs (M1).
- data/labelled_pairs.csv — manually labelled pairs (M5).
- docs/METRICS.md and docs/LABELLING.md — metric definitions and annotation protocol.
- generated_tests/ and reports/ — runtime artifacts, ignored by Git.
- .github/workflows/ci.yml — CI.
- pyproject.toml — package config, pinned dependencies, pytest, Ruff.

## Data flow

OpenAPI spec -> Pydantic parser -> schema-only baseline cases -> offline requirement mapper <- criteria.yaml -> gap report -> conservative targeted cases -> pytest exporter/runner -> JSON results -> metrics and traceability report.

Criteria must never enter the baseline generator. Apply the same mapper after generation when evaluating baseline requirement coverage, so comparison remains fair.

## Dependencies and decisions

Use Python 3.11+, FastAPI for the demo API only, Pydantic for typed models, pytest for execution and requirement markers, HTTPX for in-process API tests, PyYAML for criteria, scikit-learn for TF-IDF similarity, and Ruff for linting. Pin every dependency exactly with == when it is introduced. M0 intentionally pins only pytest and Ruff; add application dependencies in the milestone that first needs them. Prefer standard libraries and readable functions over extra abstractions. Sort outputs and fix any random seeds to preserve determinism.

## Milestones

Every milestone needs a deliverable, run command, automated tests, explicit pass/fail criteria, and one milestone commit. Fix any failure before adding features. Stop after each milestone and wait for review.

### M0 — Skeleton (current)
Deliverable: pyproject, pinned development dependencies, placeholder test, lint configuration, CI, README stub, and plan.
Run: python -m pip install -e ".[dev]" && pytest && ruff check .
Tests: deterministic placeholder smoke test.
Pass: tests and lint exit 0; CI uses Python 3.11; dependency pins are exact; no secret or network service is needed for tests.
Fail: any command fails, dependency is unpinned, or CI requires a secret.

### M1 — Demo API
Deliverable: 4–5 FastAPI endpoints with real required-field, min/max, enum, and status-code rules; exported specs/openapi.json; criteria.yaml with about 15–20 IDs; API tests.
Run: pytest.
Tests: valid input, missing fields, boundaries, invalid types/enums, expected statuses.
Pass: all API tests pass, spec export is reproducible, IDs are unique and criteria testable. Fail: unstable export or unclear contract.

### M2 — Offline schema baseline
Deliverable: parser and Pydantic case models generating valid, boundary, invalid-type, and missing-required-field cases without reading criteria.
Run: pytest.
Tests: supported schema shapes, boundary cases, unsupported-feature errors, repeatability.
Pass: repeated runs produce equivalent normalized cases and criteria are not loaded. Fail: criteria leakage or silent misrepresentation.

### M3 — Exporter and runner
Deliverable: ordinary runnable pytest files, requirement markers where mapped, demo API execution, JSON results.
Run: pytest plus documented export/run CLI.
Tests: collection, marker correctness, result schema, deterministic output.
Pass: generated tests run locally and each result traces to a case. Fail: tests cannot collect or execution needs hosted services.

### M4 — Requirement mapping
Deliverable: offline TF-IDF/keyword mapper, traceability matrix, coverage-gap report, disabled LLM stub.
Run: pytest plus mapping CLI.
Tests: positive/negative examples, ranking, threshold, unknown IDs, gap reporting, stub disabled.
Pass: stable offline mapping and visible uncovered criteria. Fail: missing gaps, nondeterminism, or provider calls.

### M5 — Evaluation harness
Deliverable: 30–40 hand-labelled pairs in CSV; LABELLING.md; 5–10 switchable demo API mutants; metric code and tests.
Run: pytest plus evaluation command.
Tests: metric formulas, empty-set behavior, mutant selection, known mutant kills.
Pass: label protocol is documented, mutants default off, calculations match hand-worked examples. Fail: inconsistent labels or irreproducible metrics.

### M6 — Comparison and docs
Deliverable: compare baseline vs full pipeline on requirement coverage and mutation score; final README, architecture diagram, measured results and limitations; complete CI.
Run: documented end-to-end offline commands from clean checkout.
Tests: end-to-end workflow, deterministic artifacts, metric regression tests.
Pass: reproducible numbers include sample sizes and limitations, metrics stay separate, all CI passes. Fail: unsupported claims or network dependency.

## Metrics

- Mapping precision = true-positive predicted criterion/test matches / all predicted matches.
- Mapping recall = true-positive predicted matches / all relevant matches in the labelled set.
- Requirement coverage = criteria covered by at least one passing, requirement-tagged test / all criteria in scope.
- Mutation score = valid non-equivalent mutants killed / valid non-equivalent mutants executed.
Mapping precision/recall apply only to the mapper on labelled pairs, not the schema-only baseline. Coverage, mapping quality, and mutation score must never be merged. docs/METRICS.md will provide worked examples and edge-case rules by M5. Do not claim improvements before running experiments; report measured counts, sample sizes, and limitations.

## Risks

- OpenAPI variability: support a small subset first and explicitly reject unsupported constructs.
- False matches: expose scores/thresholds and preserve unmapped criteria.
- Misleading coverage: count passing tagged tests; report mapped-but-failing tests separately.
- Weak mutation validity: document hand-picked mutants and acknowledge they are not exhaustive.
- Small single-labeller dataset: disclose limitations and measure self-agreement.
- Nondeterminism: sort inputs/outputs, fix seeds, avoid timestamps in golden artifacts, test repeatability.
- Dependency drift: exact pins and CI.
- Scope creep: enforce out-of-scope list below.

## Out of scope

Dashboard/web UI, multi-agent systems, GraphQL, multiple or real LLM providers, auth testing, performance/load testing, a general mutation framework, Docker orchestration, and persistent databases beyond in-memory demo state.

## Open decisions (defaults proposed; do not block implementation)

1. Baseline requirement coverage: run the same offline mapper over baseline tests after generation. Criteria do not enter the baseline generator.
2. Full pipeline additions: keep baseline tests and add conservative tests for criteria without an adequate match, grounded in the criteria wording and supported schema. Never invent undocumented behavior.
3. Labelling: label 30–40 pairs, relabel the same pairs a few days later without consulting first labels, report self-agreement, and record rationale for disagreements. One annotator cannot establish inter-rater reliability.
4. Similarity threshold: choose a documented threshold using a small development subset, freeze it before final evaluation, and disclose if the dataset is too small for a credible holdout.
5. Coverage denominator: all criteria in the versioned criteria.yaml count unless explicitly excluded before evaluation.
6. Passing tests: a requirement is covered only if at least one passing test is tagged to it; report mapped-but-failing tests separately.
7. Mutation denominator: use valid, non-equivalent mutants selected for the run. Setup errors do not count as kills.
8. CI: run tests without external services; write generated artifacts to temporary or ignored directories.
