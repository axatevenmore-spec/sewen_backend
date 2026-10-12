from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0009_lead_previous_owner_lead_reassigned_at_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="mastertask",
            name="in_task_roles",
            field=models.BooleanField(default=False),
        ),
    ]
