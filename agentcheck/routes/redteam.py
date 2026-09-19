"""Adversarial suite: run it, price it, list its families."""

from fastapi import Header, HTTPException

from agentcheck import redteam
from agentcheck import screens
from agentcheck.judges import get_judge
from agentcheck.routes import shared
import asyncio


def register(app, store, guard, default_judge):
    @app.post("/v1/redteam")
    async def redteam_run(authorization: str | None = Header(None),
                          checkset: str = "safety",
                          families: str | None = None,
                          judge: str | None = None):
        key = shared.authorize(store, authorization)
        cset = shared.get_checkset(checkset)
        fams = [f.strip() for f in families.split(",") if f.strip()] if families else None
        try:
            attacks = redteam.corpus(fams)
        except KeyError as e:
            raise HTTPException(422, str(e))
        # Price the run before doing any of it, like a batch upload.
        cost = len(attacks) * len(cset.checks)
        if not guard.allow(key, cost):
            raise HTTPException(429, "rate limit exceeded; questions-per-minute cap")
        meta = store.key_meta(key) or {}
        allowance = meta.get("monthly_allowance", 500)
        if store.used_this_month(key) + cost > allowance:
            raise HTTPException(402, shared.collision_message(store, key))

        j = get_judge(judge or default_judge)

        def run():
            rows = []
            for a in attacks:
                judgment = j.ask(a.trace(), list(cset.checks))
                answers = {x.question_id: x for x in judgment.answers}
                v = answers.get(cset.verdict)
                probe = {"trace_verdict": str(v.value) if v else None}
                screens.apply(probe, a.trace())
                verdict = probe["trace_verdict"]
                rows.append({
                    "id": a.id, "family": a.family,
                    "verdict": verdict,
                    "confidence": float(v.confidence) if v and v.confidence else None,
                    "note": a.note, "trace": a.trace(),
                    "screen": probe.get("screen"),
                    "evaded": verdict == "pass",
                })
            return rows

        loop = asyncio.get_running_loop()
        try:
            rows = await loop.run_in_executor(None, run)
        except Exception as e:
            raise HTTPException(502, f"red team run failed: {e}")
        store.record(user_key=key, judge=j.name,
                     model=getattr(j, "_model", None) or j.name,
                     request_id=None, input_tokens=0, output_tokens=0,
                     questions=cost, server_ms=None, cached=0, ok=1)
        by_family: dict[str, dict] = {}
        for r in rows:
            s = by_family.setdefault(r["family"], {"n": 0, "evaded": 0})
            s["n"] += 1
            s["evaded"] += 1 if r["evaded"] else 0
        evaded = [r for r in rows if r["evaded"]]
        asr = len(evaded) / len(rows) if rows else 0.0
        # Persist the probe so the trust score can see adversarial
        # robustness without re-running 100+ judge calls on every read.
        try:
            store.record_event(key, "redteam_run", {
                "judge": j.name,
                "checkset": cset.name,
                "families": sorted(fams) if fams else None,
                "n": len(rows),
                "evaded": len(evaded),
                "asr": asr,
                "high_conf_evaded": sum(
                    1 for r in evaded
                    if isinstance(r.get("confidence"), (int, float))
                    and r["confidence"] >= 0.5),
            })
        except Exception:
            pass
        return {
            "judge": j.name,
            "checkset": cset.name,
            "n": len(rows),
            "asr": asr,
            "by_family": by_family,
            "evaded": evaded,
            "attacks": rows,
        }

    @app.get("/v1/redteam/families")
    def redteam_families():
        """Every family with its size and kind, so a UI can price a run
        before it starts instead of hardcoding a count that goes stale."""
        counts: dict[str, int] = {}
        for a in redteam.corpus():
            counts[a.family] = counts.get(a.family, 0) + 1
        industries = set(redteam.industry_names())
        return {
            "families": redteam.family_names(),
            "counts": counts,
            "industries": sorted(industries),
            "mechanics": sorted(set(redteam.family_names()) - industries),
            "total": sum(counts.values()),
        }
