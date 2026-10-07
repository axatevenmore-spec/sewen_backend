"""Purchase routes (api.md §6)."""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

# from apps.inventory.views import QualityStandardViewSet  # Hidden: QC out of scope

from . import views

router = DefaultRouter(trailing_slash=True)
router.register("orders", views.PurchaseOrderViewSet, basename="purchase-orders")
router.register("bills", views.PurchaseBillViewSet, basename="purchase-bills")
router.register("receipts", views.GoodsReceiptViewSet, basename="purchase-receipts")
router.register("goods-receipts", views.GoodsReceiptViewSet, basename="purchase-goods-receipts")
router.register("payments", views.PaymentOutViewSet, basename="purchase-payments")
router.register("advances", views.VendorAdvanceViewSet, basename="purchase-advances")
router.register("returns", views.PurchaseReturnViewSet, basename="purchase-returns")
router.register("expenses", views.ExpenseViewSet, basename="purchase-expenses")
router.register("vendors", views.VendorLookupViewSet, basename="purchase-vendors")
# router.register(
#     "quality-standards", QualityStandardViewSet, basename="purchase-quality-standards"
# )  # Hidden: QC out of scope
router.register("asns", views.AdvanceShippingNoticeViewSet, basename="purchase-asns")

vendor_portal_urlpatterns = [
    path("auth/login/", views.VendorPortalLoginView.as_view(), name="vendor-portal-login"),
    path("orders/", views.VendorPortalOrdersView.as_view(), name="vendor-portal-orders"),
    path("orders/<str:pk>/acknowledge/", views.VendorPortalOrderAcknowledgeView.as_view(), name="vendor-portal-order-ack"),
    path("orders/<str:pk>/milestones/", views.VendorPortalOrderMilestoneView.as_view(), name="vendor-portal-order-milestone"),
    path("asn/create/", views.VendorPortalASNCreateView.as_view(), name="vendor-portal-asn-create"),
    path("ledger/", views.VendorPortalLedgerView.as_view(), name="vendor-portal-ledger"),
]

urlpatterns = [
    path("vendor-portal/", include(vendor_portal_urlpatterns)),
    path("", include(router.urls)),
]
