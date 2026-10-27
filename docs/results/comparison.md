# Suite comparison

Sample sizes: 18 criteria, 10 mutants, 43 baseline tests, 18 criteria-driven tests. Mapper threshold 0.4, top-k 3.

Requirement coverage and mutation score answer different questions. They are shown side by side and are never combined into one number.

## Requirement coverage

Share of criteria with at least one passing test of the suite tagged to them (control run on the real API).

| suite | tests | criteria covered | coverage |
|---|---|---|---|
| A. Baseline (schema only) | 43 | 13 of 18 | 0.722 |
| B. Criteria-driven | 18 | 18 of 18 | 1.000 |
| C. Combined (A + B) | 61 | 18 of 18 | 1.000 |

Criteria with no passing tagged test:

- A. Baseline (schema only): REQ-002, REQ-008, REQ-011, REQ-012, REQ-018
- B. Criteria-driven: none
- C. Combined (A + B): none

## Mutation score

Killed means at least one test of the suite failed on the mutant. A passing control run is required first. Setup errors never count as kills.

| suite | mutants killed | mutation score | survived |
|---|---|---|---|
| A. Baseline (schema only) | 6 of 10 | 0.600 | category_accepts_toy, get_unknown_product_200, health_body_wrong, order_unknown_product_201 |
| B. Criteria-driven | 8 of 10 | 0.800 | order_quantity_max_11, price_max_exclusive |
| C. Combined (A + B) | 10 of 10 | 1.000 | none |

### Per mutant

| mutant | A | B | C |
|---|---|---|---|
| category_accepts_toy | survived | killed | killed |
| get_unknown_product_200 | survived | killed | killed |
| health_body_wrong | survived | killed | killed |
| name_max_length_41 | killed | killed | killed |
| name_min_length_2 | killed | killed | killed |
| order_quantity_max_11 | killed | survived | killed |
| order_unknown_product_201 | survived | killed | killed |
| price_allows_zero | killed | killed | killed |
| price_max_exclusive | killed | survived | killed |
| stock_allows_negative_one | killed | killed | killed |

## Limitations

- **Coverage is not comparable across suites.** For A the tags come from the mapper, so its coverage inherits the mapper's errors. For B and C the tests were generated from the criteria being counted, so their coverage is partly circular. Coverage is a traceability measure, not proof that the tests are good.
- **A passing test is not a verified requirement.** Mapped is not verified.
- **Mutation score is the independent check, but it is small.** The 10 mutants were written by hand inside this project, not by an independent party, for this one demo API, and some target behaviours the criteria describe, so B's kills partly reflect that the same hands wrote both. One extra kill moves the score by 0.1.
- **One kill is tuned to its mutant.** REQ-009's generated test tries `toy` among its unsupported values because `category_accepts_toy` survived without it, so B's and C's kill of that mutant is not independent. Counting it, B and C score one higher than the independent figures.
- **No equivalent mutants were declared.** A survivor may be a real gap or a mutant no test could distinguish; each survivor should be read, not counted blindly.
- **C is the union of A and B**, taken from separate runs. See the module docstring for why that equals one combined run.
- **Results describe this demo API and these criteria.** They are not estimates for other APIs.
