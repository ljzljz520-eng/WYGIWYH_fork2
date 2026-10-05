"""Signed dry-run preview tokens.

A preview token binds together everything a commit must trust: the frozen
evaluation context, the event, the referenced transaction/rule and their
versions, the planned fingerprints and the input patch.  Clients cannot
tamper with one part without invalidating the signature, so the server
never compares two values both supplied by the client.
"""

import json
from datetime import date
from decimal import Decimal

from django.core import signing

from apps.rules.services.evaluation import FrozenEvalContext

_TOKEN_SALT = "apps.rules.services.preview_token.v1"


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _rehydrate(value, field):
    if field == "amount" and isinstance(value, str):
        return Decimal(value)
    if field in ("date", "reference_date") and isinstance(value, str):
        return date.fromisoformat(value)
    return value


def dump_input_patch(patch: dict) -> str:
    return json.dumps(dict(patch), default=_json_default)


def load_input_patch(text: str) -> dict:
    data = json.loads(text or "{}")
    return {key: _rehydrate(value, key) for key, value in data.items()}


def issue_preview_token(
    *,
    eval_context: FrozenEvalContext,
    event: str,
    transaction_ref: int,
    transaction_version: int,
    rule_ref: int,
    rule_version: int,
    fingerprints: list,
    input_patch: dict,
) -> str:
    payload = {
        "context": eval_context.to_payload(),
        "event": event,
        "transaction_ref": transaction_ref,
        "transaction_version": transaction_version,
        "rule_ref": rule_ref,
        "rule_version": rule_version,
        "fingerprints": sorted(fingerprints),
        "input_patch": json.loads(dump_input_patch(input_patch)),
    }
    return signing.dumps(payload, salt=_TOKEN_SALT)


def read_preview_token(token: str, max_age: int = 600) -> dict:
    """Validate the token and return the trusted preview payload.

    Raises ``django.core.signing.BadSignature`` on tampering/expiry.
    """
    payload = signing.loads(token, salt=_TOKEN_SALT, max_age=max_age)
    payload["context"] = FrozenEvalContext.from_payload(payload["context"])
    raw_patch = payload["input_patch"]
    payload["input_patch"] = {
        key: _rehydrate(value, key) for key, value in raw_patch.items()
    }
    return payload
