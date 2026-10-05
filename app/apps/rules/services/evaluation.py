"""Frozen expression evaluation context.

A :class:`FrozenEvalContext` freezes every non-deterministic function exposed
to rule expressions (``datetime.now``, ``date.today``, ``random``,
``randint``) so that a preview produced for a fixed transaction version and
the actual execution can share an identical evaluation environment.

The context is serializable to a small payload and to a signed Django token.
"""

import decimal
import random as random_module
import secrets
from datetime import date, datetime, timezone

from dateutil.relativedelta import relativedelta
from django.core import signing
from django.utils import timezone as django_timezone

from apps.rules.utils import transactions as transactions_utils

_TOKEN_SALT = "apps.rules.services.evaluation.v1"


class FrozenEvalContext:
    """Evaluation environment frozen at ``evaluated_at`` with ``seed``."""

    def __init__(self, evaluated_at: datetime, seed: int):
        if django_timezone.is_naive(evaluated_at):
            evaluated_at = django_timezone.make_aware(evaluated_at, timezone.utc)
        self.evaluated_at = evaluated_at
        self.seed = int(seed)

        self._rng = random_module.Random(self.seed)
        self.random = self._rng.random
        self.randint = self._rng.randint

        self.datetime = self._build_frozen_datetime()
        self.date = self._build_frozen_date()

    # -- constructors ----------------------------------------------------

    @classmethod
    def fresh(cls, now: datetime | None = None, seed: int | None = None):
        """A live context: current instant plus a cryptographically random seed."""
        return cls(
            evaluated_at=now or django_timezone.now(),
            seed=seed if seed is not None else secrets.randbits(63),
        )

    @classmethod
    def from_payload(cls, payload: dict):
        return cls(
            evaluated_at=datetime.fromisoformat(payload["evaluated_at"]),
            seed=int(payload["seed"]),
        )

    # -- serialization ---------------------------------------------------

    def to_payload(self) -> dict:
        return {
            "evaluated_at": self.evaluated_at.isoformat(),
            "seed": self.seed,
        }

    def issue_token(self) -> str:
        return signing.dumps(self.to_payload(), salt=_TOKEN_SALT)

    @classmethod
    def read_token(cls, token: str, max_age: int | None = None):
        """Validate ``token`` and rebuild the context.

        Raises ``django.core.signing.BadSignature`` (or its
        ``SignatureExpired`` subclass) on tampering or expiry.
        """
        payload = signing.loads(token, salt=_TOKEN_SALT, max_age=max_age)
        return cls.from_payload(payload)

    # -- expression function namespace -----------------------------------

    def functions(self) -> dict:
        """Return the ``functions`` mapping for simpleeval.

        Keys are kept identical to the historical rule engine.
        """
        return {
            "relativedelta": relativedelta,
            "str": str,
            "int": int,
            "float": float,
            "abs": abs,
            "randint": self.randint,
            "random": self.random,
            "decimal": decimal.Decimal,
            "datetime": self.datetime,
            "date": self.date,
            "transactions": transactions_utils.TransactionsGetter,
        }

    # -- frozen datetime / date classes ----------------------------------

    def _build_frozen_datetime(self):
        evaluated_at = self.evaluated_at

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return evaluated_at
                return evaluated_at.astimezone(tz)

            @classmethod
            def utcnow(cls):
                return evaluated_at.astimezone(timezone.utc).replace(tzinfo=None)

            @classmethod
            def today(cls, tz=None):
                if tz is None:
                    return evaluated_at.date()
                return evaluated_at.astimezone(tz).date()

        return FrozenDatetime

    def _build_frozen_date(self):
        evaluated_at = self.evaluated_at

        class FrozenDate(date):
            @classmethod
            def today(cls):
                return evaluated_at.date()

        return FrozenDate
