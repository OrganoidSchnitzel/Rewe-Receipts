"""Spliit integration.

Spliit's backend is a Next.js app that exposes its API through **tRPC**. We use
two routes:

* ``groups.get``            -> fetch the group + its participants (query, GET)
* ``groups.expenses.create`` -> create an expense (mutation, POST)

Money semantics (verified against spliit-app/spliit source):

* ``amount`` is stored as an **integer number of cents** (e.g. 4.48 EUR -> 448).
* Each ``paidFor`` share is stored verbatim; in ``splitMode: "EVENLY"`` Spliit
  **ignores** the share values and divides the amount equally among the listed
  participants (``src/lib/shares.ts`` maps EVENLY to ``() => 1``). So an even
  split with a single payer is correct regardless of the ``shares`` value we
  send — we send ``1`` to satisfy the "positive" schema validation.

The tRPC input is wrapped for superjson as ``{"json": <input>}`` and the
response unwrapped from ``result.data.json``.

NOTE: self-hosted Spliit has no API auth by default. If a reverse proxy in
front of it needs a bearer token, set ``SPLIIT_API_KEY`` and it is sent as an
``Authorization: Bearer`` header.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from . import config


class SpliitError(RuntimeError):
    pass


@dataclass
class Participant:
    id: str
    name: str


# --- low-level tRPC helpers --------------------------------------------------

def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if config.SPLIIT_API_KEY:
        headers["Authorization"] = f"Bearer {config.SPLIIT_API_KEY}"
    return headers


def _trpc_query(procedure: str, payload: dict[str, Any]) -> Any:
    """Call a tRPC *query* (HTTP GET, input in the querystring)."""
    url = f"{config.SPLIIT_URL}/api/trpc/{procedure}"
    params = {"input": json.dumps({"json": payload})}
    response = requests.get(
        url, params=params, headers=_headers(), timeout=config.HTTP_TIMEOUT
    )
    return _unwrap(response)


def _trpc_mutation(procedure: str, payload: dict[str, Any]) -> Any:
    """Call a tRPC *mutation* (HTTP POST, input in the body)."""
    url = f"{config.SPLIIT_URL}/api/trpc/{procedure}"
    response = requests.post(
        url, data=json.dumps({"json": payload}), headers=_headers(),
        timeout=config.HTTP_TIMEOUT,
    )
    return _unwrap(response)


def _unwrap(response: requests.Response) -> Any:
    """Unwrap a tRPC/superjson HTTP response to its data payload.

    Handles both batched (list) and non-batched (dict) response shapes. On a
    non-2xx status, tRPC still returns a JSON body describing exactly what
    failed (e.g. a Zod validation message) — surface that instead of the bare
    "400 Client Error" requests would otherwise raise, which discards it.
    """
    try:
        body = response.json()
    except ValueError:
        response.raise_for_status()
        raise SpliitError(f"Non-JSON response from Spliit: {response.text[:500]!r}")

    if isinstance(body, list):
        body = body[0] if body else {}
    if isinstance(body, dict) and "error" in body:
        error = body["error"]
        message = error.get("json", {}).get("message") if isinstance(error, dict) else error
        raise SpliitError(f"Spliit rejected the request: {message or error}")

    response.raise_for_status()
    try:
        return body["result"]["data"]["json"]
    except (KeyError, TypeError) as exc:
        raise SpliitError(f"Unexpected Spliit response shape: {body!r}") from exc


# --- public API --------------------------------------------------------------

def get_participants(group_id: Optional[str] = None) -> list[Participant]:
    group_id = group_id or config.SPLIIT_GROUP_ID
    data = _trpc_query("groups.get", {"groupId": group_id})
    group = data.get("group", data) if isinstance(data, dict) else {}
    participants = group.get("participants", []) if isinstance(group, dict) else []
    return [
        Participant(id=p["id"], name=p.get("name", ""))
        for p in participants
        if p.get("id")
    ]


def resolve_payer(participants: list[Participant]) -> Participant:
    """Pick the payer per config, defaulting to the first participant."""
    if not participants:
        raise SpliitError("Spliit group has no participants; cannot pick a payer.")

    if config.SPLIIT_PAYER_PARTICIPANT_ID:
        for p in participants:
            if p.id == config.SPLIIT_PAYER_PARTICIPANT_ID:
                return p
        raise SpliitError(
            f"SPLIIT_PAYER_PARTICIPANT_ID {config.SPLIIT_PAYER_PARTICIPANT_ID!r} "
            "not found among group participants."
        )

    if config.SPLIIT_PAYER_NAME:
        target = config.SPLIIT_PAYER_NAME.strip().lower()
        for p in participants:
            if p.name.strip().lower() == target:
                return p
        raise SpliitError(
            f"SPLIIT_PAYER_NAME {config.SPLIIT_PAYER_NAME!r} not found among "
            "group participants."
        )

    return participants[0]


def build_expense_payload(
    *,
    group_id: str,
    title: str,
    amount_cents: int,
    payer_id: str,
    participant_ids: list[str],
    category: int = 0,
    expense_date: Optional[str] = None,
    notes: str = "",
) -> dict[str, Any]:
    """Construct the ``groups.expenses.create`` input.

    Kept pure and side-effect free so it can be unit tested — a bug here would
    create incorrect real expenses.

    * ``amount_cents`` must already be an integer number of cents.
    * ``splitMode`` is EVENLY: every listed participant owes an equal share.
    """
    if amount_cents <= 0:
        raise SpliitError("Expense amount must be positive.")
    if not participant_ids:
        raise SpliitError("Expense must have at least one participant to split among.")
    if payer_id not in participant_ids:
        # Payer must be part of paidFor for an even split to include them.
        participant_ids = [payer_id, *participant_ids]

    # Spliit requires a coercible date; default to today when none is known.
    if not expense_date:
        expense_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    expense_form_values = {
        "title": title,
        "amount": int(amount_cents),
        "category": category,
        "expenseDate": expense_date,  # ISO date string
        "paidBy": payer_id,
        "paidFor": [
            {"participant": pid, "shares": 1} for pid in participant_ids
        ],
        "splitMode": "EVENLY",
        "saveDefaultSplittingOptions": False,
        "isReimbursement": False,
        "documents": [],
        "notes": notes,
        "recurrenceRule": "NONE",
    }
    # participantId is optional in Spliit's schema (identifies "you" for the
    # UI's "who am I" convenience) — omit the key rather than sending an
    # explicit null, since an optional (non-nullable) Zod field rejects null.
    return {
        "groupId": group_id,
        "expenseFormValues": expense_form_values,
    }


def create_expense(
    *,
    title: str,
    amount_eur: float,
    group_id: Optional[str] = None,
    notes: str = "",
    expense_date: Optional[str] = None,
) -> str:
    """Create an equal-split expense with the configured payer.

    ``amount_eur`` is a euro float; it is converted to integer cents here.
    Returns the new Spliit expense id.
    """
    group_id = group_id or config.SPLIIT_GROUP_ID
    amount_cents = round(amount_eur * 100)

    participants = get_participants(group_id)
    payer = resolve_payer(participants)
    payload = build_expense_payload(
        group_id=group_id,
        title=title,
        amount_cents=amount_cents,
        payer_id=payer.id,
        participant_ids=[p.id for p in participants],
        expense_date=expense_date,
        notes=notes,
    )
    data = _trpc_mutation("groups.expenses.create", payload)
    expense_id = data.get("expenseId") if isinstance(data, dict) else None
    if not expense_id:
        raise SpliitError(f"Spliit did not return an expenseId: {data!r}")
    return expense_id
