"""Procrastinate worker entry point for transaction events.

A transaction event (created/updated/deleted at a specific transaction
version) is processed once per rule version. Repeated deliveries return the
existing terminal :class:`RuleExecution`; stale events are recorded without
side effects.
"""

import logging

from django.contrib.auth import get_user_model
from django.db import IntegrityError, OperationalError, transaction
from procrastinate import RetryStrategy
from procrastinate.contrib.django import app

from apps.common.middleware.thread_local import (
    delete_current_user,
    get_current_user,
    write_current_user,
)
from apps.rules.models import RuleExecution, TransactionRule
from apps.rules.services.exceptions import RetryableEventError
from apps.rules.services.executor import (
    _is_lock_conflict,
    execute_plan,
)
from apps.rules.services.planner import build_plan
from apps.transactions.models import Transaction

logger = logging.getLogger(__name__)

_EVENT_RULE_FLAG = {
    RuleExecution.Event.CREATED: "on_create",
    RuleExecution.Event.UPDATED: "on_update",
    RuleExecution.Event.DELETED: "on_delete",
}


@app.task(
    name="process_transaction_event",
    retry=RetryStrategy(
        max_attempts=5,
        wait=1,
        linear_wait=2,
        retry_exceptions=[RetryableEventError],
    ),
)
def process_transaction_event(
    *,
    event,
    transaction_ref,
    transaction_version,
    user_id,
    transaction_data=None,
    is_hard_deleted=False,
):
    user = get_user_model().objects.filter(id=user_id).first()
    previous_user = get_current_user()
    if user is not None:
        write_current_user(user)
    try:
        return _process(
            event=event,
            transaction_ref=transaction_ref,
            transaction_version=transaction_version,
            user=user,
            transaction_data=transaction_data,
            is_hard_deleted=is_hard_deleted,
        )
    finally:
        if previous_user is not None:
            write_current_user(previous_user)
        else:
            delete_current_user()


def _process(
    *,
    event,
    transaction_ref,
    transaction_version,
    user,
    transaction_data,
    is_hard_deleted,
):
    flag = _EVENT_RULE_FLAG[event]
    rules = [
        rule
        for rule in TransactionRule.objects.filter(active=True).order_by(
            "order", "id"
        )
        if getattr(rule, flag)
    ]

    results = []
    with transaction.atomic():
        trigger, trigger_data, stale_reason = _resolve_state(
            event=event,
            transaction_ref=transaction_ref,
            transaction_version=transaction_version,
            transaction_data=transaction_data,
            is_hard_deleted=is_hard_deleted,
        )

        for rule in rules:
            execution = _claim(
                event=event,
                transaction_ref=transaction_ref,
                transaction_version=transaction_version,
                rule=rule,
                trigger=trigger,
                user=user,
            )
            if isinstance(execution, RuleExecution) and execution.is_terminal:
                results.append(_result_payload(execution))
                continue

            if stale_reason is not None:
                execution.status = RuleExecution.Status.STALE
                execution.detail = {"reason": stale_reason}
                execution.save()
                logger.info(
                    "Event %s tx=%s@%s rule=%s@%s judged stale: %s",
                    event,
                    transaction_ref,
                    transaction_version,
                    rule.id,
                    rule.version,
                    stale_reason,
                )
                results.append(_result_payload(execution))
                continue

            try:
                plan = build_plan(
                    rule=rule,
                    transaction=trigger,
                    trigger_data=trigger_data,
                    event=event,
                    transaction_version=transaction_version,
                    rule_version=rule.version,
                )
                if not plan.triggered:
                    execution.status = RuleExecution.Status.SKIPPED
                    execution.detail = {"reason": "trigger_did_not_match"}
                    execution.save()
                else:
                    execute_plan(
                        execution=execution,
                        plan=plan,
                        rule=rule,
                        trigger=trigger,
                    )
            except RetryableEventError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep other rules going
                logger.exception("Rule %s failed", rule.id)
                execution.status = RuleExecution.Status.FAILED
                execution.detail = {
                    "error": str(exc),
                    "rule_ref": rule.id,
                    "rule_version": rule.version,
                }
                execution.save()

            results.append(_result_payload(execution))

    return results


# -- state resolution -------------------------------------------------------


def _resolve_state(
    *,
    event,
    transaction_ref,
    transaction_version,
    transaction_data,
    is_hard_deleted,
):
    """Return (trigger_instance, trigger_snapshot, stale_reason)."""
    if event == RuleExecution.Event.DELETED:
        # The trigger is the serialized snapshot captured at delete time.
        if is_hard_deleted:
            return None, transaction_data, None
        rows = _lock_rows(transaction_ref)
        if not rows:
            return None, transaction_data, "row_missing"
        return None, transaction_data, None

    rows = _lock_rows(transaction_ref)
    if not rows:
        return None, None, "row_missing"

    row = rows[0]

    if (
        RuleExecution.objects.filter(
            transaction_ref=transaction_ref,
            event=event,
            transaction_version__gt=transaction_version,
        )
        .exclude(status=RuleExecution.Status.STALE)
        .exists()
    ):
        return None, None, "out_of_order"

    if row.deleted:
        return None, None, "soft_deleted"

    if row.version < transaction_version:
        # The event claims a version newer than what we can see: the row is
        # not visible yet (replication lag / deferred event), retry later.
        raise RetryableEventError(
            f"transaction {transaction_ref} version {transaction_version} "
            f"is not visible yet (current version {row.version})"
        )

    if row.version > transaction_version:
        return None, None, "newer_version"

    return row, None, None


def _lock_rows(transaction_ref):
    try:
        # The userless manager has no visibility joins (no DISTINCT), which
        # FOR UPDATE requires; the lock is only synchronization, visibility
        # is enforced by the planner/executor queries afterwards.
        return list(
            Transaction.userless_all_objects.select_for_update(nowait=True).filter(
                id=transaction_ref
            )
        )
    except OperationalError as exc:
        if _is_lock_conflict(exc):
            raise RetryableEventError(
                f"transaction {transaction_ref} is locked by another worker"
            ) from exc
        raise


# -- execution claiming -----------------------------------------------------


def _claim(
    *, event, transaction_ref, transaction_version, rule, trigger, user
):
    """Insert the execution row; on conflict return the existing execution."""
    try:
        with transaction.atomic():
            return RuleExecution.objects.create(
                transaction_ref=transaction_ref,
                rule_ref=rule.id,
                transaction=trigger,
                rule=rule,
                event=event,
                transaction_version=transaction_version,
                rule_version=rule.version,
                status=RuleExecution.Status.PENDING,
                created_by=user,
            )
    except IntegrityError:
        existing = RuleExecution.objects.get(
            transaction_ref=transaction_ref,
            rule_ref=rule.id,
            event=event,
            transaction_version=transaction_version,
            rule_version=rule.version,
        )
        if existing.status == RuleExecution.Status.PENDING:
            raise RetryableEventError(
                f"execution for rule {rule.id} is already in progress"
            )
        return existing


def _result_payload(execution):
    return {
        "id": execution.id,
        "status": execution.status,
        "rule_ref": execution.rule_ref,
        "rule_version": execution.rule_version,
        "detail": execution.detail,
        "summary": execution.summary,
    }
