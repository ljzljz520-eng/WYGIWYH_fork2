from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TransactionTestCase

from apps.accounts.models import Account
from apps.common.middleware.thread_local import (
    delete_current_user,
    write_current_user,
)
from apps.currencies.models import Currency
from apps.rules.models import (
    RuleExecution,
    TransactionRule,
    TransactionRuleAction,
    UpdateOrCreateTransactionRuleAction,
)
from apps.rules.services.evaluation import FrozenEvalContext
from apps.rules.services.planner import (
    RuleActionExecutionKind,
    build_plan,
)
from apps.transactions.models import Transaction


class PlannerTestBase(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="planner@example.com", password="testpass123"
        )
        write_current_user(self.user)
        self.currency = Currency.objects.create(
            code="USD", name="US Dollar", decimal_places=2
        )
        self.account = Account.objects.create(
            name="Main", currency=self.currency, owner=self.user
        )
        self.account2 = Account.objects.create(
            name="Other", currency=self.currency, owner=self.user
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

    def frozen_context(self):
        return FrozenEvalContext.fresh(seed=42)


class PlannerReadonlyTests(PlannerTestBase):
    def test_planning_performs_no_writes(self):
        tx = self.make_tx(description="rent")
        rule = self.make_rule()
        TransactionRuleAction.objects.create(
            rule=rule,
            field=TransactionRuleAction.Field.amount,
            value="decimal('20.00')",
        )
        UpdateOrCreateTransactionRuleAction.objects.create(
            rule=rule,
            search_description="'rent'",
            set_amount="decimal('5.00')",
        )

        before_count = Transaction.objects.count()
        before_version = tx.version

        plan = build_plan(
            rule=rule,
            transaction=tx,
            event=RuleExecution.Event.UPDATED,
            context=self.frozen_context(),
        )

        self.assertEqual(Transaction.objects.count(), before_count)
        tx.refresh_from_db()
        self.assertEqual(tx.version, before_version)
        self.assertEqual(tx.amount, Decimal("10.00"))

        # Two action plans: one edit modify, one upsert modify (hit)
        self.assertEqual(len(plan.action_plans), 2)
        self.assertEqual(
            plan.grouped_effects()["modify"][0]["target_ref"], tx.id
        )


class PlannerOrderingTests(PlannerTestBase):
    def test_custom_order_interleaves_actions(self):
        tx = self.make_tx(description="rent")
        rule = self.make_rule()
        edit = TransactionRuleAction.objects.create(
            rule=rule,
            field=TransactionRuleAction.Field.amount,
            value="decimal('20.00')",
            order=5,
        )
        upsert = UpdateOrCreateTransactionRuleAction.objects.create(
            rule=rule,
            search_description="'rent'",
            set_amount="decimal('5.00')",
            order=2,
        )

        plan = build_plan(
            rule=rule,
            transaction=tx,
            event=RuleExecution.Event.CREATED,
            context=self.frozen_context(),
        )

        ordered_refs = [
            (action.action_type, action.action_ref)
            for action in plan.action_plans
        ]
        self.assertEqual(
            ordered_refs,
            [
                ("update_or_create_transaction", upsert.id),
                ("edit_transaction", edit.id),
            ],
        )

    def test_default_order_edits_first_then_upserts(self):
        tx = self.make_tx(description="rent")
        rule = self.make_rule()
        upsert = UpdateOrCreateTransactionRuleAction.objects.create(
            rule=rule,
            search_description="'rent'",
            set_amount="decimal('5.00')",
        )
        edit = TransactionRuleAction.objects.create(
            rule=rule,
            field=TransactionRuleAction.Field.amount,
            value="decimal('20.00')",
        )

        plan = build_plan(
            rule=rule,
            transaction=tx,
            event=RuleExecution.Event.CREATED,
            context=self.frozen_context(),
        )

        ordered_types = [
            action.action_type for action in plan.action_plans
        ]
        self.assertEqual(
            ordered_types, ["edit_transaction", "update_or_create_transaction"]
        )

    def test_sequenced_edit_sees_previous_edit(self):
        tx = self.make_tx(amount="10.00")
        rule = self.make_rule(sequenced=True)
        TransactionRuleAction.objects.create(
            rule=rule,
            field=TransactionRuleAction.Field.amount,
            value="decimal('20.00')",
        )
        second = TransactionRuleAction.objects.create(
            rule=rule,
            field=TransactionRuleAction.Field.description,
            value="str(amount)",
        )

        plan = build_plan(
            rule=rule,
            transaction=tx,
            event=RuleExecution.Event.CREATED,
            context=self.frozen_context(),
        )

        second_plan = next(
            action
            for action in plan.action_plans
            if action.action_ref == second.id
        )
        self.assertEqual(
            second_plan.effects[0].new, "20.00"
        )

    def test_delete_event_only_plans_upserts(self):
        tx = self.make_tx(description="rent")
        rule = self.make_rule()
        TransactionRuleAction.objects.create(
            rule=rule,
            field=TransactionRuleAction.Field.amount,
            value="decimal('20.00')",
        )
        upsert = UpdateOrCreateTransactionRuleAction.objects.create(
            rule=rule,
            search_description="'rent'",
            set_amount="decimal('5.00')",
        )

        plan = build_plan(
            rule=rule,
            transaction=tx,
            event=RuleExecution.Event.DELETED,
            context=self.frozen_context(),
        )

        self.assertEqual(len(plan.action_plans), 1)
        self.assertEqual(plan.action_plans[0].action_ref, upsert.id)


class PlannerEffectKindTests(PlannerTestBase):
    def test_upsert_hit_is_modify(self):
        target = self.make_tx(description="rent", amount="100.00")
        rule = self.make_rule()
        action = UpdateOrCreateTransactionRuleAction.objects.create(
            rule=rule,
            search_description="'rent'",
            set_amount="decimal('5.00')",
        )

        plan = build_plan(
            rule=rule,
            transaction=self.make_tx(description="source"),
            event=RuleExecution.Event.CREATED,
            context=self.frozen_context(),
        )

        grouped = plan.grouped_effects()
        self.assertEqual(len(grouped["modify"]), 1)
        self.assertEqual(grouped["modify"][0]["target_ref"], target.id)
        self.assertEqual(
            Decimal(grouped["modify"][0]["after"]["amount"]),
            Decimal("5.00"),
        )
        self.assertEqual(
            Decimal(grouped["modify"][0]["before"]["amount"]),
            Decimal("100.00"),
        )

    def test_upsert_miss_is_create(self):
        rule = self.make_rule()
        UpdateOrCreateTransactionRuleAction.objects.create(
            rule=rule,
            search_description="'nonexistent'",
            set_account="account_id",
            set_amount="decimal('5.00')",
            set_date="date",
            set_reference_date="reference_date",
            set_description="'derived'",
        )

        plan = build_plan(
            rule=rule,
            transaction=self.make_tx(description="source"),
            event=RuleExecution.Event.CREATED,
            context=self.frozen_context(),
        )

        grouped = plan.grouped_effects()
        self.assertEqual(len(grouped["create"]), 1)
        created = grouped["create"][0]
        self.assertIsNone(created["target_ref"])
        self.assertEqual(created["after"]["description"], "derived")
        self.assertEqual(
            Decimal(created["after"]["amount"]), Decimal("5.00")
        )

    def test_filter_guard_false_is_reject(self):
        rule = self.make_rule()
        UpdateOrCreateTransactionRuleAction.objects.create(
            rule=rule,
            search_description="'source'",
            filter="False",
            set_amount="decimal('5.00')",
        )

        plan = build_plan(
            rule=rule,
            transaction=self.make_tx(description="source"),
            event=RuleExecution.Event.CREATED,
            context=self.frozen_context(),
        )

        grouped = plan.grouped_effects()
        self.assertEqual(len(grouped["reject"]), 1)
        self.assertEqual(
            grouped["reject"][0]["reason"], "filter_did_not_match"
        )

    def test_trigger_mismatch_produces_skipped_plan(self):
        rule = self.make_rule(trigger="is_income")
        plan = build_plan(
            rule=rule,
            transaction=self.make_tx(),
            event=RuleExecution.Event.CREATED,
            context=self.frozen_context(),
        )

        self.assertFalse(plan.triggered)
        self.assertEqual(plan.status, RuleExecution.Status.SKIPPED)
        self.assertEqual(plan.action_plans, [])

    def test_fingerprints_differ_when_values_differ(self):
        tx = self.make_tx()
        rule = self.make_rule()
        TransactionRuleAction.objects.create(
            rule=rule,
            field=TransactionRuleAction.Field.amount,
            value="decimal('20.00')",
        )

        plan_a = build_plan(
            rule=rule,
            transaction=tx,
            event=RuleExecution.Event.CREATED,
            context=FrozenEvalContext.fresh(seed=1),
        )

        TransactionRuleAction.objects.filter(
            rule=rule, field=TransactionRuleAction.Field.amount
        ).update(value="decimal('30.00')")
        rule.refresh_from_db()

        plan_b = build_plan(
            rule=rule,
            transaction=tx,
            event=RuleExecution.Event.CREATED,
            context=FrozenEvalContext.fresh(seed=1),
        )

        self.assertNotEqual(plan_a.fingerprints(), plan_b.fingerprints())
        self.assertTrue(
            any("amount=20.00" in fp for fp in plan_a.fingerprints())
        )
        self.assertTrue(
            any("amount=30.00" in fp for fp in plan_b.fingerprints())
        )
