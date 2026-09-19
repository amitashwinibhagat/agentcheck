"""Trust score and its shareable badge. One function serves both."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Header, Response

from agentcheck import trust
from agentcheck.reliability import load_report
from agentcheck.routes import shared

if TYPE_CHECKING:  # annotations only; the store is duck-typed at runtime
    from agentcheck.store import Store


def _calibration_for(store: Store, checkset: str | None) -> dict | None:
    """The published calibration report, in the shape trust_score wants.

    This is the bridge the measured tier never had: `calibrate --publish`
    writes calibration.json for the reliability model, but the trust endpoint
    read nothing, so the tier could never leave `consistency` however many
    labels were supplied. Scoped by rubric — a calibration for `safety` must
    not upgrade the tier for `refund-policy`.
    """
    report = trust.dataset_report_from_calibration(load_report(store))
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
    return trust.trust_score(rows, gate=gate, adversarial=adv,
                             dataset_report=_calibration_for(store, checkset))


def register(app, store):
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

    @app.get("/v1/trust.svg")
    async def trust_badge(authorization: str | None = Header(None),
                          checkset: str | None = None,
                          gate: float = 0.6):
        """A shareable badge: trust level + score + sample size."""
        key = shared.authorize(store, authorization)
        ts = _trust_for(store, key, checkset, gate)
        return Response(content=trust.render_badge(ts), media_type="image/svg+xml")
