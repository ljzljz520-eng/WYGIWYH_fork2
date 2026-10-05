"""Expression variable namespace for rule evaluation.

Single source for the variable contract exposed to rule expressions, ported
from the legacy task's ``_get_names`` helper. Both in-memory planned
transactions and serialized transaction dicts are supported.
"""

from decimal import Decimal
from datetime import datetime

from apps.rules.models import RuleExecution
from apps.transactions.models import Transaction


def event_flags(event: str) -> dict:
    return {
        "is_on_create": event == RuleExecution.Event.CREATED,
        "is_on_delete": event == RuleExecution.Event.DELETED,
        "is_on_update": event == RuleExecution.Event.UPDATED,
    }


def build_names_from_planned(pt, event: str, prefix: str = "") -> dict:
    """Build expression names from a :class:`PlannedTransaction`."""
    instance = pt.instance
    has_id = instance.id is not None

    if has_id:
        account = instance.account
        group = account.group if account else None
        category = instance.category
    else:
        account = group = category = None

    tag_ids = [tag_id for tag_id, _ in pt.tag_pairs]
    tag_names = [tag_name for _, tag_name in pt.tag_pairs]
    entity_ids = [entity_id for entity_id, _ in pt.entity_pairs]
    entity_names = [entity_name for _, entity_name in pt.entity_pairs]

    names = event_flags(event)
    names.update(
        {
            f"{prefix}id": instance.id if has_id else None,
            f"{prefix}account_name": account.name if account else None,
            f"{prefix}account_id": account.id if account else None,
            f"{prefix}account_group_name": group.name if group else None,
            f"{prefix}account_group_id": group.id if group else None,
            f"{prefix}is_asset_account": (
                account.is_asset if account else None
            ),
            f"{prefix}is_archived_account": (
                account.is_archived if account else None
            ),
            f"{prefix}category_name": category.name if category else None,
            f"{prefix}category_id": category.id if category else None,
            f"{prefix}tag_names": tag_names,
            f"{prefix}tag_ids": tag_ids,
            f"{prefix}entities_names": entity_names,
            f"{prefix}entities_ids": entity_ids,
            f"{prefix}is_expense": instance.type == Transaction.Type.EXPENSE,
            f"{prefix}is_income": instance.type == Transaction.Type.INCOME,
            f"{prefix}is_paid": instance.is_paid,
            f"{prefix}description": instance.description,
            f"{prefix}amount": instance.amount or 0,
            f"{prefix}notes": instance.notes,
            f"{prefix}date": instance.date,
            f"{prefix}reference_date": instance.reference_date,
            f"{prefix}internal_note": instance.internal_note,
            f"{prefix}internal_id": instance.internal_id,
            f"{prefix}is_deleted": instance.deleted,
            f"{prefix}is_muted": instance.mute,
            f"{prefix}is_recurring": (
                instance.recurring_transaction is not None
                if has_id
                else False
            ),
            f"{prefix}is_installment": (
                instance.installment_plan is not None if has_id else False
            ),
            f"{prefix}installment_number": (
                instance.installment_id
                if has_id and instance.installment_plan
                else None
            ),
            f"{prefix}installment_total": (
                instance.installment_plan.number_of_installments
                if has_id and instance.installment_plan
                else None
            ),
        }
    )
    return names


def build_names_from_dict(transaction: dict, event: str, prefix: str = ""):
    """Build expression names from a serialized transaction dict."""
    names = event_flags(event)
    names.update(
        {
            f"{prefix}id": transaction.get("id"),
            f"{prefix}account_name": transaction.get(
                "account", (None, None)
            )[1],
            f"{prefix}account_id": transaction.get("account", (None, None))[0],
            f"{prefix}account_group_name": transaction.get(
                "account_group", (None, None)
            )[1],
            f"{prefix}account_group_id": transaction.get(
                "account_group", (None, None)
            )[0],
            f"{prefix}is_asset_account": transaction.get("is_asset"),
            f"{prefix}is_archived_account": transaction.get("is_archived"),
            f"{prefix}category_name": transaction.get(
                "category", (None, None)
            )[1],
            f"{prefix}category_id": transaction.get("category", (None, None))[
                0
            ],
            f"{prefix}tag_names": [x[1] for x in transaction.get("tags", [])],
            f"{prefix}tag_ids": [x[0] for x in transaction.get("tags", [])],
            f"{prefix}entities_names": [
                x[1] for x in transaction.get("entities", [])
            ],
            f"{prefix}entities_ids": [
                x[0] for x in transaction.get("entities", [])
            ],
            f"{prefix}is_expense": transaction.get("type")
            == Transaction.Type.EXPENSE,
            f"{prefix}is_income": transaction.get("type")
            == Transaction.Type.INCOME,
            f"{prefix}is_paid": transaction.get("is_paid"),
            f"{prefix}description": transaction.get("description", ""),
            f"{prefix}amount": Decimal(transaction.get("amount")),
            f"{prefix}notes": transaction.get("notes", ""),
            f"{prefix}date": datetime.fromisoformat(transaction.get("date")),
            f"{prefix}reference_date": datetime.fromisoformat(
                transaction.get("reference_date")
            ),
            f"{prefix}internal_note": transaction.get("internal_note", ""),
            f"{prefix}internal_id": transaction.get("internal_id", ""),
            f"{prefix}is_deleted": transaction.get("deleted", True),
            f"{prefix}is_muted": transaction.get("mute", False),
            f"{prefix}is_recurring": transaction.get(
                "recurring_transaction", False
            ),
            f"{prefix}is_installment": transaction.get("installment", False),
            f"{prefix}installment_number": transaction.get("installment_id"),
            f"{prefix}installment_total": transaction.get(
                "installment_total"
            ),
        }
    )
    return names
