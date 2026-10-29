# assay

[![CI](https://github.com/vaishnavi-kalmegh/assay/actions/workflows/ci.yml/badge.svg)](https://github.com/vaishnavi-kalmegh/assay/actions/workflows/ci.yml)

**Offline-first API test generation and requirement traceability.**

assay answers one question about an API: *do the tests we have actually check the requirements we wrote down?* Given an OpenAPI specification and structured acceptance criteria it generates runnable pytest tests, links each criterion to the tests that could verify it, reports the criteria with no real test, and then measures itself: how well the linking matches hand labels, how many requirements have a passing test, and how many deliberately broken versions of the API the tests catch. Those three measurements are reported separately and never merged into one number.

It is deterministic and fully offline. No API key is needed. An optional LLM mapper exists only as a disabled interface stub.

It is a learning-focused portfolio project between QA automation and ML evaluation. The matching uses classical text similarity (TF-IDF), not a language model; the ML-style practice is in how it is measured: hand labels, a development/test split, precision and recall, Cohen's kappa, and mutation testing. Everything below is measured on one small demo API and says nothing about other APIs.

## The problem

Teams usually know how many tests they have, not which requirement each one proves. A test can pass while the rule it was meant to guard is broken, and a requirement can have no test at all without anyone noticing. assay makes both checkable: it links requirements to tests, lists the requirements with no real test and why, and then checks whether the tests notice when the API is deliberately broken.

## Demo

![Terminal demo: assay map, the gap reasons, assay acceptance, assay evaluate mutants, assay compare](docs/demo.gif)

The animation is rendered from the real captured output of these commands; only the typing is synthetic (`python scripts/make_demo_gif.py` regenerates it, needs Pillow). The `10 of 10` on the last line includes one kill that was tuned to its mutant; see Results.

## Example

A requirement, written as structured data (`criteria.yaml`):

```yaml
- id: REQ-004
  method: POST
  path: /products
  field: request.name
  constraint: min_length
  value: 3
  expected_status: 422
  description: Product name shorter than three characters is rejected.
```

`assay map` links it to the baseline test generated from the OpenAPI spec, with its score:

```
| REQ-004 | BASE-BD70624B4DB6 (0.5813) POST /products — body.name — just below minimum length 3 |
```

`assay acceptance` generates a test for it directly from the same fields:

```python
@pytest.mark.req('REQ-004')
def test_req_004(client):
    response = client.post(
        '/products',
        json={'category': 'book', 'name': 'xx', 'price': 9.99},
    )
    assert response.status_code == 422, response.text
```

And when no test can verify a requirement, the report says why instead of staying silent:

```
| REQ-002 | needs_response_body_check | the criterion is about response.body.status, but the 1 candidate test(s) check only status |
```

The baseline asserts status codes only, so it cannot verify a rule about a response body; that is what the mutation results below show.

## Results

`assay compare` runs three suites on the real demo API and on 10 hand-written mutants (copies of the API with one rule deliberately broken):

| Suite | Tests | Criteria with a passing tagged test | Mutants killed |
|---|---|---|---|
| A. Baseline (generated from the OpenAPI spec only) | 43 | 13 of 18 | 6 of 10 |
| B. Criteria-driven (one test per criterion) | 18 | 18 of 18 | 8 of 10 (7 before the `toy` change below) |
| C. Combined (A and B) | 61 | 18 of 18 | 10 of 10 (9 before the `toy` change below) |

Read these numbers with their limits:

- **Coverage is not comparable across suites.** A's tags come from the mapper, so it inherits the mapper's errors. B and C are generated from the criteria being counted, so their coverage is partly circular. It is a traceability measure, not proof of test quality.
- **Mutation score is the more independent check, and it is small.** 10 mutants, hand-written inside this project, not by an independent party, for this one API; some target behaviour the criteria describe. One mutant moves the score by 0.1. No mutant was declared equivalent.
- **What each suite misses.** A never asserts a response body and has no unknown-ID tests, so it misses `get_unknown_product_200`, `order_unknown_product_201` and `health_body_wrong`. B misses boundary mutants A catches. No suite caught `category_accepts_toy` at first (A 6, B 7, C 9 of 10), because no test sent the value `toy`. REQ-009's generated test was then extended to try several unsupported values, including `toy`, which is what made B and C kill it. **That was done after the mutant survived, so the kill is tuned to the mutant, not an independent result.** The independent measurement is the earlier one: B 7 of 10 and C 9 of 10. Treat 10 of 10 as "no known survivors", not as proof the tests are thorough.
- **Mapper quality** was scored against 40 hand-labelled pairs (5 positives, one labeller). At the default threshold 0.4 it found 1 of 1 positives in the development half and 4 of 4 in the test half, with no false positives. That is a count, not a quality claim: the rules were revised after the labelled errors were seen, so the test half is not an independent holdout. The threshold 0.4 is a frozen default, not a tuned value, because the development half has too few positives for a sweep to choose one. Two AI second raters each agreed with the labels on 19 of 20 re-labelled pairs (kappa 0.773 each; Claude was not blind to the mapper, ChatGPT 6 was given a blind packet, as reported by the user); your own self-agreement pass, which measures one person's consistency, is still pending.

The full generated report is committed at `docs/results/comparison.md` (a test fails if it goes stale). Definitions are in `docs/METRICS.md`, the labelling protocol in `docs/LABELLING.md`, and the reasoning behind every design choice, with the questions to expect in an interview, in `docs/DESIGN_DECISIONS.md`.

## How it works

```mermaid
flowchart LR
    spec[OpenAPI spec] --> base[Schema baseline generator]
    base --> bt[Baseline tests]
    crit[criteria.yaml] --> mapper[Offline mapper: rules + TF-IDF]
    bt --> mapper
    mapper --> gaps[Traceability report and gaps]
    mapper --> tags[Requirement tags]
    crit --> acc[Acceptance generator]
    spec --> acc
    acc --> at[Criteria-driven tests]
    tags --> run[pytest runner]
    bt --> run
    at --> run
    run --> api[Demo API]
    run --> mutants[10 API mutants]
    api --> cmp[assay compare]
    mutants --> cmp
    cmp --> out[Coverage and mutation score, kept separate]
```

1. **Baseline.** Tests are derived from the OpenAPI document alone and never read the criteria: valid values, boundaries, wrong types, missing required fields, invalid enum values. Case IDs are stable hashes, so labels stay valid.
2. **Mapper.** Hard rules decide which tests could verify a criterion (same operation, same expected status, checks what the criterion needs, right field, right direction of the constraint); TF-IDF similarity then ranks the eligible ones against a threshold. A criterion with no match is reported with the reason. `response.status equals N` is matched by rule when a test asserts that status.
3. **Acceptance generator.** One explicit test per structured criterion, built from its fields only, never from guessed behaviour.
4. **Evaluation.** Mapping precision and recall against hand labels, requirement coverage, and mutation score, each its own measurement.

## Reproduce from a clean checkout

Requires Python 3.11 or newer. No network access is needed after installing.

    python -m venv .venv
    # Windows: .venv\Scripts\activate
    # macOS/Linux: source .venv/bin/activate
    python -m pip install --upgrade pip
    python -m pip install -e ".[dev]"
    pytest
    ruff check .
    assay compare            # the table above; writes reports/comparison/

Other commands:

    python scripts/export_openapi.py   # regenerate specs/openapi.json
    assay run                # export, run and report the generated baseline tests
    assay acceptance         # generate and run the criteria-driven tests
    assay map                # map criteria.yaml to baseline tests and report gaps
    assay evaluate mutants   # run the baseline against the 10 mutants
    assay evaluate mapping   # score the mapper on the filled label sheet
    assay evaluate relabel   # blank subset of the pairs to label again later
    assay evaluate agreement # compare the two labelling passes

Output is deterministic: running a command twice gives identical files. CI (Ubuntu 24.04, Python 3.11) runs the tests, regenerates the OpenAPI document and checks it did not change, runs `assay acceptance` and `assay compare`, and lints the code and the generated files.

## Status

| Milestone | State |
|---|---|
| M0 Skeleton, M1 Demo API, M2 Offline schema baseline | done |
| M3 Exporter and runner, M4 Requirement mapping | done |
| M5 Evaluation harness | done; a second-rater pass is recorded, the labeller's own self-agreement pass is pending |
| M6 Comparison and docs | done |

## Related tools

assay is a learning project on one demo API, not a replacement for any of these; it asks a different question.

- Tools such as [Schemathesis](https://pypi.org/project/schemathesis/) generate test cases from an OpenAPI schema (property-based) to find crashes and schema violations. assay's baseline is simpler and deterministic, and its focus is whether *requirements* are verified, not finding new bugs.
- Mutation testing tools such as [mutmut](https://pypi.org/project/mutmut/) mutate a program's source code. assay mutates the API's behaviour (10 hand-written variants of the demo server) to see whether the tests notice.
- Test management tools keep a traceability matrix linking requirements to tests, usually maintained by hand. assay proposes the links automatically and measures how accurate its links are.

## Known limitations

- One labeller, 40 labelled pairs, 5 positives; the sample is stratified, not random.
- The mapper's rules were revised after labelled errors were seen; there is no independent holdout. Any claim that the mapper generalises needs a newly drawn, independently labelled sheet scored against a frozen mapper.
- 10 hand-written mutants on one demo API; results do not transfer to other APIs.
- Direct dependencies are pinned, transitive ones are not locked.
- Only a supported OpenAPI subset is handled; unsupported constructs fail with `UnsupportedSchemaError`.

## Documentation

`PLAN.md` (roadmap and decisions), `docs/DESIGN_DECISIONS.md`, `docs/SCHEMA_BASELINE.md`, `docs/RUNNER.md`, `docs/MAPPING.md`, `docs/METRICS.md`, `docs/LABELLING.md`.
