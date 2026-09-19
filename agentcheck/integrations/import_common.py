"""Shared ground for trace importers (Langfuse, LangSmith, ...).

The job: turn someone else's run history into AgentCheck dataset records
without inventing ground truth. Two cases:

  human gold   the source has a score/feedback (a person said good/bad).
               That becomes label_verdict/should_fail with the provenance
               recorded, because independent gold is the whole game.
  no gold      the record ships with needs_label=True and NO gold keys.
               predict_dataset refuses those (it used to silently grade
               them as "pass" — see the hardening note in calibration).

No importer here invents a verdict from vibes. Ever.
"""

from __future__ import annotations


def gold_from_score(value) -> str | None:
    """A human score value -> pass/fail/None (abstain, not invent)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "pass" if value else "fail"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Binary-ish scores (0/1) are near-universal in these APIs. A
        # fraction is an opinion, not a verdict, so it abstains. Integers
        # on ranked scales (1 = best) would misread: name such scores
        # carefully, and spot-check the import report.
        if value >= 1:
            return "pass"
        if value <= 0:
            return "fail"
        return None
    s = str(value).strip().lower()
    if s in ("pass", "correct", "good", "1", "true", "yes", "approve"):
        return "pass"
    if s in ("fail", "incorrect", "bad", "0", "false", "no", "reject"):
        return "fail"
    return None


def make_record(trace: dict, gold: str | None = None,
                provenance: dict | None = None) -> dict:
    """One dataset record. Gold keys exist ONLY when gold is real."""
    rec = {"trace": {
        "request": str(trace.get("request") or ""),
        "tool": str(trace.get("tool") or ""),
        "args": trace.get("args") or {},
    }}
    if gold in ("pass", "review", "fail"):
        rec["label_verdict"] = gold
        rec["should_fail"] = gold != "pass"
    else:
        rec["needs_label"] = True
    if provenance:
        rec["provenance"] = provenance
    return rec


def summarize(records: list[dict]) -> dict:
    gold = [r for r in records if "label_verdict" in r]
    return {
        "n": len(records),
        "gold": len(gold),
        "gold_fail": sum(1 for r in gold if r.get("should_fail")),
        "needs_label": sum(1 for r in records if r.get("needs_label")),
    }
