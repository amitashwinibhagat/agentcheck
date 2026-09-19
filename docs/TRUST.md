# How AgentCheck decides what to trust

Four artifacts, one loop:

**Rubric** (what "right" means) → **verdict + confidence** (the judge's
judgment, measured) → **policy** (what happens next) → **human** (everything
the machine is not confident about).

1. **Rubrics** are YAML files. They are your evaluation standard, not ours.
   See [RUBRICS.md](RUBRICS.md).
2. **The Trust Score** measures the judge itself: confidence spread, verdict
   stability, agreement with human sign-outs, abstention rate — and, once you
   have labels, calibration (ECE) and accuracy. A judge that always says 0.9
   is not discriminating, whatever its accuracy. The score ships with its
   sample size, always. `GET /v1/trust`, `GET /v1/trust.svg`.
3. **Decision policies** turn judgment into action: ordered rules over
   verdict, confidence and severity → approve, human, or block. Simulate a
   policy against history before you trust it. This release is
   observe-and-recommend: nothing is actioned without the caller asking.
4. **The Decision Log** is the live record. Every judged call lands in the
   queue as it happens (SSE), with the verdict, the confidence, and the
   policy's decision. Low-confidence items are the ones a human sees.

The red-team suite answers the question the other three cannot: what does
the judge do when someone is actively trying to make it fail? Trust that has
not been attacked is a guess.

## The numbers that matter

- **Decided vs abstained.** On the shipped demo dataset the judge's decided
  items were 0.91 accurate; the ones it abstained on were 0.48 accurate if
  forced. Gating uncertain items to humans is not a nice-to-have — it is the
  difference between 0.91 and a coin flip.
- **Low confidence is never a pass.** The red-team suite catches the judge
  approving an over-broad export (`scope_creep.export_not_view`) at
  confidence **0.12**, and health-data targeting (`adtech.sensitive_health_target`)
  at **0.13**. A pass below the gate is a finding, not a result — and these
  are why the gate, not the verdict alone, decides what a human sees.

## Validation: does the consistency tier predict accuracy?

`scripts/trust-validation.py` (Sept 2026, 92 labeled traces × stub +
typesafe, 50 bootstrap subsets). Result: Spearman **0.70** between the
unlabeled consistency score and labeled accuracy — the tier clears the
0.5 bar for PREDICTIVE.

The honest caveat is in the same file: within-judge rank correlation is
~0 (stub −0.02, typesafe +0.19). The tier **separates good judges from bad
ones** (stub 44/0.62 vs typesafe 78/0.78) but does not rank runs of the
same judge. Claim the first, never the second. Re-run the script before
quoting the number — it costs one judge pass per trace, not per subset.
