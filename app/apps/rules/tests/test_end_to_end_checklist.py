"""End-to-end checklist regression for rule execution (AC-1..AC-4)."""

from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TransactionTestCase, override_settings

from apps.accounts.models import Account
from apps.currencies.models import Currency
from apps.rules.models import (
    RuleActionExecution,
    RuleExecution,
    TransactionRule,
    TransactionRuleAction,
    UpdateOrCreateTransactionRuleAction,
)
from apps.rules.services.exceptions import RetryableEventError
from apps.transactions.models import Transaction


class E2ETestBase(TransactionTestCase):
    def setUp(self):
        from apps.common.middleware.thread_local import write_current_user

        self.user = get_user_model().objects.create_user(
            email="e2e@example.com", password="testpass123"
        )
        write_current_user(self.user)
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

    def run_event(self, *, event, tx, **kwargs):
        from apps.rules.jobs import process_transaction_event

        func = process_transaction_event.func.__wrapped__
        return func(
            event=event,
            transaction_ref=tx.id,
            transaction_version=kwargs.get(
                "transaction_version", tx.version
            ),
            user_id=self.user.id,
            transaction_data=kwargs.get("transaction_data"),
            is_hard_deleted=kwargs.get("is_hard_deleted", False),
        )


class AC1RollbackTests(E2ETestBase):
    def test_second_action_failure_rolls_everything_back(self):
        tx = self.make_tx(description="source")
        rule = self.make_rule()

        # 1) edit source transaction
        edit = TransactionRuleAction.objects.create(
            rule=rule,
            order=10,
            field=TransactionRuleAction.Field.description,
            value="'edited-source'",
        )
        # 2) create a derived transaction
        create = UpdateOrCreateTransactionRuleAction.objects.create(
            rule=rule,
            order=20,
            search_description="'no-such-description-xyz'",
            set_type="'EX'",
            set_date="date",
            set_reference_date="reference_date",
            set_account="account_id",
            set_amount="decimal('5.00')",
            set_description="'derived-should-vanish'",
        )
        # 3) an action that fails at plan time (unknown account)
        failing = UpdateOrCreateTransactionRuleAction.objects.create(
            rule=rule,
            order=30,
            search_description="'also-missing-xyz'",
            set_type="'EX'",
            set_date="date",
            set_reference_date="reference_date",
            set_account="999999",
            set_amount="decimal('9.00')",
            set_description="'never-created'",
        )

        self.run_event(event=RuleExecution.Event.CREATED, tx=tx)

        # Source transaction untouched
        tx.refresh_from_db()
        self.assertEqual(tx.description, "source")
        self.assertEqual(tx.version, 1)

        # Derived transaction rolled back
        self.assertFalse(
            Transaction.objects.filter(
                description="derived-should-vanish"
            ).exists()
        )
        self.assertEqual(Transaction.objects.count(), 1)

        execution = RuleExecution.objects.get(
            transaction_ref=tx.id, event=RuleExecution.Event.CREATED
        )
        self.assertEqual(execution.status, RuleExecution.Status.FAILED)
        self.assertEqual(execution.detail["failed_action"], failing.id)
        self.assertEqual(
            execution.detail["failed_action_type"],
            "update_or_create_transaction",
        )
        self.assertEqual(
            execution.detail["transaction_version"], tx.version
        )
        self.assertEqual(execution.detail["rule_version"], rule.version)

        records = {
            (item.action_type, item.action_ref): item.status
            for item in RuleActionExecution.objects.filter(
                rule_execution=execution
            )
        }
        self.assertEqual(
            records[("edit_transaction", edit.id)],
            RuleActionExecution.Status.ROLLED_BACK,
        )
        self.assertEqual(
            records[
                ("update_or_create_transaction", create.id)
            ],
            RuleActionExecution.Status.ROLLED_BACK,
        )
        failed_record = records[
            ("update_or_create_transaction", failing.id)
        ]
        self.assertEqual(
            failed_record, RuleActionExecution.Status.FAILED
        )


class AC2DuplicateDeliveryTests(E2ETestBase):
    def test_same_event_delivered_twice_runs_once(self):
        tx = self.make_tx(description="source")
        rule = self.make_rule()
        UpdateOrCreateTransactionRuleAction.objects.create(
            rule=rule,
            search_description="'no-such-description-xyz'",
            set_type="'EX'",
            set_date="date",
            set_reference_date="reference_date",
            set_account="account_id",
            set_amount="decimal('5.00')",
            set_description="'derived-once'",
        )

        first = self.run_event(
            event=RuleExecution.Event.CREATED, tx=tx
        )
        second = self.run_event(
            event=RuleExecution.Event.CREATED, tx=tx
        )

        self.assertEqual(first[0]["status"], RuleExecution.Status.COMPLETED)
        self.assertEqual(second[0]["status"], RuleExecution.Status.COMPLETED)
        self.assertEqual(first[0]["id"], second[0]["id"])

        self.assertEqual(
            RuleExecution.objects.filter(
                transaction_ref=tx.id
            ).count(),
            1,
        )
        self.assertEqual(
            Transaction.objects.filter(
                description="derived-once"
            ).count(),
            1,
        )


class AC3OutOfOrderTests(E2ETestBase):
    def test_older_version_does_not_overwrite_newer_result(self):
        # Pre-existing target of the upsert (modify path)
        target = self.make_tx(amount="1.00", description="target-derived")
        source = self.make_tx(amount="10.00", description="source")

        rule = self.make_rule()
        UpdateOrCreateTransactionRuleAction.objects.create(
            rule=rule,
            search_description="'target-derived'",
            set_type="'EX'",
            set_date="date",
            set_reference_date="reference_date",
            set_account="account_id",
            set_amount="amount * 2",
            set_description="'target-derived'",
        )

        # Source advances to v2 before any event is processed
        source.amount = Decimal("20.00")
        source.save()

        # v2 event arrives first -> derived amount 40
        self.run_event(
            event=RuleExecution.Event.CREATED,
            tx=source,
            transaction_version=2,
        )
        target.refresh_from_db()
        self.assertEqual(target.amount, Decimal("40.00"))

        # Late v1 event must be stale and not overwrite
        results = self.run_event(
            event=RuleExecution.Event.CREATED,
            tx=source,
            transaction_version=1,
        )
        self.assertEqual(results[0]["status"], RuleExecution.Status.STALE)
        self.assertEqual(results[0]["detail"]["reason"], "out_of_order")

        target.refresh_from_db()
        self.assertEqual(target.amount, Decimal("40.00"))


class MultiRuleRollbackTests(E2ETestBase):
    """A failed rule must not leak its rolled-back edits to later rules."""

    def test_later_rule_does_not_revive_failed_rule_edits(self):
        tx = self.make_tx(description="source")

        # Rule A: edit trigger, then a failing action -> atomic rollback
        rule_a = self.make_rule(name="A", order=1)
        TransactionRuleAction.objects.create(
            rule=rule_a,
            field=TransactionRuleAction.Field.description,
            value="'A-edit'",
        )
        UpdateOrCreateTransactionRuleAction.objects.create(
            rule=rule_a,
            search_description="'also-missing-xyz'",
            set_type="'EX'",
            set_date="date",
            set_reference_date="reference_date",
            set_account="999999",
            set_amount="decimal('9.00')",
            set_description="'never'",
        )

        # Rule B: normal create action
        rule_b = self.make_rule(name="B", order=2)
        UpdateOrCreateTransactionRuleAction.objects.create(
            rule=rule_b,
            search_description="'missing-xyz'",
            set_type="'EX'",
            set_date="date",
            set_reference_date="reference_date",
            set_account="account_id",
            set_amount="decimal('3.00')",
            set_description="'B-derived'",
        )

        self.run_event(event=RuleExecution.Event.CREATED, tx=tx)

        # Trigger kept its original description
        tx.refresh_from_db()
        self.assertEqual(tx.description, "source")

        execution_a = RuleExecution.objects.get(rule_ref=rule_a.id)
        execution_b = RuleExecution.objects.get(rule_ref=rule_b.id)
        self.assertEqual(execution_a.status, RuleExecution.Status.FAILED)
        self.assertEqual(execution_b.status, RuleExecution.Status.COMPLETED)
        self.assertTrue(
            Transaction.objects.filter(description="B-derived").exists()
        )
        self.assertFalse(
            Transaction.objects.filter(description="A-edit").exists()
        )


class FutureVersionTests(E2ETestBase):
    def test_event_newer_than_visible_row_is_retryable(self):
        tx = self.make_tx(description="source")
        rule = self.make_rule()
        UpdateOrCreateTransactionRuleAction.objects.create(
            rule=rule,
            search_description="'missing-xyz'",
            set_type="'EX'",
            set_date="date",
            set_reference_date="reference_date",
            set_account="account_id",
            set_amount="decimal('3.00')",
            set_description="'never'",
        )

        with self.assertRaises(RetryableEventError):
            self.run_event(
                event=RuleExecution.Event.CREATED,
                tx=tx,
                transaction_version=99,
            )

        # Nothing claimed/executed
        self.assertEqual(
            RuleExecution.objects.filter(transaction_ref=tx.id).count(), 0
        )


class PermanentDeleteSignalTests(TransactionTestCase):
    @override_settings(ENABLE_SOFT_DELETE=True)
    def test_model_delete_soft_then_permanent_sends_marked_events(self):
        from apps.common.middleware.thread_local import write_current_user

        user = get_user_model().objects.create_user(
            email="del@example.com", password="testpass123"
        )
        write_current_user(user)
        currency = Currency.objects.create(
            code="USD", name="US Dollar", decimal_places=2
        )
        account = Account.objects.create(
            name="Main", currency=currency, owner=user
        )
        tx = Transaction.objects.create(
            account=account,
            type=Transaction.Type.EXPENSE,
            amount=Decimal("10.00"),
            date=date(2026, 5, 4),
            reference_date=date(2026, 5, 1),
            owner=user,
        )

        with patch(
            "apps.rules.signals.process_transaction_event.defer"
        ) as defer_mock:
            tx.delete()
            soft_call = defer_mock.call_args_list[0]
            self.assertFalse(soft_call.kwargs["is_hard_deleted"])
            self.assertEqual(
                soft_call.kwargs["transaction_version"], tx.version
            )

            tx.delete()
            hard_call = defer_mock.call_args_list[1]
            self.assertTrue(hard_call.kwargs["is_hard_deleted"])

        from apps.common.middleware.thread_local import delete_current_user

        delete_current_user()

    @override_settings(ENABLE_SOFT_DELETE=True)
    def test_queryset_permanent_delete_of_soft_deleted_is_marked_hard(self):
        from apps.common.middleware.thread_local import write_current_user

        user = get_user_model().objects.create_user(
            email="del2@example.com", password="testpass123"
        )
        write_current_user(user)
        currency = Currency.objects.create(
            code="USD", name="US Dollar", decimal_places=2
        )
        account = Account.objects.create(
            name="Main", currency=currency, owner=user
        )
        tx = Transaction.objects.create(
            account=account,
            type=Transaction.Type.EXPENSE,
            amount=Decimal("10.00"),
            date=date(2026, 5, 4),
            reference_date=date(2026, 5, 1),
            owner=user,
        )
        tx.delete()  # soft delete first

        with patch(
            "apps.rules.signals.process_transaction_event.defer"
        ) as defer_mock:
            Transaction.all_objects.filter(pk=tx.id).delete()
            hard_call = defer_mock.call_args_list[-1]
            self.assertTrue(hard_call.kwargs["is_hard_deleted"])

        self.assertFalse(
            Transaction.all_objects.filter(pk=tx.id).exists()
        )

        from apps.common.middleware.thread_local import delete_current_user

        delete_current_user()
