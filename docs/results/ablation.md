# Ablation: rules versus TF-IDF

40 labelled pairs, 5 positive. Threshold 0.4, top-k 3 where they apply.

| variant | TP | FP | FN | TN | precision | recall | F1 | pairs predicted over all criteria x tests |
|---|---|---|---|---|---|---|---|---|
| Rules only | 5 | 9 | 0 | 26 | 0.357 | 1.000 | 0.526 | 36 |
| Rules + TF-IDF (configured) | 5 | 0 | 0 | 35 | 1.000 | 1.000 | 1.000 | 14 |
| TF-IDF only | 4 | 3 | 1 | 32 | 0.571 | 0.800 | 0.667 | 29 |

## By split

| variant | split | precision | recall |
|---|---|---|---|
| Rules only | dev | 0.143 | 1.000 |
| Rules only | test | 0.571 | 1.000 |
| Rules + TF-IDF (configured) | dev | 1.000 | 1.000 |
| Rules + TF-IDF (configured) | test | 1.000 | 1.000 |
| TF-IDF only | dev | 0.000 | 0.000 |
| TF-IDF only | test | 0.667 | 1.000 |

## Limits

- 40 labelled pairs, 5 positives, one labeller. One positive moves recall by 0.2.
- The sample is stratified on the mapper's own eligibility, so it is not a random draw of all pairs and precision here is not the precision over every criterion-test pair. Predicted pairs that were never labelled cannot be scored (the last column shows how many each variant predicts in total).
- The rules were written after seeing labelled errors, which favours the rule-based variants on these labels.
- Read the contrast between variants, not any single number.
