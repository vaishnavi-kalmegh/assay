# Exporter and runner (M3)

The runner turns the schema-only baseline cases into ordinary pytest files, runs
them against the local demo API, and writes a sorted JSON results file. It never
reads `criteria.yaml`.

## Commands

    assay export --spec specs/openapi.json --output generated_tests
    assay run --spec specs/openapi.json --output generated_tests \
        --results reports/baseline-results.json
    # optional: add requirement markers from a mapping file
    assay run --mapping mapping.json

`export` writes the files only. `run` exports, runs the generated suite with
pytest, and writes the results. Exit code: 0 all passed, 1 some test did not
pass, 2 an error (bad spec, bad mapping, unusable output directory).

## What is generated

One `test_<method>_<path>.py` file per operation, plus `conftest.py`,
`pytest.ini` and `manifest.json` (every case, its expected status and the file and
function that test it). Each test is a plain function with a stable name derived
from the baseline case ID. The files contain no timestamps or machine paths, so
exporting twice gives identical bytes.

The output directory is marked with `.assay-generated`. Re-exporting into it
replaces the generated files. Exporting into a non-empty directory assay did not
create is refused. `generated_tests/` and `reports/` are ignored by Git.

## Expected status

- A valid case expects the lowest documented 2xx response of its operation.
- An invalid case expects 422, the request-validation status.

Cases that only the application can judge (for example an unknown product ID
returning 404) are not schema-derived and are not baseline cases.

## Test isolation and prerequisites

Every test builds a brand-new, empty demo API with `create_app(seeded=False)`, so
no test can see another's data and execution order cannot change a result.

Some operations need an existing product (`GET /products/{product_id}`, the stock
update, orders). The baseline's placeholder ID for such a field is replaced by the
`product_id` fixture, which creates a product with `POST /products` and returns the
ID the API assigned. Nothing assumes a seeded product or a particular ID. The
fields treated this way are listed in `REFERENCE_FIELDS` in `src/assay/runner.py`.

## Requirement markers

Markers appear only when a mapping file is supplied: a JSON or YAML object of
`{case_id: ["REQ-001", ...]}`. Unknown case IDs and malformed requirement IDs are
rejected. Producing the mapping is the job of the requirement mapper (M4); the
runner only applies it, as `@pytest.mark.req("REQ-001")`.

## Results file

`{"summary": {...counts...}, "results": [...]}`. Each result has the case ID,
name, kind, endpoint, field, `expected_valid`, `expected_status`, `actual_status`,
`outcome` (`passed`, `failed`, `error`, `skipped` or `not_run`), the mapped
requirements, and the test that ran. The actual status is recorded by each test
through pytest's `record_property` and read back from the JUnit XML report.

## Limitations

- The generated tests target the bundled demo API (`assay.demo_api`); pointing the
  runner at another application is not supported in the MVP.
- Only status codes are checked, not response bodies. This is deliberate for the
  schema baseline; whether the suite catches real defects is measured by the
  mutation score in M5.
- A passing run shows the baseline agrees with the demo API, not that the API is
  correct.
