# Suite comparison

Sample sizes: 18 criteria, 10 mutants, 43 baseline tests, 18 criteria-driven tests. Mapper threshold 0.4, top-k 3.

Requirement coverage and mutation score answer different questions. They are shown side by side and are never combined into one number.

## Requirement coverage

Share of criteria with at least one passing test of the suite tagged to them (control run on the real API).

| suite | tests | criteria covered | coverage |
|---|---|---|---|
| A. Baseline (schema only) | 43 | 13 of 18 | 0.722 |
| B. Criteria-driven | 18 | 18 of 18 † | 1.000 † |
| C. Combined (A + B) | 61 | 18 of 18 † | 1.000 † |

† True by construction: these tests were generated from the criteria being counted, so every criterion has a test. It says nothing about test quality.

Criteria with no passing tagged test:

- A. Baseline (schema only): REQ-002, REQ-008, REQ-011, REQ-012, REQ-018
- B. Criteria-driven: none
- C. Combined (A + B): none

## Mutation score

The "mutants" are hand-written seeded faults (copies of the API with one rule broken), not mutants produced by automatic mutation operators. The mutation score is the share of seeded faults a suite detects: a fault is detected when at least one test of the suite failed on it. A passing control run is required first. Setup errors never count.

The headline score counts only independent kills. A kill that exists because a test was changed after the fault survived is shown separately.

| suite | faults detected | mutation score | survived | detected if tuned kills are counted |
|---|---|---|---|---|
| A. Baseline (schema only) | 6 of 10 | 0.600 | category_accepts_toy, get_unknown_product_200, health_body_wrong, order_unknown_product_201 | same |
| B. Criteria-driven | 7 of 10 | 0.700 | category_accepts_toy, order_quantity_max_11, price_max_exclusive | 8 of 10 |
| C. Combined (A + B) | 9 of 10 | 0.900 | category_accepts_toy | 10 of 10 |

### Per mutant

| mutant | A | B | C |
|---|---|---|---|
| category_accepts_toy | survived | killed (tuned) | killed (tuned) |
| get_unknown_product_200 | survived | killed | killed |
| health_body_wrong | survived | killed | killed |
| name_max_length_41 | killed | killed | killed |
| name_min_length_2 | killed | killed | killed |
| order_quantity_max_11 | killed | survived | killed |
| order_unknown_product_201 | survived | killed | killed |
| price_allows_zero | killed | killed | killed |
| price_max_exclusive | killed | survived | killed |
| stock_allows_negative_one | killed | killed | killed |

Tuned kills: category_accepts_toy: REQ-009's test was extended with the value 'toy' after the mutant survived.

## Limitations

- **Coverage is not comparable across suites.** For A the tags come from the mapper, so its coverage inherits the mapper's errors. For B and C it is true by construction (†). Coverage is a traceability measure, not proof that the tests are good.
- **A passing test is not a verified requirement.** Mapped is not verified.
- **The mutation score is less circular than coverage, but it is not independent.** The 10 seeded faults, the criteria and the tests were all written inside this project, and some faults target behaviour the criteria describe, so B's detections partly reflect that the same hands wrote both. One extra detection moves the score by 0.1.
- **No equivalent mutants were declared.** A survivor may be a real gap or a fault no test could distinguish; read each survivor, do not just count.
- **C is the union of A and B**, taken from separate runs. See the module docstring for why that equals one combined run.
- **Results describe this demo API and these criteria.** They are not estimates for other APIs.
