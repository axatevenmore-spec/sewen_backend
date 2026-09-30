"""Quotation-first sales: the commercial header fields a printed quotation carries."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0007_challan_out_for_delivery"),
    ]

    operations = [
        migrations.AddField(
            model_name="quotation",
            name="reference_number",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="quotation",
            name="salesperson",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="quotation",
            name="payment_terms",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="quotation",
            name="delivery_terms",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="quotation",
            name="authorized_person",
            field=models.TextField(blank=True, null=True),
        ),
    ]
