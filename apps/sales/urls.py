"""Sales routes (api.md §5). Paths match ``domainServices.js`` exactly."""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import portal, views

router = DefaultRouter(trailing_slash=True)
router.register("estimates", views.EstimateViewSet, basename="sales-estimates")
router.register("quotations", views.QuotationViewSet, basename="sales-quotations")
router.register("orders", views.SalesOrderViewSet, basename="sales-orders")
router.register(
    "proforma-invoices", views.ProformaInvoiceViewSet, basename="sales-proformas"
)
router.register("challans", views.DeliveryChallanViewSet, basename="sales-challans")
router.register("invoices", views.SalesInvoiceViewSet, basename="sales-invoices")
router.register("payments", views.PaymentInViewSet, basename="sales-payments")
router.register("cash-receipts", views.CashPaymentReceiptViewSet, basename="sales-cash-receipts")
router.register("returns", views.SalesReturnViewSet, basename="sales-returns")
# Hidden: Warranty Cards out of scope (Sweven spec)
# router.register("warranties", views.WarrantyCardViewSet, basename="sales-warranties")

urlpatterns = [path("", include(router.urls))]

# Client portal reorder (apps/sales/portal.py), mounted at /portal/client/.
portal_urlpatterns = [
    path(
        "orderable-items/",
        portal.PortalOrderableItemsView.as_view(),
        name="portal-client-orderable-items",
    ),
    path(
        "place-order/",
        portal.PortalPlaceOrderView.as_view(),
        name="portal-client-place-order",
    ),
]
