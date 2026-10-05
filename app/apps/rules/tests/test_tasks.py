from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TransactionTestCase

from apps.accounts.models import Account
from apps.common.middleware.thread_local import (
    delete_current_user,
    write_current_user,
)
from apps.currencies.models import Currency
from apps.transactions.models import (
    Transaction,
    transaction_created,
    transaction_deleted,
)


class SignalEnqueueTests(TransactionTestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            email="rules@example.com",
            password="testpass123",
        )
        write_current_user(self.user)
        self.currency = Currency.objects.create(
            code="USD",
            name="US Dollar",
            decimal_places=2,
        )
        self.account = Account.objects.create(
            name="Main Account",
            currency=self.currency,
            owner=self.user,
        )

    def tearDown(self):
        delete_current_user()

    def make_tx(self, **kwargs):
        params = {
            "account": self.account,
            "type": Transaction.Type.EXPENSE,
            "amount": Decimal("10.00"),
            "date": date(2026, 5, 4),
            "reference_date": date(2026, 5, 1),
            "description": "Source",
            "owner": self.user,
        }
        params.update(kwargs)
        return Transaction.objects.create(**params)

    @patch("apps.rules.signals.process_transaction_event.defer")
    def test_job_enqueued_after_commit_with_payload_version(
        self, mock_defer
    ):
        with transaction.atomic():
            tx = self.make_tx()
            transaction_created.send(sender=tx)
            self.assertEqual(mock_defer.call_count, 0)

        self.assertEqual(mock_defer.call_count, 1)
        payload = mock_defer.call_args.kwargs
        self.assertEqual(payload["event"], "created")
        self.assertEqual(payload["transaction_ref"], tx.id)
        self.assertEqual(payload["transaction_version"], tx.version)
        self.assertEqual(payload["user_id"], self.user.id)

    @patch("apps.rules.signals.process_transaction_event.defer")
    def test_no_job_after_rollback(self, mock_defer):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                tx = self.make_tx()
                transaction_created.send(sender=tx)
                raise RuntimeError("boom")

        self.assertEqual(mock_defer.call_count, 0)

    @patch("apps.rules.signals.process_transaction_event.defer")
    def test_soft_deleted_payload_has_version_and_snapshot(
        self, mock_defer
    ):
        tx = self.make_tx()
        version_before = tx.version

        with transaction.atomic():
            # Mimic the soft-delete flow (test settings default to hard delete)
            tx.deleted = True
            tx.save()
            deleted_version = tx.version
            transaction_deleted.send(sender=tx, hard_delete=False)

        self.assertEqual(deleted_version, version_before + 1)
        payload = mock_defer.call_args.kwargs
        self.assertEqual(payload["event"], "deleted")
        self.assertFalse(payload["is_hard_deleted"])
        self.assertEqual(payload["transaction_version"], deleted_version)
        self.assertEqual(payload["transaction_data"]["id"], tx.id)
        self.assertTrue(payload["transaction_data"]["deleted"])

    @patch("apps.rules.signals.process_transaction_event.defer")
    def test_hard_deleted_payload_is_marked_hard(self, mock_defer):
        tx = self.make_tx()

        with transaction.atomic():
            transaction_deleted.send(sender=tx, hard_delete=True)

        payload = mock_defer.call_args.kwargs
        self.assertEqual(payload["event"], "deleted")
        self.assertTrue(payload["is_hard_deleted"])
        self.assertEqual(payload["transaction_version"], tx.version)
        self.assertEqual(payload["transaction_data"]["id"], tx.id)
