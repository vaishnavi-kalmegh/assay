# Design decisions and their limits

For each choice: what it is, why, what was rejected, and where it falls short. The
limits are part of each decision and are stated with it.

## The one-paragraph version

assay checks whether the tests an API has actually verify the requirements written
for it. It generates tests from an OpenAPI spec, links each requirement to the tests
that could verify it, reports requirements with no real test, and then measures
itself three separate ways: how well the linking matches hand labels, how many
requirements have a passing test, and how many deliberately broken copies of the API
the tests catch. Everything is deterministic and runs offline.

## Decisions

### 1. Offline and deterministic, no LLM
- **What:** no network, no API key, no randomness; output files are byte-identical
  on every run. An LLM mapper exists only as a disabled stub that never runs.
- **Why:** a number you cannot reproduce is not a result. Deterministic output also
  makes tests exact and lets labels stay valid.
- **Rejected:** an LLM mapper. It would be non-deterministic and impossible to
  evaluate fairly with 40 labelled pairs.
- **Limit:** the mapper only understands wording through TF-IDF and rules, so it
  cannot match requirements and tests that mean the same thing in different words.

### 2. The baseline suite reads only the OpenAPI spec
- **What:** valid values, boundaries, wrong types, missing required fields, invalid
  enum values, all derived from the schema. It never reads the acceptance criteria.
- **Why:** so the baseline is an independent suite to compare against, not one
  already shaped by the requirements.
- **Limit:** it asserts the status code only, never a response body, and has no
  unknown-ID tests. It kills 6 of the 10 mutants and misses four: the two unknown-ID
  rules and the `/health` body value (all caught by the criteria-driven suite), plus
  the `toy` category (see decision 12).

### 3. Stable case IDs
- **What:** each test gets `BASE-` plus the first 12 hex digits of a SHA-256 of its
  canonical content. A fingerprint of all IDs is stored with the label sheet.
- **Why:** labels are tied to specific tests. If the generator changes, the old
  labels would silently describe different tests. The fingerprint turns that into
  an error: version the change and relabel.

### 4. The mapper: hard rules first, TF-IDF second
- **What:** a test is only eligible for a requirement if it passes filters in order:
  same operation, same expected status, checks what the requirement needs (a
  response-body requirement needs a body check), the right field, and the right
  direction of the constraint. Eligible tests are then ranked by TF-IDF cosine
  similarity and kept if the score is at least the threshold (0.4), up to the top 3.
- **Why:** text similarity alone cannot tell "minimum length" from "maximum length",
  or one field from another on the same endpoint. Rules remove the pairs that are
  wrong for structural reasons; similarity only ranks what is left. The filters also
  give a precise reason for every unmapped requirement.
- **TF-IDF details:** sublinear term frequency, L2 norm, smoothed IDF, and a custom
  tokenizer that lowercases and turns number words into digits.
- **Rejected:** similarity alone, which cannot separate opposite bounds or different
  fields. Even the first version with eligibility rules made three such errors (see
  the last question below) until the field-scope and bound-direction rules were added.
- **Limit:** the rules were written for this demo API's wording and constraints.

### 5. Explicit-status rule
- **What:** when a requirement says `response.status equals N` and a test asserts
  status N, they are linked by rule with score 1.0 and marked `explicit_status`.
- **Why:** their words barely overlap ("health endpoint returns a successful status"
  vs a test called "valid request"), so similarity cannot find a link that is
  logically certain. The marker keeps it visible in the report.

### 6. Mapped is not verified
- **What:** a link means "this test is a candidate". Only a passing test tagged to the
  requirement counts toward coverage, and mapped-but-failing is reported separately.
- **Why:** a test that maps but fails, or passes without checking the rule, proves
  nothing. Mutation testing exists precisely to check that.

### 7. Three measurements, never merged
- **Mapping quality** (precision, recall) against hand labels.
- **Requirement coverage**: the share of requirements with a passing tagged test.
- **Mutation score**: seeded faults detected over faults executed. The "mutants"
  are ten hand-written broken copies of the API, not output of automatic mutation
  operators, so this is a seeded-fault score.
- **Why separate:** they answer different questions and have different weaknesses.
  Coverage can be inflated by circularity (tests generated from the requirements being
  counted). Mutation score is less circular than coverage, but it is not independent (see
  decision 8). Averaging them would hide
  that.
- An empty denominator is reported as undefined, not 0 or 1.

### 8. Mutation testing details
- **What:** 10 hand-written mutants (copies of the API with one rule broken), off by
  default. A mutant is killed only if a test fails. A setup error never counts. The
  control run on the real API must pass completely or the whole run is rejected.
- **Why:** a "kill" from a broken fixture is not evidence. The control run rules that
  out.
- **Limit:** 10 mutants written inside this project, not by an independent party, on
  one API. No mutant was declared equivalent. One mutant moves the score by 0.1.

### 9. How the mapper was evaluated
- **Sample:** 40 pairs in three groups chosen by structure, never by score: 20 pairs
  the rules allowed, 12 on the same endpoint the rules ruled out, 8 on other
  endpoints. This is deliberate: a random sample would be almost all obvious
  negatives. Dev and test alternate within each group.
- **Labelling protocol:** "if the API had broken the rule, would this test have
  failed?" Labelled blind, before looking at any mapper output.
- **Threshold:** the rule fixed in advance was "highest F1 on the dev half, ties go to
  the higher threshold". With only one dev positive that rule picked 1.0, which
  scored recall 0 on test. A minimum of 5 dev positives was added after seeing this
  (disclosed in `docs/METRICS.md`), so no threshold is tuned and 0.4 is kept as a
  frozen default, not a measured optimum.
- **Frozen sheet:** the loader refuses a sheet in which a pair moved between halves or
  a data column was edited. Only `label` and `notes` may change.

### 10. The honest reading of the mapper result
- At threshold 0.4 the mapper found 1 of 1 positives on the dev half and 4 of 4 on the
  test half, with no false positives.
- **Do not call that good performance.** There are 5 positives, one labeller, and the
  rules were revised after the labelled errors were seen, so the test half is not an
  independent holdout. It shows the mapper is consistent with these labels, not that
  it generalises. A real claim needs a newly drawn, independently labelled sheet
  scored against a frozen mapper.

### 11. Second raters
- Two AI models labelled the same 20 pairs as a second opinion: each agreed with the
  first labels on 19 of 20. One (Claude) was not blind to the mapper; the other
  (ChatGPT, model name as stated by the labeller and unverified) was given a blind
  packet, as stated. Kappa is not reported: at 20 pairs with about 5 positives it is
  not meaningful. The first labeller's own self-agreement pass is still pending. AI raters are not independent humans and do not validate the
  labels.

### 12. Three suites in the final comparison
- **A** baseline (43 tests), **B** criteria-driven (18 tests, one per requirement,
  built from the requirement's fields only), **C** both together.
- C is the union of A and B. Every test builds its own fresh app, so the union equals
  one combined run; this was checked by running both suites in one session on the
  control and all 10 mutants.
- Result: A 13/18 criteria and 6/10 faults detected; B 7/10; C 9/10. Coverage for B
  and C (18/18) is true by construction and not an achievement. With the tuned kill
  counted, B is 8/10 and C is 10/10.
- **The tuned kill:** `category_accepts_toy` survived every suite until REQ-009's test
  was extended to try several unsupported values, including `toy`. That was done after
  the mutant survived, so the kill is tuned to the mutant. The headline figures are
  B 7/10 and C 9/10; the tuned figures are shown only as a footnote.

## Questions a reviewer might ask

**"Is 4 of 4 on the test half good?"** It is a count on a tiny, stratified sample that
the rules were tuned against. I would not call it a quality result.

**"Why not use an LLM to map?"** I could not evaluate it fairly with 40 labels and it
would not be reproducible. The interface is there, disabled.

**"Why is coverage 18/18 for the criteria-driven suite?"** Because those tests were
generated from the same requirements, so it is true by construction and says nothing about
test quality. Mutation score is less circular but not independent: the same author
wrote the faults, criteria and tests.

**"What would you do next?"** Draw and independently label a new holdout sheet, add
mutants written by someone else, run the self-agreement pass, and try a second API so
the results are not about one demo service.

**"What did you get wrong?"** The first mapper accepted a body-only test for a
single-field requirement, accepted a maximum-length test for a minimum-length
requirement, and missed an explicit status check. Comparing against hand labels is how
I found them. After fixing them I also had to concede that the dev-half results flatter
the mapper.
