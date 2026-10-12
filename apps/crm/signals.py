"""
Deals pipeline hooks.

A lead's deal follows what happens to the lead, wherever it happens (the API,
the public lead form, an import, the approval link):

    new lead                          -> a Draft deal
    estimate / quotation sent         -> the deal moves to Sent
    estimate / quotation accepted     -> the deal (and the lead) is Won
    estimate accepted                 -> a Draft contract is generated from it
"""
from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from apps.sales.models import Estimate, Quotation

from . import services
from .models import Lead


@receiver(post_save, sender=Lead, dispatch_uid="crm_lead_opens_a_deal")
def lead_opens_a_deal(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        transaction.on_commit(lambda: services.ensure_lead_deal(instance, user=instance.created_by))


def _remember_status(sender, instance, raw=False, **kwargs):
    if raw or instance.pk is None:
        instance._status_before_save = None
        return
    instance._status_before_save = (
        sender.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
    )


def _follow_status(kind):
    def handler(sender, instance, created, raw=False, **kwargs):
        if raw or not instance.crm_lead_id:
            return
        if not created and getattr(instance, "_status_before_save", None) == instance.status:
            return
        services.sync_deal_from_document(instance, kind)
        if kind == "estimate" and instance.status == "Accepted":
            services.contract_from_estimate(instance)

    return handler


for _model, _kind in ((Estimate, "estimate"), (Quotation, "quotation")):
    pre_save.connect(_remember_status, sender=_model, dispatch_uid=f"crm_{_kind}_status_before")
    post_save.connect(_follow_status(_kind), sender=_model, weak=False, dispatch_uid=f"crm_{_kind}_deal_stage")
