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

## A second rater (an AI model), not a self-agreement pass

On 2026-10-23 Claude (configured model id `claude-sonnet-5-5`; the serving model may
differ) labelled the same 20 pairs as
`data/relabelled_pairs.csv` and saved them to `data/relabelled_pairs_claude.csv`.
The labeller's own blank sheet `data/relabelled_pairs.csv` is untouched; the
labeller's self-agreement pass is still open and this does not replace it. Run it with
`assay evaluate agreement --relabels data/relabelled_pairs_claude.csv --second-rater "Claude (an AI model)"`;
without `--second-rater` the report calls itself self-agreement, which it is not.

How it was done: from the sheet text and the question above only, before opening the
first labels. It is **not blind to the mapper**: Claude helped write the mapper's
rules and had seen the mapper's output and the evaluation result, which may have
shaped its reading.

Result: agreed on 19 of 20 pairs (observed agreement 0.950; positives 2 from the
first labeller, 3 from Claude). With 20 pairs one label moves agreement by 5 points.
The tool also prints Cohen's kappa, but it is not meaningful at this size (about 5
positives) and is not reported as a result.

The one disagreement is `REQ-008|BASE-D9059F5116BA` (development half): the criterion
"Product category accepts a supported enum value" against the test
`POST /products` "valid input". The first labeller marked it 0, Claude 1 (marked unsure): the valid
input posts one supported category, so a broken API that rejected supported
categories would fail it, but it does not cover every value. It shows where the
protocol is ambiguous: whether a generic valid-input test verifies an "accepts a
supported value" criterion. **The first labeller's label stays the ground truth.** For scale only: had
that label been 1, the mapper would have missed it, and the development half would
read 1 of 2 positives found instead of 1 of 1; the sweep would still not run (fewer
than 5 development positives). No label was changed.

## A third rater (ChatGPT, model name unverified), blind

On 2026-10-23 the labeller gave the same 20 pairs to ChatGPT using a blind packet
(the protocol, the requirement and test text, no labels, no split, nothing about the
mapper). The model name (given as "ChatGPT 6") and what it was shown are as stated
by the labeller; the project could not verify either, so no exact model version is
claimed. Its raw answer is saved unedited in
`data/relabelled_pairs_chatgpt-6.csv`; run
`assay evaluate agreement --relabels data/relabelled_pairs_chatgpt-6.csv --second-rater "ChatGPT (model name as stated by the labeller, unverified)"`.
Unlike Claude it had not seen the mapper, so it is the closer to a blind reading.

Result against the first labels: agreed on 19 of 20 (observed agreement 0.950;
positives 2 first pass, 3 from ChatGPT). Against Claude's labels it agreed
on 18 of 20.

Where the raters differ:

| Pair | First labeller | Claude | ChatGPT |
|---|---|---|---|
| `REQ-008\|BASE-D9059F5116BA` (category accepts a supported value, "valid input") | 0 | 1 (unsure) | 0 |
| `REQ-005\|BASE-24F2A691793C` (name over 40 chars rejected, missing required name) | 0 | 0 | 1 |

On the first, two of three raters agree with the first labeller. On the second,
ChatGPT labelled 1 with a note that it holds "if name validation is enforced";
by the protocol a test for a missing name would still pass if over-long names were
accepted, so the first labeller's 0 stands. No label was changed. Three raters
agreeing on 18 of 20 pairs is some evidence the first labels are stable, from a
small sample and from raters that are not independent humans, so it does not make
the labels validated and does not replace the first labeller's own
self-agreement pass.

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
- **Not an independent holdout.** The mapper's rules were revised after labelled
  errors were seen, and the test half was probably visible when they were written,
  so the test-half result describes this sample and does not show the mapper
  generalises. A claim of quality needs a newly drawn, independently labelled
  sheet scored against a frozen mapper and configuration. The second pass below
  measures your consistency with yourself; it does not fix this.
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
