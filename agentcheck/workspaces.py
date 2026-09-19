"""Workspaces, seats, roles.

A workspace is the unit a team shares and pays for. Everything else in the
product already hangs off a key (results, metering, events); a key now hangs
off a workspace, so a seat is a person and a key is a credential.

Three deliberate choices:

  * seats come from ``billing.PLANS`` — one source of truth for what a plan
    includes, so a plan change cannot leave seat limits stale.
  * the owner is not a member row that can be deleted. A workspace with no
    owner is unrecoverable, so removal and demotion are refused rather than
    silently orphaning it.
  * invites carry a HASH of the token, never the token. Same rule as API keys:
    a database read should not hand over the ability to join.
"""

from __future__ import annotations

import hashlib
import secrets
import time

from agentcheck import billing

ROLES = ("owner", "admin", "member")
ROLE_RANK = {"member": 1, "admin": 2, "owner": 3}

# What each role may do. Readable on purpose: this table is the policy.
PERMISSIONS = {
    "member": {"view"},
    "admin": {"view", "manage_keys", "manage_members", "manage_invites"},
    "owner": {"view", "manage_keys", "manage_members", "manage_invites",
              "manage_billing", "delete_workspace"},
}

INVITE_TTL_SECONDS = 14 * 86400


class WorkspaceError(RuntimeError):
    """A refused workspace action, with a reason a user can act on."""


def seat_limit(plan: str) -> int:
    """How many people a plan admits. Unknown plan -> the free floor, never
    unlimited."""
    return int(billing.PLANS.get(plan, billing.PLANS["free"]).get("seats", 1))


def can(role: str, action: str) -> bool:
    return action in PERMISSIONS.get(role or "", set())


def require(role: str, action: str) -> None:
    if not can(role, action):
        raise WorkspaceError(
            f"role {role!r} may not {action.replace('_', ' ')}")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_invite(email: str, role: str, ttl: int = INVITE_TTL_SECONDS) -> dict:
    """(token, record fields) for a new invite. Token returned once."""
    if role not in ROLES:
        raise WorkspaceError(f"unknown role {role!r}; have {list(ROLES)}")
    if role == "owner":
        # Ownership is transferred, never handed out through an invite:
        # two owners from one link is how an account gets taken over.
        raise WorkspaceError("ownership is transferred, not invited")
    token = "inv_" + secrets.token_urlsafe(24)
    now = time.time()
    return {
        "token": token,
        "token_hash": hash_token(token),
        "email": str(email).strip().lower(),
        "role": role,
        "created": now,
        "expires_at": now + ttl,
    }


def invite_usable(record: dict, now: float | None = None) -> tuple[bool, str]:
    """(ok, reason). Expiry and single-use are both checked here so every
    caller enforces the same rules."""
    now = now if now is not None else time.time()
    if record.get("accepted_at"):
        return False, "invite already accepted"
    if not record.get("token_hash"):
        return False, "invite has no token"
    if float(record.get("expires_at") or 0) < now:
        return False, "invite expired"
    return True, ""


def seats_used(members: list[dict]) -> int:
    """A seat is a distinct person, once — a duplicate email is one seat."""
    return len({str(m.get("email") or "").strip().lower() for m in members})


def check_seat_available(limit: int, members: list[dict],
                         invited_emails: list[str] | None = None,
                         plan: str = "") -> None:
    """Raise when adding a member would exceed the workspace's seat limit.

    The limit is passed in rather than derived from the plan, because a
    workspace stores its own limit: an enterprise deal may carry seats the
    public tiers do not. ``plan`` is only used to make the message useful.

    Pending invites count: an invite is a promise of a seat, and letting them
    be handed out freely is how a 1-seat free plan ends up with 30 people.
    """
    limit = int(limit)
    emails = {str(m.get("email") or "").strip().lower() for m in members}
    emails |= {str(e).strip().lower() for e in (invited_emails or [])}
    if len(emails) >= limit:
        label = plan or "this plan"
        raise WorkspaceError(
            f"{label} includes {limit} seat{'s' if limit != 1 else ''}; "
            f"upgrade to add more")


def check_owner_survives(members: list[dict], email: str,
                         new_role: str | None) -> None:
    """Refuse a change that would leave the workspace with no owner.

    Demotion and removal both land here. A workspace with no owner cannot be
    billed, transferred, or deleted by anyone — it is a dead account, and the
    only way to avoid one is to never make the last one.
    """
    target = str(email).strip().lower()
    owners = {str(m.get("email") or "").strip().lower()
              for m in members if m.get("role") == "owner"}
    if target not in owners:
        return  # not an owner; other rules govern
    remaining = owners - {target}
    still_owner = (new_role == "owner")
    if not remaining and not still_owner:
        raise WorkspaceError(
            "this is the only owner; transfer ownership before removing or "
            "demoting them")
