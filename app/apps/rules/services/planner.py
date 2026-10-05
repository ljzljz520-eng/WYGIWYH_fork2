"""Read-only rule planner.

The planner turns (rule, triggering transaction version, event, frozen
context) into a :class:`Plan` of in-memory effects without writing the
database. The worker executor and the rule UI dry-run/commit endpoints all
consume plans produced here.
"""

from collections import namedtuple
from datetime import date
from decimal import Decimal
from itertools import chain

from simpleeval import EvalWithCompoundTypes

from apps.accounts.models import Account
from apps.rules.models import (
    RuleExecution,
    TransactionRule,
    TransactionRuleAction,
    UpdateOrCreateTransactionRuleAction,
)
from apps.rules.services.exceptions import PlannedActionError
from apps.rules.services.evaluation import FrozenEvalContext
from apps.rules.services.names import (
    build_names_from_dict,
    build_names_from_planned,
)
from apps.transactions.models import (
    Transaction,
    TransactionCategory,
    TransactionEntity,
    TransactionTag,
)

# Execution instruction kinds
SetInstruction = namedtuple("SetInstruction", ["kind", "field", "value"])

_ACTION_PLANNED = "planned"
_ACTION_WILL_FAIL = "will_fail"

# Map edit action field name -> snapshot key for old/new effect values
_EFFECT_SNAPSHOT_KEY = {
    "account": "account_id",
    "category": "category_id",
    "tags": "tag_ids",
    "entities": "entity_ids",
}


class PlannedTransaction:
    """An in-memory transaction used during planning."""

    def __init__(self, instance, existing, tag_pairs, entity_pairs):
        self.instance = instance
        self.existing = existing
        # Ordered [(id, name)] representing replacement m2m assignments
        self.tag_pairs = list(tag_pairs)
        self.entity_pairs = list(entity_pairs)


# -- in-memory construction -------------------------------------------------


def clone_transaction(source: Transaction) -> Transaction:
    """Detached in-memory copy preserving the primary key; never saves."""
    clone = Transaction()
    for field in Transaction._meta.concrete_fields:
        setattr(clone, field.attname, getattr(source, field.attname))
    return clone


def planned_from_instance(instance: Transaction) -> PlannedTransaction:
    clone = clone_transaction(instance)
    tag_pairs = [(tag.id, tag.name) for tag in instance.tags.all()]
    entity_pairs = [(entity.id, entity.name) for entity in instance.entities.all()]
    return PlannedTransaction(clone, existing=True, tag_pairs=tag_pairs,
                              entity_pairs=entity_pairs)


def planned_from_dict(data: dict) -> PlannedTransaction:
    """Reconstruct a PlannedTransaction from a serialized transaction."""
    instance = Transaction(
        id=data.get("id"),
        account_id=data.get("account", (None, None))[0],
        type=data.get("type"),
        is_paid=data.get("is_paid"),
        amount=Decimal(data.get("amount")),
        date=date.fromisoformat(data.get("date")),
        reference_date=(
            date.fromisoformat(data.get("reference_date"))
            if data.get("reference_date")
            else None
        ),
        description=data.get("description", ""),
        notes=data.get("notes", ""),
        internal_note=data.get("internal_note", ""),
        internal_id=data.get("internal_id", ""),
        mute=data.get("mute", False),
        deleted=data.get("deleted", True),
    )
    category = data.get("category", (None, None))[0]
    if category:
        instance.category_id = category
    tag_pairs = list(data.get("tags", []))
    entity_pairs = list(data.get("entities", []))
    return PlannedTransaction(
        instance, existing=False, tag_pairs=tag_pairs, entity_pairs=entity_pairs
    )


# -- snapshots & canonical values -------------------------------------------

_SNAPSHOT_KEYS = [
    "date", "reference_date", "type", "is_paid", "amount", "description",
    "notes", "internal_note", "internal_id", "account_id", "account_name",
    "category_id", "category_name", "tag_ids", "tag_names", "entity_ids",
    "entity_names", "mute", "deleted",
]


def snapshot(pt: PlannedTransaction) -> dict:
    instance = pt.instance
    has_id = instance.id is not None
    account = instance.account if has_id else None
    category = instance.category if has_id else None
    return {
        "id": instance.id,
        "date": instance.date.isoformat() if instance.date else None,
        "reference_date": (
            instance.reference_date.isoformat()
            if instance.reference_date
            else None
        ),
        "type": instance.type,
        "is_paid": instance.is_paid,
        "amount": str(instance.amount) if instance.amount is not None else None,
        "description": instance.description,
        "notes": instance.notes,
        "internal_note": instance.internal_note,
        "internal_id": instance.internal_id,
        "account_id": account.id if account else None,
        "account_name": account.name if account else None,
        "category_id": category.id if category else None,
        "category_name": category.name if category else None,
        "tag_ids": [tag_id for tag_id, _ in pt.tag_pairs],
        "tag_names": [tag_name for _, tag_name in pt.tag_pairs],
        "entity_ids": [entity_id for entity_id, _ in pt.entity_pairs],
        "entity_names": [entity_name for _, entity_name in pt.entity_pairs],
        "mute": instance.mute,
        "deleted": instance.deleted,
    }


def canonical(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical(v) for v in value) + "]"
    return str(value)


def snapshot_diffs(before: dict | None, after: dict | None) -> list:
    """Return sorted ``(key, new_value)`` pairs that differ."""
    before = before or {}
    after = after or {}
    diffs = []
    for key in _SNAPSHOT_KEYS:
        if before.get(key) != after.get(key):
            diffs.append((key, after.get(key)))
    return sorted(diffs, key=lambda item: item[0])


# -- plan data structures ---------------------------------------------------


class Effect:
    def __init__(self, kind, action_type, action_ref, order, target_ref=None,
                 field=None, old=None, new=None, before=None, after=None,
                 reason=None):
        self.kind = kind
        self.action_type = action_type
        self.action_ref = action_ref
        self.order = order
        self.target_ref = target_ref
        self.field = field
        self.old = old
        self.new = new
        self.before = before
        self.after = after
        self.reason = reason

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "action_type": self.action_type,
            "action_ref": self.action_ref,
            "order": self.order,
            "target_ref": self.target_ref,
            "field": self.field,
            "old": self.old,
            "new": self.new,
            "before": self.before,
            "after": self.after,
            "reason": self.reason,
        }

    def fingerprints(self) -> set:
        prefix = f"{self.action_type}:{self.action_ref}:{self.order}"
        if self.kind == RuleActionExecutionKind.MODIFY:
            if self.field:
                return {
                    f"{prefix}:modify:{self.target_ref}:{self.field}="
                    f"{canonical(self.new)}"
                }
            return {
                f"{prefix}:modify:{self.target_ref}:{field}={canonical(value)}"
                for field, value in snapshot_diffs(self.before, self.after)
            }
        if self.kind == RuleActionExecutionKind.CREATE:
            return {
                f"{prefix}:create:{field}={canonical(value)}"
                for field, value in snapshot_diffs(None, self.after)
            }
        return {f"{prefix}:reject:{self.reason}"}


# Effect kind constants mirroring RuleActionExecution.EffectKind values
class RuleActionExecutionKind:
    MODIFY = "modify"
    CREATE = "create"
    REJECT = "reject"


class ActionPlan:
    def __init__(self, action_type, action_ref, order, status=_ACTION_PLANNED,
                 error="", target_ref=None, create=False, rejected=False,
                 reject_reason="", sets=None, tag_ids=None, entity_ids=None,
                 effects=None):
        self.action_type = action_type
        self.action_ref = action_ref
        self.order = order
        self.status = status
        self.error = error
        self.target_ref = target_ref
        self.create = create
        self.rejected = rejected
        self.reject_reason = reject_reason
        self.sets = sets or []
        # None = leave m2m untouched; list = replacement
        self.tag_ids = tag_ids
        self.entity_ids = entity_ids
        self.effects = effects or []

    @property
    def will_fail(self):
        return self.status == _ACTION_WILL_FAIL


class Plan:
    def __init__(self, key, triggered, context, trigger_snapshot,
                 action_plans=None):
        self.key = key  # (tx_ref, event, tx_version, rule_ref, rule_version)
        self.triggered = triggered
        self.context = context
        self.trigger_snapshot = trigger_snapshot
        self.action_plans = action_plans or []

    @property
    def status(self):
        return (
            RuleExecution.Status.SKIPPED
            if not self.triggered
            else RuleExecution.Status.PENDING
        )

    def effects(self) -> list:
        return list(
            chain.from_iterable(plan.effects for plan in self.action_plans)
        )

    def grouped_effects(self) -> dict:
        grouped = {
            RuleActionExecutionKind.MODIFY: [],
            RuleActionExecutionKind.CREATE: [],
            RuleActionExecutionKind.REJECT: [],
        }
        for effect in self.effects():
            grouped[effect.kind].append(effect.as_dict())
        return grouped

    def fingerprints(self) -> set:
        result = set()
        for effect in self.effects():
            result |= effect.fingerprints()
        return result

    def context_token(self) -> str:
        return self.context.issue_token()

    def summary(self) -> dict:
        return {
            "fingerprints": sorted(self.fingerprints()),
            "counts": {
                kind: len(items)
                for kind, items in self.grouped_effects().items()
            },
        }


def same_fingerprints(plan_a: Plan, plan_b: Plan) -> bool:
    return plan_a.fingerprints() == plan_b.fingerprints()


# -- planning internals -----------------------------------------------------


def _resolve_account(evaluator, expression, *, first_only=False):
    value = evaluator.eval(expression)
    if isinstance(value, int):
        return Account.objects.get(id=value)
    if first_only:
        return Account.objects.filter(name=value).first()
    return Account.objects.get(name=value)


def _resolve_category(evaluator, expression):
    value = evaluator.eval(expression)
    if value is None:
        return None
    if isinstance(value, int):
        return TransactionCategory.objects.get(id=value)
    return TransactionCategory.objects.get(name=value)


def _resolve_tag_pairs(evaluator, expression):
    value = evaluator.eval(expression)
    raw = value if isinstance(value, (list, tuple)) else [value]
    pairs = []
    for item in raw:
        if isinstance(item, int):
            tag = TransactionTag.objects.get(id=item)
        else:
            tag = TransactionTag.objects.get(name=item)
        pairs.append((tag.id, tag.name))
    return pairs


def _resolve_entity_pairs(evaluator, expression):
    value = evaluator.eval(expression)
    raw = value if isinstance(value, (list, tuple)) else [value]
    pairs = []
    for item in raw:
        if isinstance(item, int):
            entity = TransactionEntity.objects.get(id=item)
        else:
            entity = TransactionEntity.objects.get(name=item)
        pairs.append((entity.id, entity.name))
    return pairs


def _fail_plan(action_type, action_ref, order, error) -> ActionPlan:
    return ActionPlan(
        action_type=action_type,
        action_ref=action_ref,
        order=order,
        status=_ACTION_WILL_FAIL,
        error=str(error),
        effects=[],
    )


def _plan_edit_action(pt, action, evaluator, event) -> ActionPlan:
    field = action.field
    before = snapshot(pt)
    try:
        new_value = evaluator.eval(action.value)
    except Exception as error:  # noqa: BLE001 - expression failure is part of the plan
        return _fail_plan(
            RuleActionExecutionType.EDIT, action.id, action.order, error
        )

    sets = []
    tag_ids = None
    entity_ids = None

    try:
        if field == TransactionRuleAction.Field.account:
            if isinstance(new_value, int):
                account = Account.objects.get(id=new_value)
            else:
                account = Account.objects.filter(name=new_value).first()
            sets.append(SetInstruction("fk", "account", account.id))
        elif field == TransactionRuleAction.Field.category:
            category = None
            if new_value is not None:
                if isinstance(new_value, int):
                    category = TransactionCategory.objects.get(id=new_value)
                else:
                    category = TransactionCategory.objects.get(name=new_value)
            sets.append(
                SetInstruction("fk", "category", category.id if category else None)
            )
        elif field == TransactionRuleAction.Field.tags:
            pairs = _resolve_tag_pairs_from_value(new_value)
            pt.tag_pairs = pairs
            tag_ids = [tag_id for tag_id, _ in pairs]
        elif field == TransactionRuleAction.Field.entities:
            pairs = _resolve_entity_pairs_from_value(new_value)
            pt.entity_pairs = pairs
            entity_ids = [entity_id for entity_id, _ in pairs]
        else:
            sets.append(SetInstruction("scalar", field, new_value))
            setattr(pt.instance, field, new_value)
    except Exception as error:  # noqa: BLE001
        return _fail_plan(
            RuleActionExecutionType.EDIT, action.id, action.order, error
        )

    after = snapshot(pt)
    snapshot_key = _EFFECT_SNAPSHOT_KEY.get(field, field)
    old_value = before.get(snapshot_key)
    new_canonical = after.get(snapshot_key)
    effect = Effect(
        kind=RuleActionExecutionKind.MODIFY,
        action_type=RuleActionExecutionType.EDIT,
        action_ref=action.id,
        order=action.order,
        target_ref=pt.instance.id,
        field=field,
        old=canonical_value_from_snapshot(field, old_value),
        new=canonical_value_from_snapshot(field, new_canonical),
        before=before,
        after=after,
    )
    return ActionPlan(
        action_type=RuleActionExecutionType.EDIT,
        action_ref=action.id,
        order=action.order,
        target_ref=pt.instance.id,
        sets=sets,
        tag_ids=tag_ids,
        entity_ids=entity_ids,
        effects=[effect],
    )


def canonical_value_from_snapshot(field, value):
    return value


def _resolve_tag_pairs_from_value(new_value):
    raw = new_value if isinstance(new_value, list) else [new_value]
    pairs = []
    for item in raw:
        if isinstance(item, int):
            tag = TransactionTag.objects.get(id=item)
        else:
            tag = TransactionTag.objects.get(name=item)
        pairs.append((tag.id, tag.name))
    return pairs


def _resolve_entity_pairs_from_value(new_value):
    raw = new_value if isinstance(new_value, list) else [new_value]
    pairs = []
    for item in raw:
        if isinstance(item, int):
            entity = TransactionEntity.objects.get(id=item)
        else:
            entity = TransactionEntity.objects.get(name=item)
        pairs.append((entity.id, entity.name))
    return pairs


class RuleActionExecutionType:
    EDIT = "edit_transaction"
    UPDATE_OR_CREATE = "update_or_create_transaction"


def _plan_upsert_action(pt_unused, action, evaluator, event) -> ActionPlan:
    action_type = RuleActionExecutionType.UPDATE_OR_CREATE

    search_query = action.build_search_query(evaluator)
    found = None
    if search_query:
        found = (
            Transaction.objects.filter(search_query)
            .order_by("-date", "-id")
            .first()
        )

    if found is not None:
        target = planned_from_instance(found)
        target_ref = found.id
        create = False
    else:
        target = PlannedTransaction(
            Transaction(), existing=False, tag_pairs=[], entity_pairs=[]
        )
        target_ref = None
        create = True

    before = snapshot(target) if not create else None

    # Expose target as my_* variables while processing this action
    evaluator.names.update(
        build_names_from_planned(target, event, prefix="my_")
    )

    try:
        if action.filter:
            if not evaluator.eval(action.filter):
                _clear_prefixed(evaluator, "my_")
                effect = Effect(
                    kind=RuleActionExecutionKind.REJECT,
                    action_type=action_type,
                    action_ref=action.id,
                    order=action.order,
                    target_ref=target_ref,
                    reason="filter_did_not_match",
                )
                return ActionPlan(
                    action_type=action_type,
                    action_ref=action.id,
                    order=action.order,
                    target_ref=target_ref,
                    rejected=True,
                    reject_reason="filter_did_not_match",
                    effects=[effect],
                )

        sets = []
        tag_ids = None
        entity_ids = None

        if action.set_account:
            account = _resolve_account(evaluator, action.set_account)
            sets.append(SetInstruction("fk", "account", account.id))
            target.instance.account_id = account.id
        if action.set_type:
            value = evaluator.eval(action.set_type)
            sets.append(SetInstruction("scalar", "type", value))
            target.instance.type = value
        if action.set_is_paid:
            value = evaluator.eval(action.set_is_paid)
            sets.append(SetInstruction("scalar", "is_paid", value))
            target.instance.is_paid = value
        if action.set_mute:
            # Legacy behaviour: set_mute wrote to is_paid. Kept for compatibility.
            value = evaluator.eval(action.set_mute)
            sets.append(SetInstruction("scalar", "is_paid", value))
            target.instance.is_paid = value
        if action.set_date:
            value = evaluator.eval(action.set_date)
            sets.append(SetInstruction("scalar", "date", value))
            target.instance.date = value
        if action.set_reference_date:
            value = evaluator.eval(action.set_reference_date)
            sets.append(SetInstruction("scalar", "reference_date", value))
            target.instance.reference_date = value
        if action.set_amount:
            value = evaluator.eval(action.set_amount)
            sets.append(SetInstruction("scalar", "amount", value))
            target.instance.amount = value
        if action.set_description:
            value = evaluator.eval(action.set_description)
            sets.append(SetInstruction("scalar", "description", value))
            target.instance.description = value
        if action.set_internal_note:
            value = evaluator.eval(action.set_internal_note)
            sets.append(SetInstruction("scalar", "internal_note", value))
            target.instance.internal_note = value
        if action.set_internal_id:
            value = evaluator.eval(action.set_internal_id)
            sets.append(SetInstruction("scalar", "internal_id", value))
            target.instance.internal_id = value
        if action.set_notes:
            value = evaluator.eval(action.set_notes)
            sets.append(SetInstruction("scalar", "notes", value))
            target.instance.notes = value
        if action.set_category:
            category = _resolve_category(evaluator, action.set_category)
            sets.append(
                SetInstruction(
                    "fk", "category", category.id if category else None
                )
            )
            target.instance.category_id = category.id if category else None
        if action.set_tags:
            pairs = _resolve_tag_pairs(evaluator, action.set_tags)
            target.tag_pairs = pairs
            tag_ids = [tag_id for tag_id, _ in pairs]
        if action.set_entities:
            pairs = _resolve_entity_pairs(evaluator, action.set_entities)
            target.entity_pairs = pairs
            entity_ids = [entity_id for entity_id, _ in pairs]
    except Exception as error:  # noqa: BLE001
        _clear_prefixed(evaluator, "my_")
        return _fail_plan(action_type, action.id, action.order, error)

    _clear_prefixed(evaluator, "my_")

    after = snapshot(target)
    if create:
        kind = RuleActionExecutionKind.CREATE
        effect = Effect(
            kind=kind,
            action_type=action_type,
            action_ref=action.id,
            order=action.order,
            target_ref=None,
            before=None,
            after=after,
        )
    else:
        effect = Effect(
            kind=RuleActionExecutionKind.MODIFY,
            action_type=action_type,
            action_ref=action.id,
            order=action.order,
            target_ref=target_ref,
            before=before,
            after=after,
        )

    return ActionPlan(
        action_type=action_type,
        action_ref=action.id,
        order=action.order,
        target_ref=target_ref,
        create=create,
        sets=sets,
        tag_ids=tag_ids,
        entity_ids=entity_ids,
        effects=[effect],
    )


def _clear_prefixed(evaluator, prefix):
    for key in list(evaluator.names.keys()):
        if key.startswith(prefix):
            del evaluator.names[key]


# -- public entry point -----------------------------------------------------


def build_plan(
    *,
    rule: TransactionRule,
    transaction: Transaction,
    event: str,
    context: FrozenEvalContext | None = None,
    transaction_version: int | None = None,
    rule_version: int | None = None,
    old_data: dict | None = None,
    input_patch: dict | None = None,
    trigger_data: dict | None = None,
) -> Plan:
    """Produce a read-only plan for one rule and one transaction event."""
    context = context or FrozenEvalContext.fresh()
    if trigger_data is not None:
        trigger_pt = planned_from_dict(trigger_data)
        trigger_id = trigger_data.get("id")
    else:
        trigger_pt = planned_from_instance(transaction)
        trigger_id = transaction.id

    if input_patch:
        _apply_input_patch(trigger_pt, input_patch)

    tx_version = (
        transaction_version
        if transaction_version is not None
        else trigger_pt.instance.version
    )
    r_version = rule_version if rule_version is not None else rule.version

    key = (
        trigger_id,
        event,
        tx_version,
        rule.id,
        r_version,
    )
    trigger_snapshot = snapshot(trigger_pt)

    names = build_names_from_planned(trigger_pt, event)
    evaluator = EvalWithCompoundTypes(names=names, functions=context.functions())

    if event == RuleExecution.Event.UPDATED and old_data:
        evaluator.names.update(build_names_from_dict(old_data, event, "old_"))

    if not evaluator.eval(rule.trigger):
        return Plan(
            key=key,
            triggered=False,
            context=context,
            trigger_snapshot=trigger_snapshot,
        )

    action_plans = []

    if event == RuleExecution.Event.DELETED:
        upsert_actions = list(
            rule.update_or_create_transaction_actions.all()
        )
        for action in upsert_actions:
            action_plans.append(
                _plan_upsert_action(None, action, evaluator, event)
            )
    else:
        edit_actions = list(rule.transaction_actions.all())
        upsert_actions = list(
            rule.update_or_create_transaction_actions.all()
        )
        has_custom_order = any(
            action.order > 0 for action in chain(edit_actions, upsert_actions)
        )

        if has_custom_order:
            ordered_actions = sorted(
                chain(edit_actions, upsert_actions),
                key=lambda action: (action.order, action.id),
            )
            for action in ordered_actions:
                if isinstance(action, TransactionRuleAction):
                    plan = _plan_edit_action(
                        trigger_pt, action, evaluator, event
                    )
                    action_plans.append(plan)
                    if rule.sequenced and not plan.will_fail:
                        evaluator.names.update(
                            build_names_from_planned(trigger_pt, event)
                        )
                else:
                    action_plans.append(
                        _plan_upsert_action(None, action, evaluator, event)
                    )
        else:
            for action in edit_actions:
                plan = _plan_edit_action(trigger_pt, action, evaluator, event)
                action_plans.append(plan)
                if rule.sequenced and not plan.will_fail:
                    evaluator.names.update(
                        build_names_from_planned(trigger_pt, event)
                    )
            if rule.sequenced:
                evaluator.names.update(
                    build_names_from_planned(trigger_pt, event)
                )
            for action in upsert_actions:
                action_plans.append(
                    _plan_upsert_action(None, action, evaluator, event)
                )

    return Plan(
        key=key,
        triggered=True,
        context=context,
        trigger_snapshot=trigger_snapshot,
        action_plans=action_plans,
    )


def _apply_input_patch(pt: PlannedTransaction, patch: dict):
    """Apply a dry-run form patch to the planned trigger transaction."""
    for field, value in patch.items():
        if field == "tags":
            pt.tag_pairs = _resolve_tag_pairs_from_value(list(value))
        elif field == "entities":
            pt.entity_pairs = _resolve_entity_pairs_from_value(list(value))
        elif value is not None or field in {"is_paid"}:
            setattr(pt.instance, field, value)


def materialize_input_patch(trigger, patch: dict):
    """Apply an updated-form patch to a real locked trigger instance.

    Scalars are written onto the instance (persisted by the executor's
    final save, without an extra version bump); m2m fields are set
    directly. No signals are sent: the enclosing rule execution is the
    application itself.
    """
    for field, value in patch.items():
        if field == "tags":
            trigger.tags.set(list(value))
        elif field == "entities":
            trigger.entities.set(list(value))
        elif value is not None or field in {"is_paid"}:
            setattr(trigger, field, value)
