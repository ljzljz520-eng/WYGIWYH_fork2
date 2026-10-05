from django.contrib import admin

from apps.rules.models import (
    RuleActionExecution,
    RuleExecution,
    TransactionRule,
    TransactionRuleAction,
    UpdateOrCreateTransactionRuleAction,
)


admin.site.register(TransactionRule)
admin.site.register(TransactionRuleAction)
admin.site.register(UpdateOrCreateTransactionRuleAction)


@admin.register(RuleExecution)
class RuleExecutionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "transaction_ref",
        "event",
        "transaction_version",
        "rule_ref",
        "rule_version",
        "status",
        "created_by",
        "created_at",
    )
    list_filter = ("event", "status")
    search_fields = ("transaction_ref", "rule_ref")
    readonly_fields = (
        "transaction_ref",
        "rule_ref",
        "transaction",
        "rule",
        "event",
        "transaction_version",
        "rule_version",
        "detail",
        "summary",
        "created_by",
        "created_at",
        "updated_at",
    )


@admin.register(RuleActionExecution)
class RuleActionExecutionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "rule_execution",
        "action_type",
        "action_ref",
        "order",
        "status",
    )
    list_filter = ("action_type", "status")
    search_fields = ("action_ref",)
    readonly_fields = (
        "rule_execution",
        "action_type",
        "action_ref",
        "order",
        "status",
        "error",
        "effects",
        "created_at",
    )
