from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0012_pipeline_deal_stages"),
    ]

    operations = [
        migrations.AddField(
            model_name="form",
            name="is_active",
            field=models.BooleanField(default=True),
        ),
    ]
