import json
from datetime import date
from decimal import Decimal
from itertools import chain

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db import OperationalError, transaction
from django.http import HttpResponse
from django.shortcuts import render, redirect
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods

from apps.common.decorators.htmx import only_htmx
from apps.common.functions.permissions import (
    EDIT,
    READ,
    get_shared_object_or_error,
)
from apps.rules.forms import (
    TransactionRuleForm,
    TransactionRuleActionForm,
    UpdateOrCreateTransactionRuleActionForm,
    DryRunCreatedTransacion,
    DryRunDeletedTransacion,
    DryRunUpdatedTransactionForm,
)
from apps.rules.models import (
    RuleActionExecution,
    RuleExecution,
    TransactionRule,
    TransactionRuleAction,
    UpdateOrCreateTransactionRuleAction,
)
from apps.common.models import SharedObject
from apps.common.forms import SharedObjectForm
from apps.common.decorators.demo import disabled_on_demo
from apps.common.middleware.thread_local import get_current_user
from apps.rules import jobs
from apps.rules.services.evaluation import FrozenEvalContext
from apps.rules.services.exceptions import RetryableEventError
from apps.rules.services.executor import _is_lock_conflict, execute_plan
from apps.rules.services.planner import (
    build_plan,
    materialize_input_patch,
    planned_from_instance,
    snapshot,
)
from apps.rules.services.preview_token import (
    issue_preview_token,
    read_preview_token,
)
from apps.rules.signals import transaction_created, transaction_updated
from apps.rules.utils.transactions import serialize_transaction
from apps.transactions.models import Transaction


@login_required
@disabled_on_demo
@require_http_methods(["GET"])
def rules_index(request):
    return render(
        request,
        "rules/pages/index.html",
    )


@only_htmx
@login_required
@disabled_on_demo
@require_http_methods(["GET"])
def rules_list(request):
    transaction_rules = TransactionRule.objects.all().order_by("order", "id")
    return render(
        request,
        "rules/fragments/list.html",
        {"transaction_rules": transaction_rules},
    )


@only_htmx
@login_required
@disabled_on_demo
@require_http_methods(["GET", "POST"])
def transaction_rule_toggle_activity(request, transaction_rule_id, **kwargs):
    transaction_rule = get_shared_object_or_error(
        TransactionRule, request, id=transaction_rule_id, level=EDIT
    )
    current_active = transaction_rule.active
    transaction_rule.active = not current_active
    transaction_rule.save(update_fields=["active"])

    if current_active:
        messages.success(request, _("Rule deactivated successfully"))
    else:
        messages.success(request, _("Rule activated successfully"))

    return HttpResponse(
        status=204,
        headers={
            "HX-Trigger": "updated, hide_offcanvas",
        },
    )


@only_htmx
@login_required
@disabled_on_demo
@require_http_methods(["GET", "POST"])
def transaction_rule_add(request, **kwargs):
    if request.method == "POST":
        form = TransactionRuleForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, _("Rule added successfully"))

            return HttpResponse(
                status=204,
                headers={
                    "HX-Trigger": "updated, hide_offcanvas",
                },
            )
    else:
        form = TransactionRuleForm()

    return render(
        request,
        "rules/fragments/transaction_rule/add.html",
        {"form": form},
    )


@only_htmx
@login_required
@disabled_on_demo
@require_http_methods(["GET", "POST"])
def transaction_rule_edit(request, transaction_rule_id):
    transaction_rule = get_shared_object_or_error(
        TransactionRule, request, id=transaction_rule_id, level=EDIT
    )

    if request.method == "POST":
        form = TransactionRuleForm(request.POST, instance=transaction_rule)
        if form.is_valid():
            form.save()
            messages.success(request, _("Rule updated successfully"))

            return HttpResponse(
                status=204,
                headers={
                    "HX-Trigger": "updated, hide_offcanvas",
                },
            )
    else:
        form = TransactionRuleForm(instance=transaction_rule)

    return render(
        request,
        "rules/fragments/transaction_rule/edit.html",
        {"form": form, "transaction_rule": transaction_rule},
    )


@only_htmx
@login_required
@disabled_on_demo
@require_http_methods(["GET", "POST"])
def transaction_rule_view(request, transaction_rule_id):
    transaction_rule = get_shared_object_or_error(
        TransactionRule, request, id=transaction_rule_id, level=READ
    )

    edit_actions = transaction_rule.transaction_actions.all()
    update_or_create_actions = (
        transaction_rule.update_or_create_transaction_actions.all()
    )

    all_actions = sorted(
        chain(edit_actions, update_or_create_actions),
        key=lambda a: a.order,
    )

    return render(
        request,
        "rules/fragments/transaction_rule/view.html",
        {"transaction_rule": transaction_rule, "all_actions": all_actions},
    )


@only_htmx
@login_required
@disabled_on_demo
@require_http_methods(["DELETE"])
def transaction_rule_delete(request, transaction_rule_id):
    transaction_rule = get_shared_object_or_error(
        TransactionRule, request, id=transaction_rule_id, level=READ
    )

    if transaction_rule.is_editable_by(request.user):
        transaction_rule.delete()
        messages.success(request, _("Rule deleted successfully"))
    elif transaction_rule.shared_with.filter(pk=request.user.pk).exists():
        # Someone else's rule shared with us: we can drop our own access to it,
        # but never delete it.
        transaction_rule.shared_with.remove(request.user)
        messages.success(request, _("Item no longer shared with you"))
    else:
        raise PermissionDenied

    return HttpResponse(
        status=204,
        headers={
            "HX-Trigger": "updated, hide_offcanvas",
        },
    )


@only_htmx
@login_required
@disabled_on_demo
@require_http_methods(["GET"])
def transaction_rule_take_ownership(request, transaction_rule_id):
    transaction_rule = get_shared_object_or_error(
        TransactionRule, request, id=transaction_rule_id, level=EDIT
    )

    if not transaction_rule.owner:
        transaction_rule.owner = request.user
        transaction_rule.visibility = SharedObject.Visibility.private
        transaction_rule.save()

        messages.success(request, _("Ownership taken successfully"))

    return HttpResponse(
        status=204,
        headers={
            "HX-Trigger": "updated, hide_offcanvas",
        },
    )


@only_htmx
@login_required
@disabled_on_demo
@require_http_methods(["GET", "POST"])
def transaction_rule_share(request, pk):
    obj = get_shared_object_or_error(TransactionRule, request, id=pk, level=EDIT)

    if request.method == "POST":
        form = SharedObjectForm(request.POST, instance=obj, user=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, _("Configuration saved successfully"))

            return HttpResponse(
                status=204,
                headers={
                    "HX-Trigger": "updated, hide_offcanvas",
                },
            )
    else:
        form = SharedObjectForm(instance=obj, user=request.user)

    return render(
        request,
        "rules/fragments/share.html",
        {"form": form, "object": obj},
    )


@only_htmx
@login_required
@disabled_on_demo
@require_http_methods(["GET", "POST"])
def transaction_rule_action_add(request, transaction_rule_id):
    transaction_rule = get_shared_object_or_error(
        TransactionRule, request, id=transaction_rule_id, level=EDIT
    )

    if request.method == "POST":
        form = TransactionRuleActionForm(request.POST, rule=transaction_rule)
        if form.is_valid():
            form.save()

            return HttpResponse(
                status=204,
                headers={
                    "HX-Trigger": "updated, hide_offcanvas",
                },
            )
    else:
        form = TransactionRuleActionForm(rule=transaction_rule)

    return render(
        request,
        "rules/fragments/transaction_rule/transaction_rule_action/add.html",
        {"form": form, "transaction_rule_id": transaction_rule_id},
    )


@only_htmx
@login_required
@disabled_on_demo
@require_http_methods(["GET", "POST"])
def transaction_rule_action_edit(request, transaction_rule_action_id):
    transaction_rule_action = get_shared_object_or_error(
        TransactionRuleAction,
        request,
        id=transaction_rule_action_id,
        level=EDIT,
        via="rule",
    )
    transaction_rule = transaction_rule_action.rule

    if request.method == "POST":
        form = TransactionRuleActionForm(
            request.POST, instance=transaction_rule_action, rule=transaction_rule
        )
        if form.is_valid():
            form.save()
            messages.success(request, _("Action updated successfully"))

            return HttpResponse(
                status=204,
                headers={
                    "HX-Trigger": "updated, hide_offcanvas",
                },
            )
    else:
        form = TransactionRuleActionForm(
            instance=transaction_rule_action, rule=transaction_rule
        )

    return render(
        request,
        "rules/fragments/transaction_rule/transaction_rule_action/edit.html",
        {"form": form, "transaction_rule_action": transaction_rule_action},
    )


@only_htmx
@login_required
@disabled_on_demo
@require_http_methods(["DELETE"])
def transaction_rule_action_delete(request, transaction_rule_action_id):
    transaction_rule_action = get_shared_object_or_error(
        TransactionRuleAction,
        request,
        id=transaction_rule_action_id,
        level=EDIT,
        via="rule",
    )

    transaction_rule_action.delete()

    messages.success(request, _("Action deleted successfully"))

    return HttpResponse(
        status=204,
        headers={
            "HX-Trigger": "updated, hide_offcanvas",
        },
    )


@only_htmx
@login_required
@disabled_on_demo
@require_http_methods(["GET", "POST"])
def update_or_create_transaction_rule_action_add(request, transaction_rule_id):
    transaction_rule = get_shared_object_or_error(
        TransactionRule, request, id=transaction_rule_id, level=EDIT
    )

    if request.method == "POST":
        form = UpdateOrCreateTransactionRuleActionForm(
            request.POST, rule=transaction_rule
        )
        if form.is_valid():
            form.save()
            messages.success(
                request, _("Update or Create Transaction action added successfully")
            )
            return HttpResponse(
                status=204,
                headers={
                    "HX-Trigger": "updated, hide_offcanvas",
                },
            )
    else:
        form = UpdateOrCreateTransactionRuleActionForm(rule=transaction_rule)

    return render(
        request,
        "rules/fragments/transaction_rule/update_or_create_transaction_rule_action/add.html",
        {"form": form, "transaction_rule_id": transaction_rule_id},
    )


@only_htmx
@login_required
@disabled_on_demo
@require_http_methods(["GET", "POST"])
def update_or_create_transaction_rule_action_edit(request, pk):
    linked_action = get_shared_object_or_error(
        UpdateOrCreateTransactionRuleAction, request, id=pk, level=EDIT, via="rule"
    )
    transaction_rule = linked_action.rule

    if request.method == "POST":
        form = UpdateOrCreateTransactionRuleActionForm(
            request.POST, instance=linked_action, rule=transaction_rule
        )
        if form.is_valid():
            form.save()
            messages.success(
                request, _("Update or Create Transaction action updated successfully")
            )
            return HttpResponse(
                status=204,
                headers={
                    "HX-Trigger": "updated, hide_offcanvas",
                },
            )
    else:
        form = UpdateOrCreateTransactionRuleActionForm(
            instance=linked_action, rule=transaction_rule
        )

    return render(
        request,
        "rules/fragments/transaction_rule/update_or_create_transaction_rule_action/edit.html",
        {"form": form, "action": linked_action},
    )


@only_htmx
@login_required
@disabled_on_demo
@require_http_methods(["DELETE"])
def update_or_create_transaction_rule_action_delete(request, pk):
    linked_action = get_shared_object_or_error(
        UpdateOrCreateTransactionRuleAction, request, id=pk, level=EDIT, via="rule"
    )

    linked_action.delete()

    messages.success(
        request, _("Update or Create Transaction action deleted successfully")
    )

    return HttpResponse(
        status=204,
        headers={
            "HX-Trigger": "updated, hide_offcanvas",
        },
    )


# -- rule dry-run ----------------------------------------------------------


def _component_snapshot(snap):
    """Convert a planner snapshot to the serialized shape c-transaction.item expects."""
    if snap is None:
        return None
    return {
        "id": snap.get("id"),
        "account": [snap.get("account_id"), snap.get("account_name")],
        "account_group": [None, None],
        "type": snap.get("type"),
        "is_paid": snap.get("is_paid"),
        "category": [snap.get("category_id"), snap.get("category_name")],
        "date": snap.get("date"),
        "reference_date": snap.get("reference_date"),
        "amount": snap.get("amount"),
        "description": snap.get("description"),
        "notes": snap.get("notes"),
        "tags": [
            [tag_id, tag_name]
            for tag_id, tag_name in zip(
                snap.get("tag_ids", []), snap.get("tag_names", [])
            )
        ],
        "entities": [
            [entity_id, entity_name]
            for entity_id, entity_name in zip(
                snap.get("entity_ids", []), snap.get("entity_names", [])
            )
        ],
        "deleted": snap.get("deleted", False),
        "internal_note": snap.get("internal_note"),
        "internal_id": snap.get("internal_id"),
        "mute": snap.get("mute"),
    }


def _build_groups(plan):
    grouped = plan.grouped_effects()
    groups = {}
    for kind in ("modify", "create", "reject"):
        items = []
        for effect in grouped[kind]:
            items.append(
                {
                    "action_type": effect["action_type"],
                    "action_ref": effect["action_ref"],
                    "order": effect["order"],
                    "field": effect.get("field"),
                    "old": effect.get("old"),
                    "new": effect.get("new"),
                    "reason": effect.get("reason"),
                    "before": _component_snapshot(effect.get("before")),
                    "after": _component_snapshot(effect.get("after")),
                }
            )
        groups[kind] = items
    return groups


def _build_errors(plan):
    return [
        {
            "action_type": ap.action_type,
            "action_ref": ap.action_ref,
            "error": ap.error,
        }
        for ap in plan.action_plans
        if ap.will_fail
    ]


def _build_logs(plan, groups, errors):
    if not plan.triggered:
        return "Trigger did not match: rule skipped"

    lines = ["Trigger matched"]
    for item in groups["modify"]:
        lines.append(
            f"[modify] {item['action_type']}#{item['action_ref']}: "
            f"{item['field']} {item['old']} -> {item['new']}"
        )
    for item in groups["create"]:
        lines.append(
            f"[create] {item['action_type']}#{item['action_ref']}: "
            "new transaction will be created"
        )
    for item in groups["reject"]:
        lines.append(
            f"[reject] {item['action_type']}#{item['action_ref']}: "
            f"{item['reason']}"
        )
    for item in errors:
        lines.append(
            f"[error] {item['action_type']}#{item['action_ref']}: "
            f"{item['error']}"
        )
    return "\n".join(lines)


def _render_dry_run(request, template, context):
    return render(request, template, context)


@only_htmx
@login_required
@disabled_on_demo
@require_http_methods(["GET", "POST"])
def dry_run_rule_created(request, pk):
    rule = get_shared_object_or_error(TransactionRule, request, id=pk, level=EDIT)
    form = DryRunCreatedTransacion(
        request.POST if request.method == "POST" else None
    )

    context = {"form": form, "rule": rule}

    if request.method == "POST" and form.is_valid():
        tx = form.cleaned_data["transaction"]
        eval_context = FrozenEvalContext.fresh()
        plan = build_plan(
            rule=rule,
            transaction=tx,
            event=RuleExecution.Event.CREATED,
            context=eval_context,
        )
        groups = _build_groups(plan)
        errors = _build_errors(plan)
        context.update(
            {
                "groups": groups,
                "errors": errors,
                "logs": _build_logs(plan, groups, errors),
                "token": issue_preview_token(
                    eval_context=eval_context,
                    event=RuleExecution.Event.CREATED,
                    transaction_ref=tx.id,
                    transaction_version=tx.version,
                    rule_ref=rule.id,
                    rule_version=rule.version,
                    fingerprints=sorted(plan.fingerprints()),
                    input_patch={},
                ),
                "fingerprints": sorted(plan.fingerprints()),
                "fingerprints_json": json.dumps(sorted(plan.fingerprints())),
                "can_commit": True,
                "event": RuleExecution.Event.CREATED,
                "rule_version": rule.version,
                "input_patch_json": "{}",
                "transaction_ref": tx.id,
                "transaction_version": tx.version,
            }
        )

    return _render_dry_run(
        request,
        "rules/fragments/transaction_rule/dry_run/created.html",
        context,
    )


@only_htmx
@login_required
@disabled_on_demo
@require_http_methods(["GET", "POST"])
def dry_run_rule_deleted(request, pk):
    rule = get_shared_object_or_error(TransactionRule, request, id=pk, level=EDIT)
    form = DryRunDeletedTransacion(
        request.POST if request.method == "POST" else None
    )

    context = {"form": form, "rule": rule}

    if request.method == "POST" and form.is_valid():
        tx = form.cleaned_data["transaction"]
        trigger_data = serialize_transaction(tx, deleted=True)
        eval_context = FrozenEvalContext.fresh()
        plan = build_plan(
            rule=rule,
            transaction=None,
            trigger_data=trigger_data,
            event=RuleExecution.Event.DELETED,
            context=eval_context,
            transaction_version=tx.version,
        )
        groups = _build_groups(plan)
        errors = _build_errors(plan)
        context.update(
            {
                "groups": groups,
                "errors": errors,
                "logs": _build_logs(plan, groups, errors),
                "token": eval_context.issue_token(),
                "fingerprints": sorted(plan.fingerprints()),
                "transaction_ref": tx.id,
                "transaction_version": tx.version,
            }
        )

    return _render_dry_run(
        request,
        "rules/fragments/transaction_rule/dry_run/deleted.html",
        context,
    )


@only_htmx
@login_required
@disabled_on_demo
@require_http_methods(["GET", "POST"])
def dry_run_rule_updated(request, pk):
    rule = get_shared_object_or_error(TransactionRule, request, id=pk, level=EDIT)
    form = DryRunUpdatedTransactionForm(
        request.POST if request.method == "POST" else None,
        initial=(
            None
            if request.method == "POST"
            else {"is_paid": None, "type": None}
        ),
    )

    context = {"form": form, "rule": rule}

    if request.method == "POST" and form.is_valid():
        base_transaction = Transaction.objects.get(
            id=request.POST.get("transaction")
        )
        old_data = serialize_transaction(base_transaction, deleted=False)
        input_patch = {}
        for field_name, value in form.cleaned_data.items():
            if field_name == "transaction":
                continue
            if value or isinstance(value, bool):
                if field_name in {"tags", "entities"}:
                    input_patch[field_name] = [item.id for item in value]
                elif field_name == "account" or field_name == "category":
                    input_patch[f"{field_name}_id"] = (
                        value.id if value else None
                    )
                else:
                    input_patch[field_name] = value

        eval_context = FrozenEvalContext.fresh()
        plan = build_plan(
            rule=rule,
            transaction=base_transaction,
            event=RuleExecution.Event.UPDATED,
            context=eval_context,
            old_data=old_data,
            input_patch=input_patch,
        )
        groups = _build_groups(plan)
        errors = _build_errors(plan)
        context.update(
            {
                "groups": groups,
                "errors": errors,
                "logs": _build_logs(plan, groups, errors),
                "token": issue_preview_token(
                    eval_context=eval_context,
                    event=RuleExecution.Event.UPDATED,
                    transaction_ref=base_transaction.id,
                    transaction_version=base_transaction.version,
                    rule_ref=rule.id,
                    rule_version=rule.version,
                    fingerprints=sorted(plan.fingerprints()),
                    input_patch=input_patch,
                ),
                "fingerprints": sorted(plan.fingerprints()),
                "fingerprints_json": json.dumps(sorted(plan.fingerprints())),
                "can_commit": True,
                "event": RuleExecution.Event.UPDATED,
                "rule_version": rule.version,
                "input_patch_json": json.dumps(
                    input_patch,
                    default=lambda value: (
                        value.isoformat()
                        if hasattr(value, "isoformat")
                        else str(value)
                    ),
                ),
                "transaction_ref": base_transaction.id,
                "transaction_version": base_transaction.version,
            }
        )

    return _render_dry_run(
        request,
        "rules/fragments/transaction_rule/dry_run/updated.html",
        context,
    )


# -- rule commit ------------------------------------------------------------


@only_htmx
@login_required
@disabled_on_demo
@require_http_methods(["POST"])
def commit_rule_execution(request, pk):
    """Really apply a plan produced by a dry-run, with a signed preview."""
    rule = get_shared_object_or_error(
        TransactionRule, request, id=pk, level=EDIT
    )

    try:
        preview = read_preview_token(request.POST.get("token", ""))
    except signing.BadSignature:
        return _render_commit_error(
            request,
            _("Preview is invalid or expired; run the test again."),
        )

    if preview["rule_ref"] != rule.id:
        return _render_commit_error(
            request,
            _("Preview was issued for another rule; run the test again."),
        )

    if preview["rule_version"] != rule.version:
        return _render_commit_error(
            request,
            _("Rule has changed since the preview; run the test again."),
        )

    event = preview["event"]
    transaction_ref = preview["transaction_ref"]
    transaction_version = preview["transaction_version"]
    rule_version = preview["rule_version"]
    expected_fingerprints = preview["fingerprints"]
    input_patch = preview["input_patch"]
    eval_context = preview["context"]

    try:
        with transaction.atomic():
            try:
                rows = list(
                    Transaction.userless_all_objects.select_for_update(
                        nowait=True
                    ).filter(id=transaction_ref)
                )
            except OperationalError as exc:
                if _is_lock_conflict(exc):
                    return _render_commit_error(
                        request,
                        _("Transaction is locked by another operation; retry shortly."),
                        status=409,
                    )
                raise

            if not rows:
                return _render_commit_error(
                    request,
                    _("Transaction no longer exists."),
                    status=409,
                )

            trigger = rows[0]

            # An existing execution for this exact key always wins, even if
            # the trigger moved to a newer version afterwards.
            execution = jobs._claim(
                event=event,
                transaction_ref=transaction_ref,
                transaction_version=transaction_version,
                rule=rule,
                trigger=trigger,
                user=request.user,
            )

            if execution.is_terminal:
                return _render_commit_result(
                    request, execution=execution, plan=None, reused=True
                )

            def reject_after_claim(message, status, reason):
                execution.status = RuleExecution.Status.STALE
                execution.detail = {"reason": reason}
                execution.save()
                return _render_commit_error(request, message, status=status)

            if trigger.deleted:
                return reject_after_claim(
                    _("Transaction was deleted after the preview."),
                    409,
                    "soft_deleted",
                )

            if trigger.version != transaction_version:
                return reject_after_claim(
                    _("Transaction changed after the preview; run the test again."),
                    409,
                    "newer_version",
                )

            # Base snapshot for old_ variables; capture before materializing
            old_data = serialize_transaction(trigger, deleted=False)

            # Apply the previewed patch onto the real locked trigger; the
            # executor's final save persists it without extra signals.
            materialize_input_patch(trigger, input_patch)

            plan = build_plan(
                rule=rule,
                transaction=trigger,
                event=event,
                context=eval_context,
                transaction_version=transaction_version,
                rule_version=rule_version,
                old_data=old_data,
                input_patch=input_patch,
            )

            if sorted(plan.fingerprints()) != expected_fingerprints:
                return reject_after_claim(
                    _("Plan changed since the preview; run the test again."),
                    400,
                    "plan_changed",
                )

            execute_plan(
                execution=execution,
                plan=plan,
                rule=rule,
                trigger=trigger,
            )

            return _render_commit_result(
                request, execution=execution, plan=plan, reused=False
            )
    except RetryableEventError as exc:
        return _render_commit_error(request, str(exc), status=409)


def _render_commit_error(request, message, status=400):
    return render(
        request,
        "rules/fragments/transaction_rule/dry_run/commit_result.html",
        {"commit_error": message},
        status=status,
    )


def _render_commit_result(request, *, execution, plan, reused):
    groups = _build_groups(plan) if plan is not None else {"modify": [], "create": []}

    record_ids = RuleActionExecution.objects.filter(
        rule_execution=execution
    ).values_list("id", flat=True)
    created_real = [
        {
            "id": item.id,
            "snapshot": _component_snapshot(
                snapshot(planned_from_instance(item))
            ),
        }
        for item in Transaction.objects.filter(
            generated_by_action_execution_id__in=list(record_ids)
        ).order_by("date", "id")
    ]

    return render(
        request,
        "rules/fragments/transaction_rule/dry_run/commit_result.html",
        {
            "execution_id": execution.id,
            "execution_status": execution.status,
            "reused": reused,
            "groups": groups,
            "created_real": created_real,
        },
    )
