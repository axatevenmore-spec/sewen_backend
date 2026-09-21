"""
Root URL configuration.

Base path ``/api/v1`` with DRF trailing slashes (api.md §1.1). Every path here
matches what ``services/domainServices.js`` already emits -- api.md §0 is
explicit that those signatures are part of the contract.
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from apps.accounts.urls import admin_urlpatterns, auth_urlpatterns
from apps.reports.urls import dashboard_urlpatterns, public_urlpatterns, report_urlpatterns

api_v1 = [
    # Identity and administration (api.md §2, §3)
    path("auth/", include(auth_urlpatterns)),
    path("admin/", include(admin_urlpatterns)),
    # Shared masters (api.md §4)
    path("parties/", include("apps.masters.urls")),
    path("inventory/", include("apps.inventory.urls")),
    # Documents (api.md §5, §6)
    path("sales/", include("apps.sales.urls")),
    path("purchase/", include("apps.purchase.urls")),
    # Accounts (api.md §8)
    path("accounts/", include("apps.accounting.urls")),
    # Modules (api.md §9, §10, §11)
    path("crm/", include("apps.crm.urls")),
    path("pms/", include("apps.pms.urls")),
    path("hrms/", include("apps.hrms.urls")),
    # Reports and dashboards (api.md §12)
    path("reports/", include(report_urlpatterns)),
    path("dashboard/", include(dashboard_urlpatterns)),
    # Unauthenticated surfaces (api.md §5.3, §9.7, §10.6, §11.5)
    path("public/", include(public_urlpatterns)),
    # Platform: files, notifications, audit, settings, search, support
    path("", include("apps.core.urls")),
    # OpenAPI 3.1, generated from the models so it cannot drift (db.md §14.2)
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path("redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]

urlpatterns = [
    path("api/v1/", include(api_v1)),
    path("django-admin/", admin.site.urls),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
