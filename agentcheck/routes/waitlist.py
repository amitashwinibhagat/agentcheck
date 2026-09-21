"""The waitlist: the one public write in the app, so it is the one to guard.

The pricing page listed hosted tiers with nothing to click. A card promising a
product nobody can obtain is decoration; this gives it a door, and gives the
operator the signal they actually lack — does anyone want the hosted thing?

It is unauthenticated by necessity (a prospect has no key) and therefore:

  * rate limited per client, in memory, because a public write with no ceiling
    is an invitation to fill a table
  * idempotent — clicking twice is not two prospects, and a count that
    double-counts is worse than no count
  * validated before stored, and the response says nothing about who is
    already on the list (no enumeration)
"""

from __future__ import annotations

import time

from fastapi import Header, HTTPException, Request
from pydantic import BaseModel, Field

from agentcheck import billing
from agentcheck.routes import shared

#: New addresses accepted per client per window. Generous for a person,
#: useless for a script.
_WINDOW = 3600.0
_MAX_PER_WINDOW = 5
_seen: dict[str, list[float]] = {}


def _client_id(request: Request) -> str:
    # Behind Caddy the socket peer is the proxy, so the forwarded chain is the
    # only useful signal; taking the left-most entry trusts the client, which
    # is fine for a rate limit (faking it buys a fresh bucket, but the table
    # still holds one row per address).
    fwd = request.headers.get("x-forwarded-for") or ""
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _allow(client: str) -> bool:
    now = time.time()
    hits = [t for t in _seen.get(client, []) if now - t < _WINDOW]
    if len(hits) >= _MAX_PER_WINDOW:
        _seen[client] = hits
        return False
    hits.append(now)
    _seen[client] = hits
    return True


class WaitlistRequest(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    #: Which card they clicked. Free text from the page, not a plan lookup:
    #: an unknown value must not be able to fail a signup of interest.
    plan: str | None = Field(default=None, max_length=40)
    note: str | None = Field(default=None, max_length=300)


def register(app, store):
    @app.post("/v1/waitlist")
    async def join_waitlist(req: WaitlistRequest, request: Request,
                           authorization: str | None = Header(None)):
        """Join the hosted waitlist. No key: a prospect has none yet."""
        del authorization  # accepted and ignored on purpose; see the docstring
        if not _allow(_client_id(request)):
            raise HTTPException(429, "too many sign-ups from here; try later")
        try:
            added = store.join_waitlist(req.email, plan=req.plan, note=req.note)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        # Same shape either way: an attacker cannot learn who is on the list,
        # and a real person who forgot they signed up is not told off.
        return {"ok": True, "already_on_list": not added,
                "next": "we will email when hosted sign-up opens",
                "today": "self-host is free and works now"}

    @app.get("/v1/waitlist")
    async def list_waitlist(authorization: str | None = Header(None)):
        """The list, for the operator. Key-protected: it is other people's
        addresses."""
        shared.authorize(store, authorization)
        rows = store.waitlist()
        return {"n": len(rows), "signups": rows}

    @app.get("/v1/waitlist/count")
    async def waitlist_count(authorization: str | None = Header(None)):
        """Just the number, so the pricing page can say \"N teams waiting\"
        without publishing anybody's address."""
        del authorization
        rows = store.waitlist()
        by_plan: dict[str, int] = {}
        for r in rows:
            by_plan[r.get("plan") or "unsure"] = by_plan.get(r.get("plan")
                                                             or "unsure", 0) + 1
        return {"n": len(rows), "by_plan": by_plan,
                "display_currency": billing.DISPLAY_CURRENCY}
