from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TransactionTestCase
from django.urls import reverse

from apps.accounts.models import Account
from apps.currencies.models import Currency
from apps.rules.models import (
    TransactionRule,
    UpdateOrCreateTransactionRuleAction,
)
from apps.transactions.models import Transaction

HTMX = {"HTTP_HX_REQUEST": "true"}


class DryRunViewTestBase(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="dryrun@example.com", password="testpass123"
        )
        self.client.force_login(self.user)
        self.currency = Currency.objects.create(
            code="USD", name="US Dollar", decimal_places=2
        )
        self.account = Account.objects.create(
            name="Main", currency=self.currency, owner=self.user
        )

    def make_tx(self, amount="10.00", description="", **kwargs):
        defaults = {
            "account": self.account,
            "type": Transaction.Type.EXPENSE,
            "amount": Decimal(amount),
            "date": date(2026, 5, 4),
            "reference_date": date(2026, 5, 1),
            "description": description,
            "owner": self.user,
        }
        defaults.update(kwargs)
        return Transaction.objects.create(**defaults)

    def make_rule(self, trigger="is_expense", **kwargs):
        defaults = {
            "name": "R",
            "trigger": trigger,
            "owner": self.user,
        }
        defaults.update(kwargs)
        return TransactionRule.objects.create(**defaults)

    def add_create_action(self, rule, description, amount="5.00"):
        return UpdateOrCreateTransactionRuleAction.objects.create(
            rule=rule,
            search_description="'no-such-description-xyz'",
            set_type="'EX'",
            set_date="date",
            set_reference_date="reference_date",
            set_account="account_id",
            set_amount=f"decimal('{amount}')",
            set_description=repr(description),
        )


class DryRunCreatedViewTests(DryRunViewTestBase):
    def test_preview_does_not_write_and_shows_groups_and_token(self):
        tx = self.make_tx(description="source")
        rule = self.make_rule()
        self.add_create_action(rule, "preview-derived")

        count_before = Transaction.objects.count()
        version_before = tx.version

        response = self.client.post(
            reverse(
                "transaction_rule_dry_run_created", kwargs={"pk": rule.id}
            ),
            data={"transaction": tx.id},
            **HTMX,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Transaction.objects.count(), count_before)
        tx.refresh_from_db()
        self.assertEqual(tx.version, version_before)
        self.assertFalse(
            Transaction.objects.filter(
                description="preview-derived"
            ).exists()
        )

        body = response.content.decode()
        self.assertIn("Will create", body)
        self.assertIn("preview-derived", body)
        self.assertIn("Apply / Commit", body)
        self.assertIn('name="token"', body)

    def test_preview_shows_skip_when_trigger_does_not_match(self):
        tx = self.make_tx(description="source")
        rule = self.make_rule(trigger="is_income")
        self.add_create_action(rule, "never")

        response = self.client.post(
            reverse(
                "transaction_rule_dry_run_created", kwargs={"pk": rule.id}
            ),
            data={"transaction": tx.id},
            **HTMX,
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("would be skipped", body)
        self.assertFalse(
            Transaction.objects.filter(description="never").exists()
        )


class DryRunUpdatedViewTests(DryRunViewTestBase):
    def test_preview_uses_patch_and_shows_result(self):
        tx = self.make_tx(amount="10.00", description="source")
        rule = self.make_rule(on_create=False, on_update=True)
        self.add_create_action(rule, "updated-derived", amount="7.00")

        count_before = Transaction.objects.count()
        version_before = tx.version

        response = self.client.post(
            reverse(
                "transaction_rule_dry_run_updated", kwargs={"pk": rule.id}
            ),
            data={
                "transaction": tx.id,
                "amount": "12.00",
            },
            **HTMX,
        )

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("updated-derived", body)
        # Patched amount is visible in the trigger snapshot context
        self.assertIn("12.00", body)

        # Nothing was actually written
        self.assertEqual(Transaction.objects.count(), count_before)
        tx.refresh_from_db()
        self.assertEqual(tx.version, version_before)
        self.assertEqual(tx.amount, Decimal("10.00"))
