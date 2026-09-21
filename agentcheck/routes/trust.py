"""Trust score and its shareable badge. One function serves both."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import Header, HTTPException, Request, Response

from agentcheck import calibration as cal
from agentcheck import trust
from agentcheck.reliability import load_report
from agentcheck.routes import shared

#: Decided sign-outs required before a measured tier means anything. Mirrors
#: the gate in trust.trust_score; stated here so the UI can show progress
#: toward it instead of a vague "publish a calibration".
SIGNOFFS_NEEDED = 30

if TYPE_CHECKING:  # annotations only; the store is duck-typed at runtime
    from agentcheck.store import Store


def _calibration_for(store: Store, checkset: str | None,
                     workspace_id: str | None = None) -> dict | None:
    """The published calibration report, in the shape trust_score wants.

    This is the bridge the measured tier never had: `calibrate --publish`
    writes calibration.json for the reliability model, but the trust endpoint
    read nothing, so the tier could never leave `consistency` however many
    labels were supplied. Scoped by rubric — a calibration for `safety` must
    not upgrade the tier for `refund-policy` — and by WORKSPACE, because an
    instance-wide file let one tenant's publish move everybody's tier.
    """
    report = trust.dataset_report_from_calibration(
        load_report(store, workspace_id))
    if report and checkset and report.get("checkset") and \
            report["checkset"] != checkset:
        return None
    return report


def _trust_for(store: Store, key: str, checkset: str | None,
               gate: float) -> dict:
    """The trust payload both the JSON endpoint and the SVG badge serve.

    One function so the badge can never drift from the number: same rows,
    same rubric scope, same adversarial probe, same calibration bridge.
    """
    rows = store.results(key, limit=5000)
    if checkset:
        rows = [r for r in rows if r.get("checkset") == checkset]
    try:
        adv = store.latest_event(key, "redteam_run")
    except Exception:
        adv = None
    ws = store.workspace_for_key(key)
    out = trust.trust_score(
        rows, gate=gate, adversarial=adv,
        dataset_report=_calibration_for(store, checkset,
                                       ws["id"] if ws else None))
    # Progress toward the measured tier, from the user's OWN labels. The score
    # cannot tell you how close you are; this can, and it is the one number
    # that turns "come back later" into "eight more".
    counts = store.signoff_counts(key, gate=gate)
    out["signoffs"] = {**counts, "needed": SIGNOFFS_NEEDED,
                       "remaining": max(SIGNOFFS_NEEDED - counts["decided"], 0)}
    return out


def register(app, store):
    def _signoff_report(key: str, checkset: str | None, gate: float) -> dict:
        rows = store.signoffs(key)
        if checkset:
            rows = [r for r in rows if r.get("checkset") == checkset]
        rep = cal.report_from_signoffs(
            rows, getattr(app.state, "judge", "unknown"),
            checkset=checkset or "safety", gate=gate)
        # Name the source. "a labeled dataset" is vague; this is the user's own
        # traffic, and the tier note should say so.
        rep["dataset"] = "your sign-outs"
        rep["needed"] = SIGNOFFS_NEEDED
        rep["meets_threshold"] = rep["decided"]["n"] >= SIGNOFFS_NEEDED
        return rep

    @app.get("/v1/trust")
    async def trust_score_endpoint(authorization: str | None = Header(None),
                    checkset: str | None = None,
                    gate: float = 0.6):
        """Live Trust Score for the caller's stored results.

        Tier 'consistency': no labels needed. When the caller has signed
        out assessments, human agreement folds in as one component.
        """
        key = shared.authorize(store, authorization)
        return _trust_for(store, key, checkset, gate)

    @app.get("/v1/calibration/signoffs")
    async def signoff_calibration(authorization: str | None = Header(None),
                                  checkset: str | None = None,
                                  gate: float = 0.6):
        """Calibrate the judge against this workspace's own sign-outs.

        No judge calls and no dataset file: the confidence is the one recorded
        with each judgment and the truth is the person who signed it out, so
        the measured tier becomes reachable on your traffic rather than on
        ours.
        """
        return _signoff_report(shared.authorize(store, authorization),
                               checkset, gate)

    @app.post("/v1/calibration/signoffs/publish")
    async def publish_signoff_calibration(request: Request,
                                          authorization: str | None = Header(None),
                                          checkset: str | None = None,
                                          gate: float = 0.6):
        """Publish the sign-out calibration as this workspace's reliability model.

        Same file the CLI writes, so both paths converge: the trust score
        picks it up on the next request, and per-check corrections on the next
        restart (the check pipeline loads the model at startup).
        """
        rep = _signoff_report(shared.authorize(store, authorization),
                              checkset, gate)
        if rep["decided"]["n"] == 0:
            raise HTTPException(
                422, "no decided sign-outs yet — sign judgments out first; a "
                     "report with no labels would be a fabricated number")
        home = Path(os.environ.get("AGENTCHECK_HOME", Path.home() / ".agentcheck"))
        home.mkdir(parents=True, exist_ok=True)
        # Per WORKSPACE. A single instance-wide calibration.json meant Alice
        # publishing moved Bob's tier to `measured` with her ECE attached to
        # his traffic — verified before this change, and a compliance problem
        # for a hosted deployment.
        ws = store.workspace_for_key(shared.authorize(store, authorization))
        wid = (ws or {}).get("id")
        if not wid:
            raise HTTPException(403, "this key has no workspace")
        target = home / f"calibration-{wid}.json"
        target.write_text(json.dumps(rep, indent=2))
        # The check pipeline reads the model at startup, so say so rather than
        # implying the correction changed mid-flight.
        return {"published": str(target), "report": rep,
                "tier_now": "measured" if rep["meets_threshold"] else "consistency",
                "note": "trust score updates immediately; restart serve to "
                        "apply corrections to new checks"}

    @app.get("/v1/trust.svg")
    async def trust_badge(authorization: str | None = Header(None),
                          checkset: str | None = None,
                          gate: float = 0.6):
        """A shareable badge: trust level + score + sample size."""
        key = shared.authorize(store, authorization)
        ts = _trust_for(store, key, checkset, gate)
        return Response(content=trust.render_badge(ts), media_type="image/svg+xml")
