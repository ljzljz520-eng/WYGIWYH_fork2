from datetime import date
from decimal import Decimal

from django.test import TransactionTestCase

from apps.accounts.models import Account
from apps.currencies.models import Currency
from apps.common.middleware.thread_local import write_current_user, delete_current_user
from apps.transactions.models import Transaction


class TransactionVersionTests(TransactionTestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user(
            email="versioning@example.com", password="testpass123"
        )
        write_current_user(self.user)
        self.currency = Currency.objects.create(
            code="USD", name="US Dollar", decimal_places=2
        )
        self.account = Account.objects.create(
            name="Main Account", currency=self.currency, owner=self.user
        )

    def tearDown(self):
        delete_current_user()

    def _create(self, **kwargs):
        defaults = {
            "account": self.account,
            "type": Transaction.Type.EXPENSE,
            "amount": Decimal("10.00"),
            "date": date(2026, 5, 4),
            "reference_date": date(2026, 5, 1),
            "owner": self.user,
        }
        defaults.update(kwargs)
        return Transaction.objects.create(**defaults)

    def test_new_transaction_starts_at_version_1(self):
        transaction = self._create()
        self.assertEqual(transaction.version, 1)

    def test_plain_saves_bump_version_monotonically(self):
        transaction = self._create()
        transaction.description = "first edit"
        transaction.save()
        transaction.description = "second edit"
        transaction.save()

        transaction.refresh_from_db()
        self.assertEqual(transaction.version, 3)

    def test_update_fields_bump_and_persist_version(self):
        transaction = self._create()
        transaction.description = "updated via update_fields"
        transaction.save(update_fields=["description"])

        transaction.refresh_from_db()
        self.assertEqual(transaction.description, "updated via update_fields")
        self.assertEqual(transaction.version, 2)

    def test_queryset_bulk_update_bumps_version(self):
        transaction = self._create()
        transaction.description = "bulk edited"
        Transaction.objects.bulk_update([transaction], ["description"])

        transaction.refresh_from_db()
        self.assertEqual(transaction.description, "bulk edited")
        self.assertEqual(transaction.version, 2)

    def test_queryset_update_bumps_version(self):
        transaction = self._create()
        Transaction.objects.filter(id=transaction.id).update(
            description="qs update"
        )

        transaction.refresh_from_db()
        self.assertEqual(transaction.description, "qs update")
        self.assertEqual(transaction.version, 2)
