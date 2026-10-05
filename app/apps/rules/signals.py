from django.conf import settings
from django.db import transaction as db_transaction
from django.dispatch import receiver

from apps.transactions.models import (
    Transaction,
    transaction_created,
    transaction_updated,
    transaction_deleted,
)
from apps.rules.jobs import process_transaction_event
from apps.common.middleware.thread_local import get_current_user
from apps.rules.utils.transactions import serialize_transaction


def _enqueue(
    *,
    event,
    transaction_ref,
    transaction_version,
    user_id,
    transaction_data=None,
    is_hard_deleted=False,
):
    payload = {
        "event": event,
        "transaction_ref": transaction_ref,
        "transaction_version": transaction_version,
        "user_id": user_id,
        "transaction_data": transaction_data,
        "is_hard_deleted": is_hard_deleted,
    }
    # Runs immediately when there is no active transaction; otherwise after
    # the outer transaction commits (and never if it rolls back).
    db_transaction.on_commit(
        lambda: process_transaction_event.defer(**payload)
    )


@receiver(transaction_created)
@receiver(transaction_updated)
@receiver(transaction_deleted)
def transaction_changed_receiver(sender: Transaction, signal, **kwargs):
    current_user = get_current_user()
    user_id = current_user.id if current_user else None

    if signal is transaction_deleted:
        # Serialize transaction data for processing
        transaction_data = serialize_transaction(sender, deleted=True)

        _enqueue(
            event="deleted",
            transaction_ref=sender.id,
            transaction_version=sender.version,
            user_id=user_id,
            transaction_data=transaction_data,
            is_hard_deleted=kwargs.get(
                "hard_delete", not settings.ENABLE_SOFT_DELETE
            ),
        )
        return

    for dca_entry in sender.dca_expense_entries.all():
        dca_entry.amount_paid = sender.amount
        dca_entry.save()
    for dca_entry in sender.dca_income_entries.all():
        dca_entry.amount_received = sender.amount
        dca_entry.save()

    _enqueue(
        event=(
            "created"
            if signal is transaction_created
            else "updated"
        ),
        transaction_ref=sender.id,
        transaction_version=sender.version,
        user_id=user_id,
    )
