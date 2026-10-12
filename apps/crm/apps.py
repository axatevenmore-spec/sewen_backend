from django.apps import AppConfig


class CrmConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.crm"
    label = "crm"
    verbose_name = "CRM"

    def ready(self):
        from . import signals  # noqa: F401 -- connects the Deals pipeline hooks
