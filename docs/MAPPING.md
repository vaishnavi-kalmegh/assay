# Requirement mapping (M4)

The mapper decides which generated tests plausibly verify each acceptance
criterion, builds a traceability matrix, and reports the criteria that no test
covers. It runs offline and is deterministic. It does not run any tests.

## Commands

    assay map --spec specs/openapi.json --criteria criteria.yaml \
        --output reports/mapping [--threshold 0.4] [--top-k 3]
    # tag the generated tests with the result and run them
    assay run --mapping reports/mapping/mapping.json

Exit code 0 on success, 2 on an error (bad criteria file, bad settings, unusable
spec, or `--mapper llm`, which is disabled).

## Method

For each criterion the mapper takes the tests generated from the OpenAPI spec and:

1. **Filters to eligible tests.** A test can only verify a criterion if it
   - exercises the same operation (method and path);
   - expects the same status code as the criterion;
   - checks what the criterion is about: a criterion on `response.body.*` needs a
     test that checks the response body, and the baseline tests assert only the
     status code;
   - targets the same field. A test about one field cannot verify a criterion about
     another. Whole-request tests ("valid input", "valid request") are eligible for
     any field.
2. **Ranks eligible tests by TF-IDF cosine similarity** (scikit-learn,
   `TfidfVectorizer`, sublinear term frequency, L2 norm, fitted on all criteria
   and test texts together). The criterion text is its field, a phrase for its
   constraint (for example `greater_than` becomes "exclusive minimum"), its value,
   "valid"/"invalid" from its expected status, and its description. The test text
   is its field, the last part of its name ("just below minimum length 3"), and
   "valid"/"invalid". Number words become digits and `10000.0` becomes `10000`.
   Method, path and status are left out because eligibility already requires them
   to match.
3. **Maps the eligible tests that score at or above the threshold**, at most
   `top-k` per criterion. Scores are rounded to four decimals; ties break on case
   ID.

## Outputs

- `mapping.json`: `{case_id: [requirement IDs]}`, the format `assay run --mapping`
  reads to add `@pytest.mark.req(...)` markers.
- `traceability.json`: per criterion, the mapped tests and **every eligible
  candidate with its score**, or the reason it has no mapped test.
- `report.md`: the traceability table and the gap list for reading.

No timestamps or machine paths, so repeated runs give identical bytes.

## Gap reasons

| Reason | Meaning |
| --- | --- |
| `no_test_for_operation` | no test exercises the criterion's method and path |
| `no_test_with_expected_status` | the operation has tests, but none expects the criterion's status (for example, no test expects 404) |
| `needs_response_body_check` | the criterion is about response content and the candidate tests assert only the status code |
| `no_test_for_field` | tests exist for the operation and status, but none targets the criterion's field |
| `below_threshold` | eligible tests exist, but the best scored under the threshold |

## The threshold is provisional

The default threshold, 0.4, is **not validated**. It was chosen after looking at
the score table for this demo's 18 criteria, so any precision or recall measured
on the same criteria will be optimistic. For this demo, the number of criteria with
at least one mapped test is 12 for every threshold from 0.11 to 0.43, and the number
of mapped (criterion, test) pairs falls from 36 to 14 across that range. So the
threshold changes how many extra tests get tagged, not how many criteria count as
covered, in that range. That is a property of this data, not a general result.

Milestone 5 picks the threshold properly:

- label criterion-test pairs yourself **before looking at the mapper's scores**;
  draw them from `candidates` in `traceability.json`, including low-scoring ones so
  recall can be measured;
- choose the threshold on a development subset, freeze it, and report on the rest;
- say that the dataset is too small for a credible holdout if it is.

## What a mapping does and does not mean

- **Mapped is not verified.** It means the test is eligible and textually similar.
  A mapped test may not check the property the criterion states. Whether a
  requirement counts as covered is decided later: at least one passing, tagged test.
- **Likely false gaps.** REQ-001 and REQ-008 are reported `below_threshold`, with
  best scores of about 0.10. In both cases the only eligible test is a plain
  "valid" case, whose name shares almost no words with the criterion. In my reading
  those tests plausibly do cover the criteria, so the mapper probably has a recall
  problem there. The labelled evaluation (M5) is what decides.
- **Weak runners-up.** Some tests above the threshold probably do not verify their
  criterion. For example, "name maximum length 40" scores 0.47 for REQ-003 (name
  accepted at the minimum length) because both are about the name field.
- **Real gaps.** REQ-002 and REQ-011 (response body content) and REQ-012 and
  REQ-018 (404 for an unknown ID) are covered by no baseline test: the baseline
  asserts only status codes and generates only schema-derived inputs.
- **Hand-built vocabulary.** The number-word list and the constraint-to-words table
  were written for this API's criteria and test names. How well they carry over to
  other specs is untested.

## The optional LLM mapper

`DisabledLLMMapper` marks where a pluggable mapper would go. It raises
`LLMDisabledError` when used, makes no network calls, and imports no provider.
`assay map --mapper llm` exits with an error. Any real mapper would implement the
same `map(criteria, tests)` interface.
