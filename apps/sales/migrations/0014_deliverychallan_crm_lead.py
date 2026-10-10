# Generated for DeliveryChallan.crm_lead linkage to CRM leads.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0009_lead_previous_owner_lead_reassigned_at_and_more"),
        ("sales", "0013_deliverychallan_is_one_time_party_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="deliverychallan",
            name="crm_lead",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="challans",
                to="crm.lead",
            ),
        ),
    ]
