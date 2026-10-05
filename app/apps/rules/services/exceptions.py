"""Exceptions shared by rule planning, execution and the worker."""


class PlannedActionError(Exception):
    """Raised when the executor reaches an action that could not be planned.

    The planner records such actions as ``will_fail`` (e.g. an expression
    that raises at evaluation time); the executor raises this error at the
    action's position so savepoint/rollback semantics are preserved.
    """


class RetryableEventError(Exception):
    """A transient conflict during event processing.

    Raised on narrow upsert lock conflicts (NOWAIT) and on create-time
    unique races. procrastinate retries the whole event when it sees this
    exception.
    """
