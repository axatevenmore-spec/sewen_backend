"""Accounts endpoints (api.md §8)."""
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.exceptions import Codes, Conflict, NotFound, ValidationFailed
from apps.core.money import ZERO, D, round2
from apps.core.pagination import envelope
from apps.core.permissions import HasModulePermission
from apps.core.viewsets import ReadOnlyTenantViewSet, TenantModelViewSet

from . import reports, services
from .models import Account, BankAccount, BankTransfer, Budget, ExpenseCategory, JournalEntry, JournalLine
from .serializers import (
    AccountSerializer,
    BankAccountSerializer,
    BankTransferSerializer,
    BudgetSerializer,
    ExpenseCategorySerializer,
    JournalEntrySerializer,
)

MONEY = DecimalField(max_digits=18, decimal_places=2)


class AccountViewSet(TenantModelViewSet):
    queryset = Account.objects.select_related("parent")
    serializer_class = AccountSerializer
    audit_entity_type = "Account"
    audit_label_field = "name"
    permission_map = {
        "read": ["view_ledger"],
        # Setting up accounts, bank accounts, transfers and budgets posts to or
        # shapes the ledger -- the accountant's permission, not a viewer's.
        "write": ["manage_journal_entries"],
    }
    status_field = None
    filter_map = {"type": "type", "subtype": "subtype", "isActive": "is_active"}
    search_fields = ["name", "code"]
    ordering = ["code"]

    def perform_destroy(self, instance):
        if instance.is_system:
            raise Conflict(
                "System accounts cannot be deleted -- the posting matrix depends on them.",
                code="SYSTEM_ACCOUNT",
            )
        if JournalLine.objects.filter(account=instance, deleted_at__isnull=True).exists():
            raise Conflict(
                "This account has ledger entries and cannot be deleted.",
                code=Codes.IN_USE,
            )
        super().perform_destroy(instance)

    @action(detail=False, methods=["get"], url_path="tree")
    def tree(self, request):
        """``GET /accounts/chart-of-accounts/`` -- the account tree."""
        rows = list(self.filter_queryset(self.get_queryset()))
        nodes = {
            row.id: {**AccountSerializer(row).data, "children": []} for row in rows
        }
        roots = []
        for row in rows:
            node = nodes[row.id]
            parent = nodes.get(row.parent_id) if row.parent_id else None
            (parent["children"] if parent else roots).append(node)
        return Response({"accounts": roots})


class BankAccountViewSet(TenantModelViewSet):
    queryset = BankAccount.objects.select_related("account")
    serializer_class = BankAccountSerializer
    audit_entity_type = "BankAccount"
    audit_label_field = "name"
    permission_map = {
        "read": ["view_bank_accounts"],
        # Setting up accounts, bank accounts, transfers and budgets posts to or
        # shapes the ledger -- the accountant's permission, not a viewer's.
        "write": ["manage_journal_entries"],
    }
    status_field = None
    search_fields = ["name", "account_number", "bank_name"]
    ordering = ["-is_default", "name"]

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        balances = {
            row["id"]: row["balance"]
            for row in services.account_balances(request.client_id)
            for _ in [0]
        }
        for bank in page:
            bank.computed_balance = balances.get(str(bank.id))
        return self.get_paginated_response(self.get_serializer(page, many=True).data)

    @transaction.atomic
    def perform_create(self, serializer):
        bank = super().perform_create(serializer)
        if bank.is_default:
            BankAccount.objects.filter(
                client_id=self.get_client_id(), is_default=True
            ).exclude(pk=bank.pk).update(is_default=False)
        return bank


class BalancesView(APIView):
    """``GET /accounts/balances/`` -- replaces ``getAccountBalances``."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_bank_accounts"]

    def get(self, request):
        rows = services.account_balances(
            request.client_id, as_of=request.query_params.get("asOf")
        )
        return Response(
            envelope(
                rows,
                aggregates={"totalBalance": round2(sum((r["balance"] for r in rows), ZERO))},
            )
        )


class BankTransferViewSet(TenantModelViewSet):
    queryset = BankTransfer.objects.select_related("from_account", "to_account")
    serializer_class = BankTransferSerializer
    audit_entity_type = "BankTransfer"
    permission_map = {
        "read": ["view_bank_accounts"],
        # Setting up accounts, bank accounts, transfers and budgets posts to or
        # shapes the ledger -- the accountant's permission, not a viewer's.
        "write": ["manage_journal_entries"],
    }
    status_field = None
    default_date_field = "transfer_date"
    ordering = ["-transfer_date"]

    @transaction.atomic
    def perform_create(self, serializer):
        transfer = super().perform_create(serializer)
        entry = services.post_bank_transfer(transfer, user=self.request.user)
        if entry is not None:
            transfer.journal_entry = entry
            transfer.save(update_fields=["journal_entry", "updated_at"])
        return transfer


class JournalEntryViewSet(TenantModelViewSet):
    queryset = JournalEntry.objects.prefetch_related("lines__account", "lines__party")
    serializer_class = JournalEntrySerializer
    audit_entity_type = "JournalEntry"
    audit_label_field = "entry_number"
    required_permissions = ["manage_journal_entries"]
    status_field = "status"
    default_date_field = "entry_date"
    search_fields = ["entry_number", "narration"]
    ordering = ["-entry_date", "-created_at"]
    filter_map = {
        "sourceDocumentType": "source_document_type",
        "sourceDocumentId": "source_document_id",
        "isSystem": "is_system",
    }

    def get_aggregates(self, queryset):
        rows = JournalLine.objects.filter(
            journal_entry__in=queryset.values("id"), deleted_at__isnull=True
        ).aggregate(
            debit=Coalesce(Sum("debit"), Value(Decimal("0.00")), output_field=MONEY),
            credit=Coalesce(Sum("credit"), Value(Decimal("0.00")), output_field=MONEY),
        )
        return {"count": queryset.count(), **rows}

    @transaction.atomic
    def perform_create(self, serializer):
        """Manual entries only -- system entries come from the posting matrix."""
        lines = serializer.validated_data.pop("lines")
        postings = [
            services.Posting(
                line["account"],
                debit=line.get("debit") or ZERO,
                credit=line.get("credit") or ZERO,
                party=line.get("party"),
                description=line.get("description"),
            )
            for line in lines
        ]
        entry = services.post_entry(
            client=self.request.user.client,
            postings=postings,
            entry_date=serializer.validated_data.get("entry_date"),
            narration=serializer.validated_data.get("narration"),
            is_system=False,
            user=self.request.user,
        )
        serializer.instance = entry
        self._created_instance = entry
        self._concurrency_instance = entry
        self.write_audit("create", entry, description="Manual journal entry posted")
        return entry

    def perform_update(self, serializer):
        if serializer.instance.is_system:
            raise Conflict(
                "System journal entries are immutable.",
                code="SYSTEM_ENTRY",
                detail="Cancel the source document instead, which posts a reversing entry.",
            )
        return super().perform_update(serializer)

    def perform_destroy(self, instance):
        raise Conflict(
            "Journal entries are reversed, never deleted.",
            code="SYSTEM_ENTRY",
            detail="Use /reverse/ to post a mirror entry.",
        )

    @action(detail=True, methods=["post"])
    def reverse(self, request, pk=None):
        entry = self.get_object()
        reversal = services.reverse_entry(
            entry, user=request.user, narration=request.data.get("narration")
        )
        if reversal is None:
            raise Conflict("This entry has already been reversed.", code=Codes.ALREADY_DONE)
        return Response(
            self.get_serializer(reversal).data, status=status.HTTP_201_CREATED
        )


class AccountLedgerView(APIView):
    """``GET /accounts/ledger/{accountId}/``."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_ledger"]

    def get(self, request, account_id):
        if not Account.objects.filter(pk=account_id, client_id=request.client_id).exists():
            raise NotFound("That account no longer exists.")
        return Response(
            services.account_ledger(
                request.client_id,
                account_id,
                date_from=request.query_params.get("date_from"),
                date_to=request.query_params.get("date_to"),
            )
        )


class BudgetViewSet(TenantModelViewSet):
    queryset = Budget.objects.select_related("account")
    serializer_class = BudgetSerializer
    audit_entity_type = "Budget"
    permission_map = {
        "read": ["view_financial_reports"],
        # Setting up accounts, bank accounts, transfers and budgets posts to or
        # shapes the ledger -- the accountant's permission, not a viewer's.
        "write": ["manage_journal_entries"],
    }
    status_field = None
    default_date_field = "period_start"
    ordering = ["-period_start"]

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        for budget in page:
            rows = JournalLine.objects.filter(
                client_id=request.client_id,
                account=budget.account,
                deleted_at__isnull=True,
                journal_entry__entry_date__gte=budget.period_start,
                journal_entry__entry_date__lte=budget.period_end,
            ).exclude(journal_entry__status="Reversed").aggregate(
                debit=Coalesce(Sum("debit"), Value(Decimal("0.00")), output_field=MONEY),
                credit=Coalesce(Sum("credit"), Value(Decimal("0.00")), output_field=MONEY),
            )
            budget.actual_amount = round2(rows["debit"] - rows["credit"])
        return self.get_paginated_response(self.get_serializer(page, many=True).data)


class ExpenseCategoryViewSet(TenantModelViewSet):
    queryset = ExpenseCategory.objects.select_related("account")
    serializer_class = ExpenseCategorySerializer
    audit_entity_type = "ExpenseCategory"
    audit_label_field = "name"
    permission_map = {
        "read": ["view_purchase"],
        # Setting up accounts, bank accounts, transfers and budgets posts to or
        # shapes the ledger -- the accountant's permission, not a viewer's.
        "write": ["manage_journal_entries"],
    }
    status_field = None
    ordering = ["name"]


# ---------------------------------------------------------------------------
# Reports (api.md §8)
# ---------------------------------------------------------------------------
class FinancialReportView(APIView):
    """``GET /accounts/reports/{key}/``."""

    permission_classes = [HasModulePermission]
    required_permissions = ["view_financial_reports"]

    HANDLERS = {
        "profit-and-loss": "profit_and_loss",
        "balance-sheet": "balance_sheet",
        "cash-flow": "cash_flow",
        "trial-balance": "trial_balance",
        "gst-summary": "gst_summary",
        "ageing": "ageing",
    }

    def get(self, request, report_key):
        handler = self.HANDLERS.get(report_key)
        if handler is None:
            raise NotFound(f"Unknown report '{report_key}'.")

        params = request.query_params
        date_from = params.get("date_from")
        date_to = params.get("date_to")
        as_of = params.get("asOf") or params.get("as_of")

        if report_key == "profit-and-loss":
            return Response(reports.profit_and_loss(request.client_id, date_from, date_to))
        if report_key == "balance-sheet":
            return Response(reports.balance_sheet(request.client_id, as_of))
        if report_key == "cash-flow":
            return Response(reports.cash_flow(request.client_id, date_from, date_to))
        if report_key == "trial-balance":
            rows = services.trial_balance(request.client_id, as_of=as_of)
            return Response(
                envelope(
                    rows,
                    aggregates={
                        "totalDebit": round2(sum((r["debit"] for r in rows), ZERO)),
                        "totalCredit": round2(sum((r["credit"] for r in rows), ZERO)),
                    },
                )
            )
        if report_key == "gst-summary":
            return Response(reports.gst_summary(request.client_id, date_from, date_to))
        return Response(
            reports.ageing(
                request.client_id, params.get("type") or "receivable", as_of
            )
        )
