from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TransactionTestCase

from apps.accounts.models import Account
from apps.common.middleware.thread_local import (
    delete_current_user,
    write_current_user,
)
from apps.currencies.models import Currency
from apps.rules.models import (
    RuleActionExecution,
    RuleExecution,
    TransactionRule,
    TransactionRuleAction,
    UpdateOrCreateTransactionRuleAction,
)
from apps.rules.services.executor import execute_plan
from apps.rules.services.planner import build_plan
from apps.transactions.models import Transaction


class ExecutorTestBase(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="executor@example.com", password="testpass123"
        )
        write_current_user(self.user)
        self.currency = Currency.objects.create(
            code="USD", name="US Dollar", decimal_places=2
        )
        self.account = Account.objects.create(
            name="Main", currency=self.currency, owner=self.user
        )

    def tearDown(self):
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

    def claim_execution(self, rule, tx, event):
        return RuleExecution.objects.create(
            transaction=tx,
            transaction_ref=tx.id,
            rule=rule,
            rule_ref=rule.id,
            event=event,
            transaction_version=tx.version,
            rule_version=rule.version,
            status=RuleExecution.Status.PENDING,
            created_by=self.user,
        )


class AtomicExecutorTests(ExecutorTestBase):
    def test_failure_rolls_back_modify_and_create_and_records_failure(self):
        tx = self.make_tx(amount="10.00", description="source")
        rule = self.make_rule()

        TransactionRuleAction.objects.create(
            rule=rule,
            field=TransactionRuleAction.Field.amount,
            value="decimal('20.00')",
        )
        create_action = UpdateOrCreateTransactionRuleAction.objects.create(
            rule=rule,
            search_description="'will-not-match'",
            set_account="account_id",
            set_amount="decimal('7.00')",
            set_date="date",
            set_reference_date="reference_date",
            set_description="'derived'",
            order=1,
        )
        failing_action = TransactionRuleAction.objects.create(
            rule=rule,
            field=TransactionRuleAction.Field.notes,
            value="int('not-a-number')",
            order=5,
        )

        with transaction.atomic():
            execution = self.claim_execution(
                rule, tx, RuleExecution.Event.CREATED
            )
            plan = build_plan(
                rule=rule,
                transaction=tx,
                event=RuleExecution.Event.CREATED,
            )
            execute_plan(
                execution=execution,
                plan=plan,
                rule=rule,
                trigger=tx,
            )

        # Source modification rolled back
        tx.refresh_from_db()
        self.assertEqual(tx.amount, Decimal("10.00"))

        # Derived transaction never existed
        self.assertFalse(
            Transaction.objects.filter(description="derived").exists()
        )
        self.assertEqual(Transaction.objects.count(), 1)

        # Failed RuleExecution with failure action and input versions
        execution.refresh_from_db()
        self.assertEqual(execution.status, RuleExecution.Status.FAILED)
        self.assertEqual(
            execution.detail["failed_action"], failing_action.id
        )
        self.assertEqual(
            execution.detail["transaction_version"], execution.transaction_version
        )
        self.assertEqual(
            execution.detail["rule_version"], execution.rule_version
        )
        self.assertTrue(execution.detail["error"])

        # Records: rolled_back, rolled_back, failed
        statuses = list(
            execution.action_executions.values_list("status", flat=True)
        )
        self.assertEqual(
            statuses,
            [
                RuleActionExecution.Status.ROLLED_BACK,
                RuleActionExecution.Status.ROLLED_BACK,
                RuleActionExecution.Status.FAILED,
            ],
        )


class IsolatedExecutorTests(ExecutorTestBase):
    def test_middle_action_failure_keeps_other_actions(self):
        tx = self.make_tx(amount="10.00", description="source")
        rule = self.make_rule(execution_mode=TransactionRule.ExecutionMode.ISOLATED)

        first = TransactionRuleAction.objects.create(
            rule=rule,
            field=TransactionRuleAction.Field.amount,
            value="decimal('20.00')",
        )
        second = TransactionRuleAction.objects.create(
            rule=rule,
            field=TransactionRuleAction.Field.notes,
            value="int('not-a-number')",
        )
        third = UpdateOrCreateTransactionRuleAction.objects.create(
            rule=rule,
            search_description="'will-not-match'",
            set_account="account_id",
            set_amount="decimal('7.00')",
            set_date="date",
            set_reference_date="reference_date",
            set_description="'derived'",
        )

        with transaction.atomic():
            execution = self.claim_execution(
                rule, tx, RuleExecution.Event.CREATED
            )
            plan = build_plan(
                rule=rule,
                transaction=tx,
                event=RuleExecution.Event.CREATED,
            )
            execute_plan(
                execution=execution,
                plan=plan,
                rule=rule,
                trigger=tx,
            )

        tx.refresh_from_db()
        self.assertEqual(tx.amount, Decimal("20.00"))

        derived = Transaction.objects.get(description="derived")
        self.assertEqual(derived.amount, Decimal("7.00"))

        execution.refresh_from_db()
        self.assertEqual(execution.status, RuleExecution.Status.COMPLETED)

        records = execution.action_executions
        statuses = list(
            records.values_list("action_type", "action_ref", "status")
        )
        self.assertEqual(
            statuses,
            [
                (
                    RuleActionExecution.ActionType.EDIT,
                    first.id,
                    RuleActionExecution.Status.APPLIED,
                ),
                (
                    RuleActionExecution.ActionType.EDIT,
                    second.id,
                    RuleActionExecution.Status.FAILED,
                ),
                (
                    RuleActionExecution.ActionType.UPDATE_OR_CREATE,
                    third.id,
                    RuleActionExecution.Status.APPLIED,
                ),
            ],
        )
        failed_record = records.get(
            action_type=RuleActionExecution.ActionType.EDIT,
            action_ref=second.id,
        )
        self.assertTrue(failed_record.error)


class ActionRecordAndProvenanceTests(ExecutorTestBase):
    def test_one_record_per_action_with_effects_and_provenance(self):
        tx = self.make_tx(amount="10.00", description="source")
        self.make_tx(amount="100.00", description="rent")

        rule = self.make_rule()

        edit = TransactionRuleAction.objects.create(
            rule=rule,
            field=TransactionRuleAction.Field.amount,
            value="decimal('20.00')",
        )
        hit = UpdateOrCreateTransactionRuleAction.objects.create(
            rule=rule,
            search_description="'rent'",
            set_amount="decimal('5.00')",
        )
        miss = UpdateOrCreateTransactionRuleAction.objects.create(
            rule=rule,
            search_description="'nonexistent'",
            set_account="account_id",
            set_amount="decimal('7.00')",
            set_date="date",
            set_reference_date="reference_date",
            set_description="'derived'",
        )
        rejected = UpdateOrCreateTransactionRuleAction.objects.create(
            rule=rule,
            search_description="'source'",
            filter="False",
            set_amount="decimal('9.00')",
        )

        with transaction.atomic():
            execution = self.claim_execution(
                rule, tx, RuleExecution.Event.CREATED
            )
            plan = build_plan(
                rule=rule,
                transaction=tx,
                event=RuleExecution.Event.CREATED,
            )
            execute_plan(
                execution=execution,
                plan=plan,
                rule=rule,
                trigger=tx,
            )

        records = execution.action_executions
        self.assertEqual(records.count(), 4)

        # Edit -> applied with one modify effect on trigger
        edit_record = records.get(
            action_type=RuleActionExecution.ActionType.EDIT,
            action_ref=edit.id,
        )
        self.assertEqual(edit_record.status, RuleActionExecution.Status.APPLIED)
        self.assertEqual(edit_record.effects[0]["kind"], "modify")
        self.assertEqual(edit_record.effects[0]["target_ref"], tx.id)

        # Upsert hit -> applied, modify effect with correct target
        hit_record = records.get(
            action_type=RuleActionExecution.ActionType.UPDATE_OR_CREATE,
            action_ref=hit.id,
        )
        self.assertEqual(hit_record.status, RuleActionExecution.Status.APPLIED)
        hit_effect = hit_record.effects[0]
        self.assertEqual(hit_effect["kind"], "modify")
        rent_id = Transaction.objects.get(description="rent").id
        self.assertEqual(hit_effect["target_ref"], rent_id)
        self.assertEqual(
            Decimal(hit_effect["after"]["amount"]), Decimal("5.00")
        )

        # Upsert miss -> applied, create effect and provenance on derived tx
        miss_record = records.get(
            action_type=RuleActionExecution.ActionType.UPDATE_OR_CREATE,
            action_ref=miss.id,
        )
        self.assertEqual(miss_record.status, RuleActionExecution.Status.APPLIED)
        self.assertEqual(miss_record.effects[0]["kind"], "create")

        derived = Transaction.objects.get(description="derived")
        self.assertEqual(
            derived.generated_by_action_execution_id, miss_record.id
        )

        # Rejected guard -> skipped with reject effect
        reject_record = records.get(
            action_type=RuleActionExecution.ActionType.UPDATE_OR_CREATE,
            action_ref=rejected.id,
        )
        self.assertEqual(
            reject_record.status, RuleActionExecution.Status.SKIPPED
        )
        self.assertEqual(reject_record.effects[0]["kind"], "reject")
