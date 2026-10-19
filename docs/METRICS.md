# Metrics

assay reports three numbers that answer three different questions. They are
never combined into one score, and every ratio is shown with its counts.

| Metric | Question it answers | Measured on |
|---|---|---|
| Mapping precision / recall | Does the mapper link criteria to the right tests? | Hand-labelled pairs (the mapper only) |
| Requirement coverage | Does every criterion have a passing, tagged test? | The generated suite run on the real API |
| Mutation score | Do the tests notice when the API's behaviour changes? | The suite run on hand-written mutants |

Code: `src/assay/metrics.py` (pure formulas) and `src/assay/evaluation.py`
(sampling, label checks, mutant runs). Command: `assay evaluate ...`.

## Undefined is not zero

A ratio with an empty denominator is **undefined**, reported as `undefined` and
stored as `null`. Precision with no predictions is not 0, and recall with no
positive labels is not 1. A report that quietly turns these into numbers can make
a broken mapper look perfect.

## Mapping precision and recall

For a labelled sample, with `predicted` = pairs the mapper kept (above the
threshold, within top-k):

- TP: predicted and labelled 1. FP: predicted and labelled 0.
- FN: labelled 1 but not predicted. TN: labelled 0 and not predicted.
- **precision = TP / (TP + FP)**, undefined if nothing labelled was predicted.
- **recall = TP / (TP + FN)**, undefined if no labelled pair is positive.
- Predictions on pairs that have no label cannot be judged. They are counted in
  `unlabelled_predictions` and left out of both ratios.

Worked example (also a test): predicted A, B, C; labels A yes, B no, D yes, E no.
TP = A, FP = B, FN = D, TN = E, C unlabelled. Precision 1/2, recall 1/2.

These apply to the mapper only, on labelled pairs. They say nothing about the
schema-only baseline, which does not map anything.

### Dev/test split and threshold choice

Each labelled pair is assigned `dev` or `test`, alternating within each sampling
group so both halves see every group. The threshold sweep (0.00 to 1.00 in
steps of 0.05) uses **dev pairs only**. The rule, fixed before any label existed:
highest F1 on dev; on a tie, the higher threshold; thresholds with undefined F1
are skipped. The chosen threshold is then scored on the **test** half, which it
never influenced. The report also shows the configured threshold on both halves
for comparison.

Limits: 40 pairs (20 per half) is small, the sample is stratified rather than
random, and there is one labeller. See `docs/LABELLING.md`.

## Requirement coverage

**coverage = criteria with at least one passing test tagged with the criterion /
all criteria in scope.**

- Only outcome `passed` counts. `failed`, `error` and `not_run` do not.
- A criterion with tagged tests but none passing is listed in
  `mapped_not_passing`, apart from criteria with no tagged test at all
  (`not_mapped`).
- One passing test is enough, even if another tagged test fails.
- Untagged passing tests prove nothing, and tags outside the criteria in scope
  are ignored.
- Undefined when there are no criteria.

Worked example (also a test): criteria R1..R4; R1 and R4 have a passing tagged
test, R2 only a failing one, R3 none. Coverage = 2/4.

**Circularity.** When targeted tests are generated from the same criteria being
measured, coverage partly measures that the tool did what it was told. Treat it
as a traceability measure, not proof the tests are good. Mutation score is the
independent check.

Mapped is not verified: a test that maps to a criterion is a candidate, and only
a *passing* tagged test counts.

## Mutation score

**mutation score = valid, non-equivalent mutants killed / valid, non-equivalent
mutants executed.**

- A mutant is **killed** when at least one test `failed` on it. A setup `error`
  (for example a broken fixture) does not count: it is not evidence that a test
  noticed the change.
- The **control run** on the unmutated API must pass completely, otherwise a
  "kill" could just be a broken test and the whole evaluation is rejected.
- An **equivalent** mutant changes no observable behaviour, so no test can kill
  it and it must not count against the suite. `mutation_score` accepts a set of
  equivalent mutants to exclude; none of the current ten needs it, because each
  has a probe request on which it answers differently from the real API
  (`tests/test_evaluation.py`).
- Undefined when no valid mutant ran.

Worked example (also a test): mutants a, b, c, d with b surviving gives 3/4; if b
is declared equivalent the score is 3/3.

### The mutants

Ten hand-written mutants in `src/assay/demo_api.py` (`MUTANTS`). Each is one
deliberate change. They are **off by default**: `create_app()` is the real API,
the OpenAPI spec is exported from the real API, and the runner clears
`ASSAY_MUTANT` unless `assay evaluate mutants` sets it.

| Mutant | Change |
|---|---|
| `name_min_length_2` | product name minimum length 3 becomes 2 |
| `name_max_length_41` | product name maximum length 40 becomes 41 |
| `price_allows_zero` | price `> 0` becomes `>= 0` |
| `price_max_exclusive` | price `<= 10000` becomes `< 10000` |
| `category_accepts_toy` | category also accepts `toy` |
| `stock_allows_negative_one` | stock `>= 0` becomes `>= -1` |
| `order_quantity_max_11` | order quantity maximum 10 becomes 11 |
| `get_unknown_product_200` | GET of an unknown product returns 200, not 404 |
| `order_unknown_product_201` | an order for an unknown product is accepted |
| `health_body_wrong` | `/health` body status `up` instead of `ok` |

Limits, stated plainly: the mutants were chosen by hand by the same author who
wrote the baseline generator and knew what it checks; ten is a small sample; they
are not exhaustive and not a random draw from all possible bugs. Six change a
validation bound, one changes an enum, and three change behaviour (two 404 rules
and one response body). The score describes this set only.

### First measurement (baseline suite only)

On the 43 schema-only baseline tests, with the control run passing 43 of 43, the
suite kills **6 of 10** mutants (0.600). The four survivors are
`category_accepts_toy`, `get_unknown_product_200`, `order_unknown_product_201`
and `health_body_wrong`.

What the survivors have in common, checked against the generator: the baseline
asserts the status code only, never a response body; it has no test that fetches
or orders an unknown product; and its invalid-enum test sends
`__invalid_enum_value__`, not `toy`. This is the gap the criteria-driven
generator is meant to close. It is a measurement of the baseline, not a
comparison: the full pipeline does not exist yet (`PLAN.md`, open decision 9).
The result is pinned in `tests/test_evaluation.py` as a characterisation of
today's tests.

## Commands

```
assay evaluate sample              # write the blank sheet (refuses to overwrite)
assay evaluate mapping             # score the mapper on a filled-in sheet
assay evaluate mutants             # control run, then every mutant
assay evaluate mutants --mutant price_allows_zero   # a chosen subset
```

Reports go to `reports/evaluation/` and `reports/mutation/` (ignored by Git).
Output is sorted and has no timestamps, so repeated runs are byte-identical.
