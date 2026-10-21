# Labelling protocol

`data/labelled_pairs.csv` holds 40 (criterion, test) pairs. A person decides, for
each one, whether the test verifies the criterion. Those decisions are the ground
truth the mapper is scored against, so they must not be influenced by the mapper.

## Why a person, and why blind

If the mapper's author labels pairs after seeing what the mapper chose, the
labels drift towards the mapper and precision and recall look better than they
are. So:

- **Label before looking at any mapper output.** Do not run `assay map`, open
  `reports/`, `traceability.json` or `docs/MAPPING.md`'s score tables, or read
  `src/assay/mapping.py` until every row is labelled.
- The sheet deliberately shows **no score, rank, eligibility or sampling
  stratum**. Judge each row from the columns shown, nothing else.
- Rows are in shuffled order, so neighbouring rows tell you nothing.

## The question for every row

> If this test passed, but the API had **broken the rule in the criterion**,
> would this test have failed?

- **Yes → `1`.** The test is real evidence for the criterion.
- **No → `0`.** It would pass either way, so it proves nothing about the
  criterion.

Work through these checks; any "no" means `0`.

1. **Same operation.** Same HTTP method and path.
2. **Same expected outcome.** The test expects the status the criterion states.
3. **Same field and rule.** The test exercises the field and constraint the
   criterion is about. A test for a different field does not verify it, even if
   it uses the same endpoint and status.
4. **Checks what the criterion needs.** The `test_checks` column says what the
   test asserts. Every generated baseline test asserts the **status code only**.
   A criterion about a response body value cannot be verified by a status-only
   test, so label it `0`.

A test may verify more than one criterion; label each pair on its own.

## Illustrative examples

These use an invented API, not the demo API, and are not rows in the sheet.

| Criterion | Test | Label | Why |
|---|---|---|---|
| `POST /widgets`: "weight must be greater than 0", rejected with 422 | just outside the minimum, weight 0 | 1 | Breaking the rule (accepting 0) makes this test fail |
| same | `weight` valid mid-range value → 201 | 0 | Expects a different status and passes whether or not 0 is rejected |
| same | `label` missing required field → 422 | 0 | Right status, wrong field |
| `GET /widgets/{id}`: response body `id` equals the requested id | status-only test of a valid fetch → 200 | 0 | The id in the body is never checked |

## Practical rules

- Fill in the `label` column with `1` or `0`. Nothing else is accepted, and a
  blank cell is an error. Optionally add a note.
- If you are torn, give your best judgement and write `unsure` in `notes`.
  Do not skip rows. The count of unsure rows is reported as a limitation.
- Do not edit any other column, add rows or reorder. The evaluation checks IDs
  and refuses a sheet whose rows no longer match.
- Save as **CSV UTF-8**. Test names contain an em dash. A byte-order mark, which
  spreadsheet programs add when saving as "CSV UTF-8", is accepted. Plain "CSV" on
  Windows writes the dash in a legacy encoding; the commands reject that with a
  message telling you to save as CSV UTF-8.
- Expect roughly 30 to 40 minutes.

## After labelling

1. Run `assay evaluate mapping`. It reports precision and recall on the
   **test** half using a threshold chosen on the **dev** half only, as well as
   the configured threshold on both halves. See `docs/METRICS.md`.
2. The threshold rule was fixed before any label existed: highest F1 on the dev
   half, and on a tie the higher threshold. It is not adjusted after seeing the
   test half.
   One amendment was made after labelling: the sweep only runs when the dev half
   has at least 5 positives (see `docs/METRICS.md`). The committed sheet has 1 dev
   positive, so no threshold is chosen from it.
3. Because nothing was tuned, `0.4` stays the configured default and is frozen as
   such: a default, not a value measured to be best. It changes only if a larger
   labelled sheet gives the dev half enough positives for the sweep to run.

## Second pass: self-agreement

With one labeller there is no second opinion, so the closest check is you against
yourself, a few days later. Do this only after the first pass is finished and has
been saved.

1. `assay evaluate relabel` writes `data/relabelled_pairs.csv`: a blank copy of 20
   of the 40 pairs (change with `--size`), in a different order. The pairs are
   chosen by a seeded hash of the pair ID alone, so the choice cannot depend on
   your first labels, and the labels and notes are cleared. It refuses to
   overwrite an existing file (`--force`) and never writes over the first sheet.
2. Wait a few days, then label that sheet without looking at your first labels,
   following the same rules as above.
3. `assay evaluate agreement` compares the two passes on the pairs in the second
   sheet and writes `reports/evaluation/agreement.json` and `agreement.md`:
   observed agreement, Cohen's kappa, each disagreement, and how many rows you
   marked `unsure` each time. Look at the disagreements: they show which kinds of
   pair you find ambiguous.

The first labels stay the ground truth. The second pass measures how stable they
are; it does not replace them.

## What the labels can and cannot show

- **One labeller.** There is no second opinion. A single person's reading of
  "verifies" is what the numbers measure. Until you have done the second pass
  above, label noise is unknown, and even after it the result is one person's
  consistency with themselves, not agreement between people.
- **Stratified, not random.** The sample is drawn from three groups:
  pairs the mapper's rules allowed (20), pairs on the same endpoint the rules
  ruled out (12), and pairs on other endpoints (8). It over-represents the
  hard cases on purpose. Precision and recall describe **this sample**, not all
  774 possible pairs.
- **Small.** With 40 pairs split into two halves of 20, one changed label moves
  a score by several points. Report counts next to every ratio.
- **Frozen tests.** Labels are tied to the current generated test IDs. A test
  that checks them (`tests/test_evaluation.py::test_case_ids_are_frozen`) fails
  if the IDs change. If the generator changes, version the change and relabel.
- **Regenerating the sheet.** `assay evaluate sample` refuses to overwrite an
  existing sheet. Do not use `--force` after you have started labelling.
- **The sheet is frozen once drawn.** The sampler's groups depend on the mapper's
  eligibility rules, so a fresh draw after a mapper change picks somewhat
  different pairs (after the M5 mapper fixes, 6 of the 40 pairs would be replaced
  by others, and 1 pair that stays would land in the other half). The committed
  sheet is never redrawn to follow the mapper; its labels belong to these exact
  pairs. `tests/test_evaluation.py` pins every pair with its dev/test assignment
  and compares every non-label column with the current criteria and tests, so
  moving a pair between halves or editing a data column fails a test.
