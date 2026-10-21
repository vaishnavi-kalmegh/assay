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
     another. A whole-request test (field `request`, such as "valid request") sends
     every field with a valid value, so it is eligible for any field. A test that
     names no field and is not whole-request, such as "missing required JSON body"
     (field `body`), says nothing about a specific field, so it is **not** eligible
     for a criterion about `request.name`. A criterion on `response.status` has no
     request field, so this rule does not apply to it;
   - exercises the same kind of bound. A criterion whose constraint is a lower bound
     (`min_length`, `greater_than`, `greater_than_or_equal`) is never matched to a
     test of a maximum, and an upper-bound criterion (`max_length`, `less_than`,
     `less_than_or_equal`) is never matched to a test of a minimum. A test says
     which it exercises in the last part of its name ("just above maximum length
     40"). A name that mentions neither or both is not filtered. Only opposing
     bounds are excluded; this does not stop, say, a missing-required test from
     competing for a bound criterion.
2. **Recognizes explicit status checks.** If the criterion is `response.status
   equals N` and an eligible test asserts that status (it checks `status` and
   expects N), the match is direct evidence and scores 1.0 by rule, with
   `basis: explicit_status` in `traceability.json`. Word overlap is not meaningful
   here, because the criterion's words ("status", "equals", "200") never appear in a
   test named "valid request".
3. **Ranks every other eligible test by TF-IDF cosine similarity** (scikit-learn,
   `TfidfVectorizer`, sublinear term frequency, L2 norm, fitted on all criteria
   and test texts together). The criterion text is its field, a phrase for its
   constraint (for example `greater_than` becomes "exclusive minimum"), its value,
   "valid"/"invalid" from its expected status, and its description. The test text
   is its field, the last part of its name ("just below minimum length 3"), and
   "valid"/"invalid". Number words become digits and `10000.0` becomes `10000`.
   Method, path and status are left out because eligibility already requires them
   to match.
4. **Maps the eligible tests that score at or above the threshold**, at most
   `top-k` per criterion. Scores are rounded to four decimals; ties break on case
   ID.

## Outputs

- `mapping.json`: `{case_id: [requirement IDs]}`, the format `assay run --mapping`
  reads to add `@pytest.mark.req(...)` markers.
- `traceability.json`: per criterion, the mapped tests and **every eligible
  candidate with its score and basis** (`tfidf` or `explicit_status`), or the reason
  it has no mapped test.
- `report.md`: the traceability table and the gap list for reading. A match from
  the explicit status rule is shown as `(1.0000, explicit_status)`, the same
  `basis` value as in `traceability.json`; ordinary matches show the score alone.

No timestamps or machine paths, so repeated runs give identical bytes.

## Gap reasons

| Reason | Meaning |
| --- | --- |
| `no_test_for_operation` | no test exercises the criterion's method and path |
| `no_test_with_expected_status` | the operation has tests, but none expects the criterion's status (for example, no test expects 404) |
| `needs_response_body_check` | the criterion is about response content and the candidate tests assert only the status code |
| `no_test_for_field` | tests exist for the operation and status, but none targets the criterion's field |
| `no_test_for_constraint` | tests exist for the field, but all exercise the opposite bound of the criterion's constraint |
| `below_threshold` | eligible tests exist, but the best scored under the threshold |

## The threshold is provisional

The default threshold, 0.4, is **not validated**. It was chosen after looking at
the score table for this demo's 18 criteria, so any precision or recall measured
on the same criteria will be optimistic. For this demo, the number of criteria with
at least one mapped test is 13 for every threshold from 0.11 to 0.44 (14 at 0.10 and
below, 12 at 0.45). The number of mapped (criterion, test) pairs across that range
depends on `top-k`: with a `--top-k` large enough not to truncate (for example 100)
it falls from 35 to 14, and at the default `--top-k 3` it falls from 33 to 14. At
the default threshold of 0.4 it is 14 either way. So the
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
- **A likely false gap.** REQ-008 (category accepts a supported enum value) is
  reported `below_threshold`, with a best score of about 0.11. The only eligible
  test is a plain "valid" case whose name shares almost no words with the
  criterion. In my reading that test plausibly covers it, so the mapper probably has
  a recall problem there. The labelled evaluation (M5) is what decides. (REQ-001
  used to be reported the same way; it is now mapped by the explicit status rule.)
- **Weak runners-up can remain.** Some tests above the threshold may not verify
  their criterion, because text similarity is only a proxy. The three rules above
  came from specific errors found by comparing the mapper with hand labels (a
  missing-body test accepted for a named field, a maximum-length test accepted for a
  minimum-length criterion, and a status criterion missed). They fix those classes
  of error, not every possible one.
- **Tuned on the development half.** The three errors that motivated those rules
  are all development-half pairs in the committed sheet. Because the rules were
  written after seeing them, development-half precision and recall now flatter the
  mapper. The test half was not used to choose the rules, so it is the better
  estimate, but it is not untouched: the evaluation command reports both halves
  together, so test-half scores were probably visible when the rules were written.
  At 20 pairs it is also small.
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
