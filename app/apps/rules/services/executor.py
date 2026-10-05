"""Execute a planned rule.

Applies :class:`~apps.rules.services.planner.ActionPlan` items inside
savepoints, persists :class:`~apps.rules.models.RuleActionExecution` rows
(with effects and provenance) and finalizes the :class:`RuleExecution`.

Modes:
- atomic (default): one inner savepoint for the whole rule; failure rolls
  back every write; final statuses are rewritten in the outer transaction.
- isolated: one savepoint per action; a failure rolls back only that
  action and later actions still run.
"""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, transaction

from apps.rules.models import (
    RuleActionExecution,
    TransactionRule,
)
from apps.rules.services.exceptions import (
    PlannedActionError,
    RetryableEventError,
)
from apps.transactions.models import (
    Transaction,
    TransactionEntity,
    TransactionTag,
)


def execute_plan(*, execution, plan, rule, trigger=None):
    if rule.execution_mode == TransactionRule.ExecutionMode.ISOLATED:
        _execute_isolated(execution, plan, trigger)
    else:
        _execute_atomic(execution, plan, trigger)
    return execution


# -- record helpers ---------------------------------------------------------


def _create_record(execution, ap, status, error="", effects=None):
    return RuleActionExecution.objects.create(
        rule_execution=execution,
        action_type=ap.action_type,
        action_ref=ap.action_ref,
        order=ap.order,
        status=status,
        error=error,
        effects=[effect.as_dict() for effect in (effects if effects is not None else ap.effects)],
    )


# -- action application -----------------------------------------------------


def _apply_action(ap, trigger, record):
    if ap.action_type == "edit_transaction":
        _apply_edit(ap, trigger)
    else:
        _apply_upsert(ap, trigger, record)


def _apply_edit(ap, trigger):
    for instruction in ap.sets:
        if instruction.kind == "scalar":
            setattr(trigger, instruction.field, instruction.value)
        else:
            setattr(trigger, f"{instruction.field}_id", instruction.value)

    if ap.tag_ids is not None:
        trigger.tags.clear()
        if ap.tag_ids:
            trigger.tags.add(
                *TransactionTag.objects.filter(id__in=ap.tag_ids)
            )
    if ap.entity_ids is not None:
        trigger.entities.clear()
        if ap.entity_ids:
            trigger.entities.add(
                *TransactionEntity.objects.filter(id__in=ap.entity_ids)
            )


def _apply_upsert(ap, trigger, record):
    try:
        if ap.create:
            owner = (
                trigger.owner
                if trigger is not None
                else record.rule_execution.created_by
            )
            target = Transaction(owner=owner)
        else:
            target = (
                Transaction.objects.filter(id=ap.target_ref)
                .select_for_update(nowait=True)
                .get()
            )

        for instruction in ap.sets:
            if instruction.kind == "scalar":
                setattr(target, instruction.field, instruction.value)
            else:
                setattr(
                    target, f"{instruction.field}_id", instruction.value
                )

        if ap.create:
            target.generated_by_action_execution_id = record.id

        target.save()

        if ap.tag_ids is not None:
            target.tags.clear()
            if ap.tag_ids:
                target.tags.add(
                    *TransactionTag.objects.filter(id__in=ap.tag_ids)
                )
        if ap.entity_ids is not None:
            target.entities.clear()
            if ap.entity_ids:
                target.entities.add(
                    *TransactionEntity.objects.filter(id__in=ap.entity_ids)
                )
    except RetryableEventError:
        raise
    except ValidationError as exc:
        if ap.create and _is_unique_validation_error(exc):
            raise RetryableEventError(str(exc)) from exc
        raise
    except (OperationalError, IntegrityError) as exc:
        if _is_lock_conflict(exc) or (ap.create and isinstance(exc, IntegrityError)):
            raise RetryableEventError(str(exc)) from exc
        raise


def _is_unique_validation_error(exc):
    codes = []

    def collect(error):
        if hasattr(error, "error_list"):
            for item in error.error_list:
                collect(item)
        elif hasattr(error, "message_dict"):
            for items in error.message_dict.values():
                for item in items:
                    collect(item)
        elif isinstance(error, str):
            if "already exists" in error:
                codes.append("unique")
        else:
            codes.append(getattr(error, "code", None))

    collect(exc)
    return any(
        isinstance(code, str) and code.startswith("unique") for code in codes
    )


def _is_lock_conflict(exc):
    try:
        from psycopg.errors import LockNotAvailable
    except ImportError:
        return False

    seen = exc
    while seen is not None:
        if isinstance(seen, LockNotAvailable):
            return True
        seen = getattr(seen, "__cause__", None)
    return False


# -- trigger instance protection for isolated mode --------------------------


def _snapshot_trigger_state(trigger):
    return {
        "fields": {
            field.attname: getattr(trigger, field.attname)
            for field in Transaction._meta.concrete_fields
        },
        "tag_ids": list(trigger.tags.values_list("id", flat=True)),
        "entity_ids": list(trigger.entities.values_list("id", flat=True)),
    }


def _restore_trigger_state(trigger, state):
    for attname, value in state["fields"].items():
        setattr(trigger, attname, value)
    trigger.tags.set(state["tag_ids"])
    trigger.entities.set(state["entity_ids"])


# -- atomic mode ------------------------------------------------------------


def _execute_atomic(execution, plan, trigger):
    failure = None
    failed_ap = None
    current = None
    had_edits = False

    try:
        with transaction.atomic():
            for ap in plan.action_plans:
                current = ap

                if ap.will_fail:
                    raise PlannedActionError(ap.error)

                if ap.rejected:
                    _create_record(
                        execution,
                        ap,
                        RuleActionExecution.Status.SKIPPED,
                    )
                    continue

                record = _create_record(
                    execution,
                    ap,
                    RuleActionExecution.Status.APPLIED,
                )
                _apply_action(ap, trigger, record)
                if ap.action_type == "edit_transaction":
                    had_edits = True

            if trigger is not None:
                if had_edits:
                    trigger.full_clean()
                trigger.save()
    except RetryableEventError:
        raise
    except Exception as exc:  # noqa: BLE001 - any failure rolls the rule back
        failure = exc
        failed_ap = current

    if failure is not None:
        _persist_failure_records(execution, plan, failed_ap, failure)
        execution.status = execution.Status.FAILED
        execution.detail = {
            "failed_action": failed_ap.action_ref,
            "failed_action_type": failed_ap.action_type,
            "error": str(failure),
            "transaction_ref": execution.transaction_ref,
            "transaction_version": execution.transaction_version,
            "rule_ref": execution.rule_ref,
            "rule_version": execution.rule_version,
        }
        execution.summary = {"planned_fingerprints": plan.summary().get("fingerprints", [])}
        execution.save()
        # The savepoint rollback restored the database but not this Python
        # instance: later rules share it, so reload it or they would
        # silently persist the rolled-back edits.
        if trigger is not None:
            trigger.refresh_from_db()
        return

    execution.status = execution.Status.COMPLETED
    execution.summary = plan.summary()
    execution.save()


def _persist_failure_records(execution, plan, failed_ap, error):
    failed_position = next(
        index
        for index, item in enumerate(plan.action_plans)
        if item.action_type == failed_ap.action_type
        and item.action_ref == failed_ap.action_ref
    )

    for position, ap in enumerate(plan.action_plans):
        if position == failed_position:
            _create_record(
                execution,
                ap,
                RuleActionExecution.Status.FAILED,
                error=str(error),
                effects=[],
            )
        elif ap.rejected:
            _create_record(
                execution,
                ap,
                RuleActionExecution.Status.SKIPPED,
            )
        elif position < failed_position:
            _create_record(
                execution,
                ap,
                RuleActionExecution.Status.ROLLED_BACK,
            )
        else:
            _create_record(
                execution,
                ap,
                RuleActionExecution.Status.SKIPPED,
                effects=[],
            )


# -- isolated mode ----------------------------------------------------------


def _execute_isolated(execution, plan, trigger):
    had_edits = False

    for ap in plan.action_plans:
        if ap.will_fail:
            _create_record(
                execution,
                ap,
                RuleActionExecution.Status.FAILED,
                error=ap.error,
                effects=[],
            )
            continue

        if ap.rejected:
            _create_record(
                execution,
                ap,
                RuleActionExecution.Status.SKIPPED,
            )
            continue

        state = (
            _snapshot_trigger_state(trigger) if trigger is not None else None
        )
        try:
            with transaction.atomic():
                record = _create_record(
                    execution,
                    ap,
                    RuleActionExecution.Status.APPLIED,
                )
                _apply_action(ap, trigger, record)
        except RetryableEventError:
            raise
        except Exception as exc:  # noqa: BLE001
            if state is not None:
                _restore_trigger_state(trigger, state)
            _create_record(
                execution,
                ap,
                RuleActionExecution.Status.FAILED,
                error=str(exc),
                effects=[],
            )
            continue

        if ap.action_type == "edit_transaction":
            had_edits = True

    if trigger is not None:
        if had_edits:
            trigger.full_clean()
        trigger.save()

    execution.status = execution.Status.COMPLETED
    execution.summary = plan.summary()
    execution.save()
