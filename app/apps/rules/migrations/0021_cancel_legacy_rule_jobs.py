from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("rules", "0020_ruleexecution_ruleactionexecution_and_more"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
                UPDATE procrastinate_jobs
                SET status = 'cancelled'
                WHERE task_name = 'check_for_transaction_rules'
                  AND status = 'todo';
            """,
            reverse_sql="""
                UPDATE procrastinate_jobs
                SET status = 'todo'
                WHERE task_name = 'check_for_transaction_rules'
                  AND status = 'cancelled';
            """,
        ),
    ]
