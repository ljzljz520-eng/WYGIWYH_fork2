import json
import re
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TransactionTestCase
from django.urls import reverse

from apps.accounts.models import Account
from apps.currencies.models import Currency
from apps.rules.models import (
    RuleExecution,
    TransactionRule,
    UpdateOrCreateTransactionRuleAction,
)
from apps.rules.services.evaluation import FrozenEvalContext
from apps.rules.services.planner import build_plan
from apps.transactions.models import Transaction

HTMX = {"HTTP_HX_REQUEST": "true"}


class CommitViewTestBase(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="commit@example.com", password="testpass123"
        )
        from apps.common.middleware.thread_local import write_current_user

        write_current_user(self.user)
        self.client.force_login(self.user)
        self.currency = Currency.objects.create(
            code="USD", name="US Dollar", decimal_places=2
        )
        self.account = Account.objects.create(
            name="Main", currency=self.currency, owner=self.user
        )

    def tearDown(self):
        from apps.common.middleware.thread_local import delete_current_user

        delete_current_user()

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
        defaults = {"name": "R", "trigger": trigger, "owner": self.user}
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

    def preview_payload(self, *, rule, tx, event, input_patch=None):
        from apps.rules.services.preview_token import issue_preview_token

        eval_context = FrozenEvalContext.fresh()
        plan = build_plan(
            rule=rule,
            transaction=tx,
            event=event,
            context=eval_context,
            old_data=serialize(tx),
            input_patch=input_patch or {},
        )
        fingerprints = sorted(plan.fingerprints())
        token = issue_preview_token(
            eval_context=eval_context,
            event=event,
            transaction_ref=tx.id,
            transaction_version=tx.version,
            rule_ref=rule.id,
            rule_version=rule.version,
            fingerprints=fingerprints,
            input_patch=input_patch or {},
        )
        return {"token": token}, fingerprints


def serialize(tx):
    from apps.rules.utils.transactions import serialize_transaction

    return serialize_transaction(tx, deleted=False)


class CommitViewTests(CommitViewTestBase):
    def test_commit_applies_plan_with_real_ids_and_single_execution(self):
        tx = self.make_tx(description="source")
        rule = self.make_rule()
        self.add_create_action(rule, "committed-derived")
        payload, expected = self.preview_payload(
            rule=rule, tx=tx, event=RuleExecution.Event.CREATED
        )

        with patch(
            "apps.rules.signals.process_transaction_event.defer"
        ) as defer_mock:
            response = self.client.post(
                reverse(
                    "transaction_rule_commit", kwargs={"pk": rule.id}
                ),
                data=payload,
                **HTMX,
            )

        self.assertEqual(
            response.status_code, 200, response.content.decode()
        )
        body = response.content.decode()
        self.assertIn("Applied successfully", body)
        self.assertIn("committed-derived", body)
        self.assertIn("Created", body)

        derived = Transaction.objects.get(description="committed-derived")
        self.assertIn(str(derived.id), body)

        self.assertEqual(
            RuleExecution.objects.filter(
                transaction_ref=tx.id
            ).count(),
            1,
        )
        execution = RuleExecution.objects.get(transaction_ref=tx.id)
        self.assertEqual(execution.status, RuleExecution.Status.COMPLETED)
        self.assertEqual(
            execution.summary["fingerprints"], expected
        )

        # Commit produced no queued follow-up events
        defer_mock.assert_not_called()
        self.assertEqual(
            Transaction.objects.filter(
                generated_by_action_execution_id__isnull=False
            ).count(),
            1,
        )

    def test_commit_after_real_preview_endpoint_matches_preview(self):
        tx = self.make_tx(description="source")
        rule = self.make_rule()
        self.add_create_action(rule, "e2e-derived")

        preview = self.client.post(
            reverse(
                "transaction_rule_dry_run_created", kwargs={"pk": rule.id}
            ),
            data={"transaction": tx.id},
            **HTMX,
        )
        self.assertEqual(preview.status_code, 200)
        html = preview.content.decode()

        match = re.search(
            r'name="token"[^>]*value=(["\'])(.*?)\1', html
        )
        self.assertIsNotNone(match)

        response = self.client.post(
            reverse("transaction_rule_commit", kwargs={"pk": rule.id}),
            data={"token": match.group(2)},
            **HTMX,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("e2e-derived", response.content.decode())
        self.assertEqual(
            Transaction.objects.filter(description="e2e-derived").count(), 1
        )

    def test_commit_is_idempotent_when_execution_already_exists(self):
        tx = self.make_tx(description="source")
        rule = self.make_rule()
        self.add_create_action(rule, "once-derived")
        payload, _ = self.preview_payload(
            rule=rule, tx=tx, event=RuleExecution.Event.CREATED
        )

        first = self.client.post(
            reverse("transaction_rule_commit", kwargs={"pk": rule.id}),
            data=payload,
            **HTMX,
        )
        self.assertEqual(first.status_code, 200)

        second = self.client.post(
            reverse("transaction_rule_commit", kwargs={"pk": rule.id}),
            data=payload,
            **HTMX,
        )
        self.assertEqual(second.status_code, 200)
        self.assertIn("already applied", second.content.decode())

        self.assertEqual(
            RuleExecution.objects.filter(transaction_ref=tx.id).count(), 1
        )
        self.assertEqual(
            Transaction.objects.filter(description="once-derived").count(), 1
        )

    def test_commit_rejects_transaction_version_drift_without_side_effects(self):
        tx = self.make_tx(description="source")
        rule = self.make_rule()
        self.add_create_action(rule, "drift-derived")
        payload, _ = self.preview_payload(
            rule=rule, tx=tx, event=RuleExecution.Event.CREATED
        )

        tx.description = "changed"
        tx.save()

        response = self.client.post(
            reverse("transaction_rule_commit", kwargs={"pk": rule.id}),
            data=payload,
            **HTMX,
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("changed after the preview", response.content.decode())
        self.assertFalse(
            Transaction.objects.filter(description="drift-derived").exists()
        )
        stale = RuleExecution.objects.get(transaction_ref=tx.id)
        self.assertEqual(stale.status, RuleExecution.Status.STALE)
        self.assertEqual(stale.detail["reason"], "newer_version")

    def test_commit_rejects_rule_version_drift(self):
        tx = self.make_tx(description="source")
        rule = self.make_rule()
        self.add_create_action(rule, "rule-drift-derived")
        payload, _ = self.preview_payload(
            rule=rule, tx=tx, event=RuleExecution.Event.CREATED
        )

        rule.name = "Changed rule"
        rule.save()

        response = self.client.post(
            reverse("transaction_rule_commit", kwargs={"pk": rule.id}),
            data=payload,
            **HTMX,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Rule has changed", response.content.decode())
        self.assertFalse(
            Transaction.objects.filter(
                description="rule-drift-derived"
            ).exists()
        )

    def test_commit_rejects_bad_token(self):
        tx = self.make_tx(description="source")
        rule = self.make_rule()
        self.add_create_action(rule, "bad-token-derived")
        payload, _ = self.preview_payload(
            rule=rule, tx=tx, event=RuleExecution.Event.CREATED
        )
        payload["token"] = "tampered:value"

        response = self.client.post(
            reverse("transaction_rule_commit", kwargs={"pk": rule.id}),
            data=payload,
            **HTMX,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("invalid or expired", response.content.decode())
        self.assertFalse(
            Transaction.objects.filter(
                description="bad-token-derived"
            ).exists()
        )

    def test_updated_commit_applies_patch_and_derived_reflects_it(self):
        tx = self.make_tx(amount="10.00", description="source")
        rule = self.make_rule(on_create=False, on_update=True)
        # Derived transaction amount equals the (patched) trigger amount
        UpdateOrCreateTransactionRuleAction.objects.create(
            rule=rule,
            search_description="'no-such-description-xyz'",
            set_type="'EX'",
            set_date="date",
            set_reference_date="reference_date",
            set_account="account_id",
            set_amount="amount",
            set_description="'updated-commit-derived'",
        )

        # Preview through the updated endpoint with an amount patch
        preview = self.client.post(
            reverse(
                "transaction_rule_dry_run_updated", kwargs={"pk": rule.id}
            ),
            data={"transaction": tx.id, "amount": "12.00"},
            **HTMX,
        )
        self.assertEqual(preview.status_code, 200)
        match = re.search(
            r'name="token"[^>]*value=(["\'])(.*?)\1',
            preview.content.decode(),
        )
        self.assertIsNotNone(match)

        with patch(
            "apps.rules.signals.process_transaction_event.defer"
        ) as defer_mock:
            response = self.client.post(
                reverse(
                    "transaction_rule_commit", kwargs={"pk": rule.id}
                ),
                data={"token": match.group(2)},
                **HTMX,
            )

        self.assertEqual(response.status_code, 200)

        # Patch persisted on the trigger
        tx.refresh_from_db()
        self.assertEqual(tx.amount, Decimal("12.00"))

        # Derived created from the patched state
        derived = Transaction.objects.get(
            description="updated-commit-derived"
        )
        self.assertEqual(derived.amount, Decimal("12.00"))

        # Exactly one execution; no queued follow-up events
        self.assertEqual(
            RuleExecution.objects.filter(transaction_ref=tx.id).count(), 1
        )
        defer_mock.assert_not_called()
