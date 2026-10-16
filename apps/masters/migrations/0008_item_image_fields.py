from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0001_initial"),
        ("masters", "0007_phase3_metal_item_master_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="item",
            name="image_file",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="core.file",
            ),
        ),
        migrations.AddField(
            model_name="item",
            name="image_url",
            field=models.TextField(blank=True, null=True),
        ),
    ]
