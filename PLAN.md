# assay — Project Plan

## Goal

Build a small, explainable, offline-first tool that accepts an OpenAPI JSON document and acceptance criteria with stable IDs (REQ-001, etc.), generates runnable pytest tests, runs them against a local FastAPI demo service, and reports requirement traceability and mutation-testing results. No API key is ever required. Keep modules small, choices conventional, and generated output deterministic.

## Architecture

1. Read and normalize OpenAPI paths, operations, parameters, request bodies, schemas, and responses into Pydantic models.
2. Generate a schema-only baseline: valid examples, boundaries, invalid types, missing required fields, and invalid enum values. This stage must not read acceptance criteria.
3. Map tests to criteria offline using an inspectable TF-IDF/cosine-similarity or keyword baseline.
4. Identify criteria not adequately represented and conservatively plan extra tests using only evidence from criteria and the supported API schema.
5. Export ordinary pytest files, tagging mapped tests with a pytest requirement marker such as req("REQ-xxx").
6. Execute tests against the in-process/local demo API and write structured JSON results and a traceability matrix.
7. Evaluate mapping quality on hand-labelled pairs, requirement coverage, and mutation score separately.
8. Expose an optional LLM adapter interface with a disabled stub only. No real provider integration or network calls in the MVP.

## Folder layout

- src/assay/ — package and pipeline modules.
- tests/ — unit and integration tests.
- demo_api/ — local FastAPI app.
- specs/openapi.json — reproducible exported API spec.
- criteria.yaml — structured acceptance criteria with unique IDs.
- data/labelled_pairs.csv — manually labelled pairs (M5).
- docs/METRICS.md and docs/LABELLING.md — metric definitions and annotation protocol.
- generated_tests/ and reports/ — runtime artifacts, ignored by Git.
- .github/workflows/ci.yml — CI pinned to Ubuntu 24.04.
- pyproject.toml — package config, pinned dependencies, pytest, Ruff.

## Data flow

OpenAPI spec -> Pydantic parser -> schema-only baseline cases -> offline requirement mapper <- criteria.yaml -> gap report -> conservative targeted cases -> pytest exporter/runner -> JSON results -> metrics and traceability report.

Criteria must never enter the baseline generator. Apply the same mapper after generation when evaluating baseline requirement coverage, so comparison remains fair.

## Dependencies and decisions

Use Python 3.11+, FastAPI for the demo API only, Pydantic for typed models, pytest for execution and requirement markers, HTTPX for in-process API tests, PyYAML for criteria, scikit-learn for TF-IDF similarity, and Ruff for linting. Pin every dependency exactly with == when it is introduced. M0 pinned only pytest and Ruff; M1 adds exact FastAPI, HTTPX, and PyYAML pins; M4 adds exact scikit-learn. Prefer standard libraries and readable functions over extra abstractions. Sort outputs and fix any random seeds to preserve determinism.

## Milestones

Every milestone needs a deliverable, run command, automated tests, explicit pass/fail criteria, and one milestone commit. Fix any failure before adding features. Stop after each milestone and wait for review.

### M0 — Skeleton (approved)
Deliverable: pyproject, pinned development dependencies, placeholder test, lint configuration, CI, README stub, and plan.
Run: python -m pip install -e ".[dev]" && pytest && ruff check .
Tests: deterministic placeholder smoke test.
Pass: tests and lint exit 0; CI uses Python 3.11 and Ubuntu 24.04; dependency pins are exact; no secret or network service is needed for tests.
Fail: any command fails, dependency is unpinned, or CI requires a secret.

### M1 — Demo API (approved)
Deliverable: five FastAPI endpoints with required-field, min/max, enum, and status-code rules; exported specs/openapi.json; criteria.yaml with 18 structured IDs; API and criteria tests.
Run: pytest. Regenerate the spec with python scripts/export_openapi.py; repeated exports must be identical.
Tests: valid input, missing fields, boundaries, invalid types/enums, expected statuses, unique and structurally valid criteria, repeatable OpenAPI output.
Pass: all tests pass, spec export is reproducible, IDs are unique and criteria testable. Fail: unstable export or unclear contract.

### M2 — Offline schema baseline (approved)
Deliverable: src/assay/baseline.py with Pydantic case models, deterministic IDs, normalized output, and a documented supported OpenAPI subset. Generates valid, boundary (bound and just outside), invalid-type, missing-required, and invalid-enum cases without reading criteria.
Run: pytest tests/test_baseline.py, then pytest.
Tests: path parameters, JSON object bodies, required fields, string and numeric bounds, enums, unsupported-feature errors, deterministic normalized output, and a guard proving criteria.yaml is never opened.
Pass: repeated runs produce identical normalized cases; unsupported constructs raise UnsupportedSchemaError; CI is green. Fail: criteria leakage, unstable IDs/order, or silent schema misrepresentation.

### M3 — Exporter and runner (approved)
Deliverable: src/assay/runner.py and the `assay` CLI (`export`, `run`); ordinary pytest files, one per operation, plus conftest.py, pytest.ini and manifest.json; optional requirement markers from a mapping file; JSON results; `create_app()` factory in the demo API so each test has its own state. Documented in docs/RUNNER.md.
Run: pytest, then `assay run`.
Tests: expected-status derivation, deterministic export, stale-file and overwrite protection, marker correctness, mapping validation, result schema, run-to-run identical results, reverse-order execution, a wrong API response reported as a failure, criteria.yaml never opened, CLI exit codes.
Pass: generated tests run locally against the demo API and each result traces to a baseline case ID; export and results are byte-identical across runs; specs/openapi.json is unchanged by the factory refactor. Fail: tests cannot collect, execution needs hosted services, or results cannot be traced to cases.
Finding: the first end-to-end run exposed an M2 defect. FastAPI writes integer bounds as floats (1 as 1.0), so the baseline generated float values for integer fields. Integral float bounds on integer fields are now converted to ints and fractional ones are rejected; affected case IDs changed. Case IDs are frozen from M5 labelling onward.

### M4 — Requirement mapping (approved)
Deliverable: src/assay/mapping.py and `assay map` (the CLI now lives in src/assay/cli.py); eligibility filter (same operation, expected status, checked content, field) plus TF-IDF ranking and a threshold; mapping.json (runner format), traceability.json with every eligible candidate and score, report.md with the gap list and reasons; disabled LLM mapper stub. Documented in docs/MAPPING.md.
Run: pytest, then `assay map`, then `assay run --mapping reports/mapping/mapping.json`.
Tests: eligibility and each gap reason, ranking and tie-breaks, threshold boundary and top-k, invalid settings and duplicate IDs, positive and negative examples on the demo data, determinism, mapping accepted by the runner and tags tests, stub disabled with no network, CLI behavior. The tests were checked by deliberately breaking six rules of the mapper and confirming a test fails each time.
Pass: mapping is identical across runs; uncovered criteria are listed with a reason; no provider calls. Fail: missing gaps, nondeterminism, or provider calls.
Finding: the first version mapped REQ-010 ("missing product name") to the missing-price and missing-category tests too, because "missing required" dominated the text similarity. A test targeting one field can no longer verify a criterion about another.
Limitation: the default threshold (0.4) is provisional and was chosen after seeing the demo score table; M5 re-chooses it on labelled data and freezes it.

### M5 — Evaluation harness (harness implemented, 40 pairs labelled, threshold frozen at 0.4; second pass pending)
Deliverable: src/assay/metrics.py (pure formulas), src/assay/evaluation.py and `assay evaluate sample|mapping|relabel|agreement|mutants`; data/labelled_pairs.csv (40 pairs, 20 dev / 20 test, score-blind; labelled, 5 positive) with data/labelled_pairs.meta.json; ten switchable demo API mutants (`create_app(mutant=...)`, off by default); docs/LABELLING.md and docs/METRICS.md.
Run: pytest, then `assay evaluate mutants`; after labelling, `assay evaluate mapping`; after a second pass days later, `assay evaluate relabel` then `assay evaluate agreement`.
Tests: metric formulas against hand-worked examples, empty-set behaviour (undefined, not 0 or 1), sampling determinism and strata, label-sheet validation, dev-only threshold sweep, each mutant changes behaviour while the real API does not, stray ASSAY_MUTANT cleared, control run must pass, known mutant kills, byte-identical output, case-ID freeze guard. Each group of tests was checked by deliberately breaking the rules it covers and confirming a test fails (five rules for metrics and mutants, eight for the report marker, direction guard and sheet pins, nine for the label reader, relabel sheet and agreement report).
Pass: label protocol is documented, mutants default off, calculations match hand-worked examples, test IDs frozen. Fail: inconsistent labels or irreproducible metrics.
Finding: the baseline suite kills 6 of 10 mutants (control 43 of 43 passing). It misses the enum value `toy`, both 404 rules and the `/health` body value, because it asserts status codes only and has no unknown-ID tests. A measurement of the baseline, not a comparison.
Finding (mapper fixes): comparing the mapper with hand labels exposed three error classes, now fixed in src/assay/mapping.py: a body-only test ("missing required JSON body") was accepted for a criterion about one named field; a maximum-length test was accepted for a minimum-length criterion; and `response.status equals N` was missed because its words never overlap a test named "valid request". The mapper now separates whole-request tests from body-only tests, never matches opposing bounds, and maps an explicit status check by rule (score 1.0, `basis: explicit_status`, marked in report.md). All three pairs were development-half rows, so development-half scores now flatter the mapper; the test half is the better estimate but its scores were probably visible when the rules were written. The sheet is frozen: a test pins every pair with its dev/test assignment and every data column.
Result so far: at threshold 0.4 dev 1/1 found, test 4/4 found, no false positives (counts, not a quality claim: 5 positives, one labeller, rules written after seeing errors). Threshold: the pre-set rule picked 1.0 because dev has one positive, which is degenerate (on test: recall 0, precision undefined). A minimum of 5 dev positives was added after the labels existed (disclosed in docs/METRICS.md); below it no threshold is tuned and 0.4 is kept as a frozen default, not a measured optimum. Pending: the second pass, which is the person's, a few days after the first. The second pass (`relabel`, then `agreement`) is a few days later.

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

**Evaluation caveat — partial circularity:** full-pipeline requirement coverage is partly circular because targeted tests are generated from the acceptance criteria being measured. Treat coverage as a traceability measure, not independent proof of test quality. Mutation score is the independent check on whether tests detect behavior changes.

**Freeze before labelling:** freeze the generator implementation/configuration, generated test output, and test IDs before creating or finalizing the M5 labelled pairs. Do not revise generated test IDs or output after labels are collected without versioning the change and repeating the evaluation.

## Risks

- OpenAPI variability: support a small subset first and explicitly reject unsupported constructs.
- False matches: expose scores/thresholds and preserve unmapped criteria.
- Misleading coverage: count passing tagged tests; report mapped-but-failing tests separately.
- Circular coverage: disclose criteria-driven generation; use mutation score as the independent check.
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
9. Targeted-test generator (unscheduled): the "full pipeline" in open decision 2 adds tests for criteria the baseline does not cover, but no milestone builds that generator, and M6 compares baseline against it. The criteria the baseline cannot cover today are REQ-002, REQ-011 (response body content) and REQ-012, REQ-018 (404 for an unknown ID). Proposed default: add a milestone between M5 and M6 that generates tests only from structured criteria fields, never from guessed behavior. Needs a decision before M6.
