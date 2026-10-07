"""HRMS endpoints (api.md §11)."""
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.audit import notify, record_audit
from apps.core.exceptions import (
    BusinessRuleViolation,
    Codes,
    Conflict,
    NotFound,
    PermissionDenied,
    ValidationFailed,
)
from apps.core.money import ZERO, D, round2
from apps.core.numbering import allocate_number
from apps.core.pagination import envelope
from apps.core.permissions import HasModulePermission, has_permission
from apps.core.printing import PdfNotAvailable, print_payload
from apps.core.viewsets import ReadOnlyTenantViewSet, TenantModelViewSet

from . import services, user_link
from .models import (
    Appraisal,
    AppraisalCycle,
    AppraisalHistory,
    ApprovalChain,
    Application,
    Asset,
    AssetAssignment,
    AssetCategory,
    AssetRequest,
    Attendance,
    AttendancePunch,
    AttendanceRegularization,
    CalendarEvent,
    Candidate,
    Complaint,
    CompOff,
    Department,
    Designation,
    Employee,
    Goal,
    Holiday,
    HrDocument,
    Interview,
    Job,
    Kpi,
    LeaveBalance,
    LeaveEncashment,
    LeaveRequest,
    LeaveType,
    Location,
    Offer,
    OnboardingTask,
    PayrollRun,
    Payslip,
    PerformanceIndicator,
    Policy,
    PolicyAcknowledgement,
    PolicyCategory,
    PolicyVersion,
    Resignation,
    SalaryAdvance,
    SalaryStructure,
    ScreeningQuestion,
    ScreeningAnswer,
    Team,
    Termination,
    Trainer,
    Training,
    TrainingParticipant,
    WorkingDay,
    EmployeeTransfer,
    EmployeePromotion,
    EmployeeWarning,
    EmployeeAward,
    TravelRequest,
    Announcement,
)
from .serializers import (
    AppraisalCycleSerializer,
    AppraisalSerializer,
    ApprovalChainSerializer,
    ApplicationSerializer,
    AssetCategorySerializer,
    AssetRequestSerializer,
    AssetSerializer,
    AttendancePunchSerializer,
    AttendanceSerializer,
    BulkAttendanceSerializer,
    CalendarEventSerializer,
    CandidateSerializer,
    ComplaintSerializer,
    CompOffSerializer,
    DepartmentSerializer,
    DesignationSerializer,
    EmployeeSerializer,
    GoalSerializer,
    HolidaySerializer,
    HrDocumentSerializer,
    InterviewSerializer,
    JobSerializer,
    KpiSerializer,
    LeaveBalanceSerializer,
    LeaveEncashmentSerializer,
    LeaveRequestSerializer,
    LeaveTypeSerializer,
    LocationSerializer,
    OfferSerializer,
    OnboardingTaskSerializer,
    PayrollRunSerializer,
    PayslipSerializer,
    PerformanceIndicatorSerializer,
    PolicyAcknowledgementSerializer,
    PolicyCategorySerializer,
    PolicySerializer,
    ProcessPayrollSerializer,
    RegularizationSerializer,
    ResignationSerializer,
    SalaryAdvanceSerializer,
    SalaryStructureSerializer,
    ScreeningQuestionSerializer,
    ScreeningAnswerSerializer,
    TeamSerializer,
    TerminationSerializer,
    TrainerSerializer,
    TrainingParticipantSerializer,
    TrainingSerializer,
    WorkingDaySerializer,
    EmployeeTransferSerializer,
    EmployeePromotionSerializer,
    EmployeeWarningSerializer,
    EmployeeAwardSerializer,
    TravelRequestSerializer,
    AnnouncementSerializer,
)


#: Leave and pay screens serve both the employee (own rows) and the approver
#: (team rows, via OwnEmployeeScopeMixin). Requiring only the self-service id
#: locked HR Manager -- who approves but does not apply -- out of the queue.
LEAVE_READERS = ("apply_leave", "approve_leave")
PAY_READERS = ("view_own_payslip", "generate_payroll", "approve_payroll")

class OwnEmployeeScopeMixin:
    """Record-level scope for self-service HR data.

    The permission map decides whether a screen can be used at all; this
    decides whose rows it shows. Holders of any of ``team_scope_permissions``
    see the tenant; everyone else sees -- and may write -- only rows for the
    employee linked to their own user. ``PayslipViewSet`` applies the same
    rule by hand.
    """

    team_scope_permissions = ()

    def has_team_scope(self):
        return has_permission(self.request.user, tuple(self.team_scope_permissions))

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.has_team_scope():
            return queryset
        employee_id = getattr(self.request.user, "employee_id", None)
        return queryset.filter(employee_id=employee_id) if employee_id else queryset.none()

    def check_own_employee(self, employee):
        if self.has_team_scope():
            return
        resolved = None
        if hasattr(self, "_resolve_employee"):
            resolved = self._resolve_employee(self.request)
        own = resolved.pk if resolved else getattr(self.request.user, "employee_id", None)
        if employee is None or own is None or employee.pk != own:
            raise PermissionDenied(
                "You can only do this for your own employee record.",
                code=self.team_scope_permissions[0] if self.team_scope_permissions else "PERMISSION_DENIED",
            )

    def _resolve_employee(self, request, employee_id=None):
        client_id = getattr(request, "client_id", None) or getattr(request.user, "client_id", None)
        if employee_id:
            try:
                emp = Employee.objects.filter(
                    pk=employee_id, client_id=client_id, deleted_at__isnull=True
                ).first()
                if emp:
                    return emp
            except (ValueError, TypeError, Exception):
                pass
            return Employee.objects.filter(
                employee_code=employee_id, client_id=client_id, deleted_at__isnull=True
            ).first()

        if getattr(request.user, "employee", None):
            return request.user.employee

        if getattr(request.user, "email", None):
            emp = Employee.objects.filter(
                client_id=client_id, email__iexact=request.user.email, deleted_at__isnull=True
            ).first()
            if emp:
                if not getattr(request.user, "employee_id", None) and hasattr(request.user, "save"):
                    request.user.employee = emp
                    request.user.save(update_fields=["employee"])
                return emp

        if getattr(request.user, "name", None):
            emp = Employee.objects.filter(
                client_id=client_id, name__iexact=request.user.name, deleted_at__isnull=True
            ).first()
            if emp:
                if not getattr(request.user, "employee_id", None) and hasattr(request.user, "save"):
                    request.user.employee = emp
                    request.user.save(update_fields=["employee"])
                return emp

        return None

# ---------------------------------------------------------------------------
# Organisation (api.md §11.1)
# ---------------------------------------------------------------------------
class EmployeeViewSet(TenantModelViewSet):
    queryset = Employee.objects.select_related(
        "designation", "department", "manager", "location", "salary_structure"
    )
    serializer_class = EmployeeSerializer
    audit_entity_type = "Employee"
    audit_label_field = "employee_code"
    status_field = "status"
    search_fields = ["name", "employee_code", "email", "phone"]
    ordering = ["name"]
    default_date_field = "joining_date"
    filter_map = {
        "department": "department__name",
        "departmentId": "department_id",
        "designation": "designation__name",
        "location": "location__name",
        "managerId": "manager_id",
        "employmentType": "employment_type",
    }
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"], "create": ["create_staff"]}

    def get_aggregates(self, queryset):
        return queryset.aggregate(
            total=Count("id"),
            active=Count("id", filter=Q(status="Active")),
            onLeave=Count("id", filter=Q(status="On Leave")),
            probation=Count("id", filter=Q(status="Probation")),
            exited=Count("id", filter=Q(status__in=["Resigned", "Terminated"])),
        )

    def get_queryset(self):
        # The linked Administration login, for the directory's "Login" column
        # (users.employee_id is the only pointer, so it is read as a join).
        from django.db.models import OuterRef, Subquery

        from apps.accounts.models import User

        logins = User.objects.filter(
            employee=OuterRef("pk"), deleted_at__isnull=True
        ).exclude(status="Deleted").order_by("created_at")
        return super().get_queryset().annotate(
            linked_user_id=Subquery(logins.values("id")[:1]),
            linked_user_email=Subquery(logins.values("email")[:1]),
            linked_user_status=Subquery(logins.values("status")[:1]),
            linked_user_role=Subquery(logins.values("role__name")[:1]),
        )

    @transaction.atomic
    def perform_create(self, serializer):
        serializer.validated_data["employee_code"] = allocate_number(
            self.request.user.client, "EMP"
        )
        employee = super().perform_create(serializer)

        # api.md §11.1 -- Connect employee with Administration user account
        if employee.email:
            from apps.accounts.invites import send_invite_on_commit
            from apps.accounts.models import User, Role
            client_id = self.get_client_id()
            normalized_email = employee.email.strip().lower()

            existing_user = User.objects.filter(
                client_id=client_id, email=normalized_email,
                deleted_at__isnull=True,
            ).first()

            if existing_user:
                if not user_link.linked_employee(existing_user):
                    existing_user.employee = employee
                    existing_user.save(update_fields=["employee", "updated_at"])
                    user_link.reconcile(existing_user, employee)
                    self.write_audit(
                        "link_user", employee,
                        description=f"Linked user account {existing_user.email} to employee {employee.employee_code}",
                    )
            elif self.request.data.get("createUserAccount", True):
                # No default password -- without one the login is unusable
                # until the employee activates it from the invite email.
                emp_password = (
                    self.request.data.get("password")
                    or self.request.data.get("userPassword")
                    or None
                )
                role_id = self.request.data.get("roleId")
                emp_role = None
                if role_id:
                    emp_role = Role.objects.filter(client_id=client_id, pk=role_id).first()
                if not emp_role:
                    emp_role = Role.objects.filter(client_id=client_id, code__in=["EM", "EMP"]).first()

                user = User.objects.create_user(
                    email=normalized_email,
                    password=emp_password,
                    client_id=client_id,
                    name=employee.name,
                    phone=employee.phone,
                    employee=employee,
                    department=employee.department.name if employee.department_id else None,
                    role=emp_role,
                    status="Active",
                )
                # Location, joining date, photo and manager follow the HR record.
                user_link.reconcile(user, employee)
                send_invite_on_commit(user)
                self.write_audit(
                    "provision_user", employee,
                    description=f"User account created for {user.email}",
                )
        return employee

    @transaction.atomic
    def perform_update(self, serializer):
        before = user_link.capture(serializer.instance, user_link.EMPLOYEE_FIELDS)
        employee = super().perform_update(serializer)
        changed = user_link.changed_fields(employee, before, user_link.EMPLOYEE_FIELDS)
        if changed:
            user_link.push_employee_to_user(employee, changed)
        return employee

    @transaction.atomic
    def perform_destroy(self, instance):
        super().perform_destroy(instance)
        user_link.unlink_employee(instance)

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def terminate(self, request, pk=None):
        employee = self.get_object()
        if employee.status == "Terminated":
            raise Conflict("This employee is already terminated.", code=Codes.ALREADY_DONE)

        reason = request.data.get("reason")
        last_working_day = request.data.get("lastWorkingDay") or timezone.localdate()

        employee.status = "Terminated"
        employee.termination_reason = reason
        employee.last_working_day = last_working_day
        employee.save(
            update_fields=["status", "termination_reason", "last_working_day", "updated_at"]
        )

        Termination.objects.create(
            client_id=request.client_id,
            employee=employee,
            reason=reason,
            last_working_day=last_working_day,
            status="Completed",
            created_by=request.user,
        )
        # A terminated employee must not keep an active login.
        from apps.accounts.models import User, UserSession

        users = User.objects.filter(employee=employee, deleted_at__isnull=True)
        users.update(status="Inactive", is_active=False)
        UserSession.objects.filter(user__in=users, revoked_at__isnull=True).update(
            revoked_at=timezone.now(), revoked_reason="employee_terminated"
        )

        self.write_audit("terminate", employee, description=reason)
        return Response(self.get_serializer(employee).data)

    @action(detail=True, methods=["get"])
    def documents(self, request, pk=None):
        employee = self.get_object()
        rows = HrDocument.objects.filter(
            client_id=request.client_id, employee=employee, deleted_at__isnull=True
        )
        return Response(envelope(HrDocumentSerializer(rows, many=True).data))

    @action(detail=True, methods=["get"])
    def assets(self, request, pk=None):
        employee = self.get_object()
        rows = Asset.objects.filter(
            client_id=request.client_id, assigned_employee=employee, deleted_at__isnull=True
        )
        return Response(envelope(AssetSerializer(rows, many=True).data))


def _live_employees(prefix="employees"):
    return Q(**{f"{prefix}__deleted_at__isnull": True})


def _refuse_if_staffed(label, count, what="employee"):
    """An org unit still in use is not deleted out from under its people."""
    if count:
        raise Conflict(
            f"{count} {what}{'s' if count != 1 else ''} still use{'s' if count == 1 else ''} this {label}.",
            code=Codes.IN_USE,
            detail=f"Move them to another {label} first.",
        )


class HrmsDepartmentViewSet(TenantModelViewSet):
    queryset = Department.objects.select_related("head_employee", "parent")
    serializer_class = DepartmentSerializer
    audit_entity_type = "HrmsDepartment"
    audit_label_field = "name"
    status_field = "status"
    search_fields = ["name", "code"]
    ordering = ["name"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def get_queryset(self):
        from django.db.models import IntegerField, OuterRef, Subquery, Sum
        from django.db.models.functions import Coalesce

        open_jobs = (
            Job.objects.filter(department=OuterRef("pk"), status="Open", deleted_at__isnull=True)
            .values("department")
            .annotate(total=Sum("openings"))
            .values("total")
        )
        return super().get_queryset().annotate(
            employee_count=Count("employees", filter=_live_employees(), distinct=True),
            team_count=Count("teams", filter=Q(teams__deleted_at__isnull=True), distinct=True),
            open_roles=Coalesce(Subquery(open_jobs, output_field=IntegerField()), 0),
        )

    def check_delete_allowed(self, department):
        _refuse_if_staffed(
            "department", department.employees.filter(deleted_at__isnull=True).count()
        )
        _refuse_if_staffed(
            "department",
            department.designations.filter(deleted_at__isnull=True).count(),
            what="designation",
        )


class DesignationViewSet(TenantModelViewSet):
    queryset = Designation.objects.select_related("department")
    serializer_class = DesignationSerializer
    audit_entity_type = "Designation"
    audit_label_field = "name"
    status_field = None
    search_fields = ["name"]
    ordering = ["name"]
    filter_map = {"departmentId": "department_id"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def get_queryset(self):
        return super().get_queryset().annotate(
            employee_count=Count("employees", filter=_live_employees(), distinct=True),
        )

    def check_delete_allowed(self, designation):
        _refuse_if_staffed(
            "designation", designation.employees.filter(deleted_at__isnull=True).count()
        )


class HrmsLocationViewSet(TenantModelViewSet):
    queryset = Location.objects.all()
    serializer_class = LocationSerializer
    audit_entity_type = "HrmsLocation"
    audit_label_field = "name"
    status_field = None
    search_fields = ["name", "timezone"]
    ordering = ["name"]
    filter_map = {"type": "location_type"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def get_queryset(self):
        return super().get_queryset().annotate(
            employee_count=Count("employees", filter=_live_employees(), distinct=True),
        )

    def check_delete_allowed(self, location):
        _refuse_if_staffed(
            "location", location.employees.filter(deleted_at__isnull=True).count()
        )


class OrgChartView(APIView):
    permission_classes = [HasModulePermission]
    required_permissions = ["view_staff"]

    def get(self, request):
        from . import org_structure

        # ``tree`` is the plain reporting-line walk; the rest is the
        # Department -> Head -> reporting hierarchy the Org Chart draws.
        return Response({
            "tree": services.org_chart(request.client_id),
            **org_structure.build(request.client_id),
        })


class HrmsDashboardView(APIView):
    permission_classes = [HasModulePermission]
    required_permissions = ["show_hrm_dashboard"]

    def get(self, request):
        client_id = request.client_id
        today = timezone.localdate()
        employees = Employee.objects.filter(client_id=client_id, deleted_at__isnull=True)

        attendance_today = Attendance.objects.filter(
            client_id=client_id, work_date=today, deleted_at__isnull=True
        ).values("status").annotate(count=Count("id"))

        upcoming = CalendarEvent.objects.filter(
            client_id=client_id,
            deleted_at__isnull=True,
            starts_at__gte=timezone.now(),
            starts_at__lte=timezone.now() + timedelta(days=30),
        ).order_by("starts_at")[:10]

        exits = employees.filter(
            status__in=["Resigned", "Terminated"],
            last_working_day__gte=today - timedelta(days=365),
        ).count()
        headcount = employees.filter(status__in=["Active", "On Leave", "Probation"]).count()

        return Response(
            {
                "headcount": headcount,
                "newJoinersThisMonth": employees.filter(
                    joining_date__year=today.year, joining_date__month=today.month
                ).count(),
                "attritionRatePct": round(exits / headcount * 100, 1) if headcount else 0,
                "onLeaveToday": Attendance.objects.filter(
                    client_id=client_id, work_date=today, status="On Leave",
                    deleted_at__isnull=True,
                ).count(),
                "attendanceToday": {
                    row["status"]: row["count"] for row in attendance_today
                },
                "pendingLeaveRequests": LeaveRequest.objects.filter(
                    client_id=client_id,
                    status__in=["Pending Review", "Delegate Confirmed"],
                    deleted_at__isnull=True,
                ).count(),
                "openPositions": Job.objects.filter(
                    client_id=client_id, status="Open", deleted_at__isnull=True
                ).count(),
                "upcomingEvents": CalendarEventSerializer(upcoming, many=True).data,
            }
        )


# ---------------------------------------------------------------------------
# Attendance (api.md §11.2)
# ---------------------------------------------------------------------------
class AttendanceViewSet(OwnEmployeeScopeMixin, TenantModelViewSet):
    queryset = Attendance.objects.select_related("employee", "employee__department")
    serializer_class = AttendanceSerializer
    audit_entity_type = "Attendance"
    status_field = "status"
    default_date_field = "work_date"
    allowed_date_fields = ("work_date",)
    ordering = ["-work_date"]
    search_fields = ["employee__name", "employee__employee_code"]
    filter_map = {
        "employeeId": "employee_id",
        "department": "employee__department__name",
        "date": "work_date",
    }
    permission_map = {
        "read": ["view_team_attendance"],
        "write": ["mark_attendance"],
        # Marking a whole day for many people is a team action.
        "bulk": ["mark_attendance", "view_team_attendance"],
        "today": [],
        "punch": [],
        "correct_punch": ["regularize_attendance", "mark_attendance"],
        "punches": [],
        "punch_timeline": [],
    }
    # Without it, `mark_attendance` covers the caller's own row only.
    team_scope_permissions = ("view_team_attendance",)

    def get_aggregates(self, queryset):
        return queryset.aggregate(
            total=Count("id"),
            present=Count("id", filter=Q(status="Present")),
            absent=Count("id", filter=Q(status="Absent")),
            late=Count("id", filter=Q(status="Late")),
            onLeave=Count("id", filter=Q(status="On Leave")),
            wfh=Count("id", filter=Q(status="WFH")),
        )

    def perform_create(self, serializer):
        data = serializer.validated_data
        self.check_own_employee(data.get("employee"))
        row = services.mark_attendance(
            client=self.request.user.client,
            employee=data["employee"],
            work_date=data["work_date"],
            check_in=data.get("check_in"),
            check_out=data.get("check_out"),
            status=data.get("status"),
            remark=data.get("remark"),
            source="manual",
            user=self.request.user,
        )
        serializer.instance = row
        self._created_instance = row
        self._concurrency_instance = row
        return row

    def perform_update(self, serializer):
        data = serializer.validated_data
        instance = serializer.instance
        self.check_own_employee(data.get("employee", instance.employee))
        row = services.mark_attendance(
            client=self.request.user.client,
            employee=data.get("employee", instance.employee),
            work_date=data.get("work_date", instance.work_date),
            check_in=data.get("check_in", instance.check_in),
            check_out=data.get("check_out", instance.check_out),
            status=data.get("status", instance.status),
            remark=data.get("remark", instance.remark),
            source="manual",
            user=self.request.user,
        )
        serializer.instance = row
        self._concurrency_instance = row
        return row

    @action(detail=False, methods=["post"])
    @transaction.atomic
    def bulk(self, request):
        """``{ date, records: [] }`` or ``{ ids: [], status }`` (api.md §11.2)."""
        serializer = BulkAttendanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if data.get("ids") and data.get("status"):
            updated = Attendance.objects.filter(
                client_id=request.client_id, pk__in=data["ids"], deleted_at__isnull=True
            ).update(status=data["status"], source="bulk")
            return Response({"updated": updated})

        work_date = data.get("date") or timezone.localdate()
        written = 0
        for record in data.get("records", []):
            employee_id = record.get("employeeId") or record.get("employee_id") or record.get("id")
            employee = None
            try:
                employee = Employee.objects.filter(
                    pk=employee_id, client_id=request.client_id, deleted_at__isnull=True
                ).first()
            except (ValueError, TypeError, Exception):
                employee = None
            if employee is None and employee_id:
                employee = Employee.objects.filter(
                    employee_code=employee_id, client_id=request.client_id, deleted_at__isnull=True
                ).first()
            if employee is None:
                continue
            services.mark_attendance(
                client=request.user.client,
                employee=employee,
                work_date=record.get("date") or work_date,
                check_in=record.get("checkIn"),
                check_out=record.get("checkOut"),
                status=record.get("status"),
                remark=record.get("remark"),
                source="bulk",
                user=request.user,
            )
            written += 1
        return Response({"written": written, "date": work_date})

    @action(detail=False, methods=["get"])
    def summary(self, request):
        month = request.query_params.get("month")
        parsed = None
        if month:
            try:
                parsed = datetime.strptime(month[:7], "%Y-%m").date()
            except ValueError:
                raise ValidationFailed(
                    "Month must be YYYY-MM.", field_errors={"month": ["Expected YYYY-MM."]}
                )
        rows = services.attendance_summary(
            request.client_id,
            month=parsed,
            employee_id=request.query_params.get("employeeId"),
            department_id=request.query_params.get("departmentId"),
        )
        return Response(envelope(rows))

    @action(detail=False, methods=["get"], url_path=r"individual/(?P<employee_id>[^/.]+)")
    def individual(self, request, employee_id=None):
        rows = Attendance.objects.filter(
            client_id=request.client_id, employee_id=employee_id, deleted_at__isnull=True
        ).order_by("work_date")
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        if date_from:
            rows = rows.filter(work_date__gte=date_from)
        if date_to:
            rows = rows.filter(work_date__lte=date_to)
        return Response(envelope(AttendanceSerializer(rows, many=True).data))

    @action(detail=False, methods=["get"])
    def audit(self, request):
        """``GET /hrms/attendance/audit/`` -- the manual-edit trail (db.md §11.2)."""
        from apps.core.models import AuditLog
        from apps.core.serializers_platform import AuditLogSerializer

        rows = AuditLog.objects.filter(
            client_id=request.client_id,
            entity_type="Attendance",
            action__in=["attendance_edit", "attendance_mark"],
        )[:200]
        return Response(envelope(AuditLogSerializer(rows, many=True).data))

    def _resolve_employee(self, request, employee_id=None):
        if employee_id:
            try:
                emp = Employee.objects.filter(
                    pk=employee_id, client_id=request.client_id, deleted_at__isnull=True
                ).first()
                if emp:
                    return emp
            except (ValueError, TypeError, Exception):
                pass
            return Employee.objects.filter(
                employee_code=employee_id, client_id=request.client_id, deleted_at__isnull=True
            ).first()

        # Check request.user.employee
        if getattr(request.user, "employee", None):
            return request.user.employee

        # Fallback to match user email with employee email
        if getattr(request.user, "email", None):
            emp = Employee.objects.filter(
                client_id=request.client_id, email__iexact=request.user.email, deleted_at__isnull=True
            ).first()
            if emp:
                if not getattr(request.user, "employee_id", None) and hasattr(request.user, "save"):
                    request.user.employee = emp
                    request.user.save(update_fields=["employee"])
                return emp

        # Fallback to match user name with employee name
        if getattr(request.user, "name", None):
            emp = Employee.objects.filter(
                client_id=request.client_id, name__iexact=request.user.name, deleted_at__isnull=True
            ).first()
            if emp:
                if not getattr(request.user, "employee_id", None) and hasattr(request.user, "save"):
                    request.user.employee = emp
                    request.user.save(update_fields=["employee"])
                return emp

        return None

    @action(detail=False, methods=["get"])
    def today(self, request):
        """``GET /hrms/attendance/today/`` -- punch state for current employee today."""
        employee_id = request.query_params.get("employeeId")
        employee = self._resolve_employee(request, employee_id)
        if not employee:
            return Response({
                "has_employee": False,
                "status": "Not Punched In",
                "is_punched_in": False,
                "punches": [],
                "formatted_working_time": "00h 00m",
                "working_seconds": 0,
                "working_hours": 0.0,
                "late_minutes": 0,
                "overtime_hours": 0.0,
                "first_punch": None,
                "last_punch": None,
                "date": timezone.localdate().isoformat(),
            })
        status_data = services.get_today_punch_status(request.client_id, employee)
        return Response(status_data)

    @action(detail=False, methods=["post"])
    def punch(self, request):
        """``POST /hrms/attendance/punch/`` -- record Punch In or Punch Out."""
        punch_type = (
            request.data.get("punch_type")
            or request.data.get("punchType")
            or request.data.get("type")
        )
        if not punch_type:
            raise ValidationFailed(
                "Punch type ('IN' or 'OUT') is required.",
                field_errors={"punch_type": ["Required."]},
            )

        employee = self._resolve_employee(
            request, request.data.get("employeeId") or request.data.get("employee_id")
        )
        if not employee:
            raise ValidationFailed(
                "No employee profile linked to current user account.",
                code="NO_EMPLOYEE",
            )

        # Self-service scope: non-managers can only punch for their own employee
        if not self.has_team_scope():
            self.check_own_employee(employee)

        remark = request.data.get("remark") or request.data.get("notes")
        source = request.data.get("source") or "web"
        early_reason = (
            request.data.get("earlyReason")
            or request.data.get("early_reason")
            or request.data.get("reason")
        )
        request_regularization = bool(
            request.data.get("requestRegularization")
            or request.data.get("request_regularization")
            or request.data.get("createRegularization")
        )

        punch_obj, status_data = services.record_punch(
            client=request.user.client,
            employee=employee,
            punch_type=punch_type,
            source=source,
            remark=remark,
            early_reason=early_reason,
            request_regularization=request_regularization,
            user=request.user,
        )
        return Response(
            status_data,
            status=status.HTTP_201_CREATED if punch_obj else status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="correct-punch")
    def correct_punch(self, request):
        """``POST /hrms/attendance/correct-punch/`` -- HR/Admin manual punch correction."""
        if not has_permission(request.user, ("regularize_attendance", "mark_attendance")):
            raise PermissionDenied("You do not have permission to correct punches.")

        employee_id = request.data.get("employeeId") or request.data.get("employee_id")
        employee = self._resolve_employee(request, employee_id)
        if not employee:
            raise ValidationFailed(
                "Employee not found.", field_errors={"employeeId": ["Invalid employee."]}
            )

        punch_type = request.data.get("punchType") or request.data.get("punch_type")
        work_date_str = request.data.get("date") or request.data.get("work_date")
        punch_time_str = request.data.get("punchTime") or request.data.get("punch_time")
        reason = request.data.get("reason") or "Manual correction"

        if not work_date_str or not punch_time_str or not punch_type:
            raise ValidationFailed("Date, punch time, and punch type are required.")

        try:
            work_date = datetime.strptime(str(work_date_str)[:10], "%Y-%m-%d").date()
        except ValueError:
            raise ValidationFailed("Invalid date format. Expected YYYY-MM-DD.")

        if "T" in str(punch_time_str):
            from django.utils.dateparse import parse_datetime
            dt = parse_datetime(punch_time_str)
            if dt and not timezone.is_aware(dt):
                dt = timezone.make_aware(dt)
        else:
            time_part = datetime.strptime(str(punch_time_str)[:5], "%H:%M").time()
            dt = timezone.make_aware(datetime.combine(work_date, time_part))

        punch_obj, status_data = services.correct_punch(
            client=request.user.client,
            employee=employee,
            work_date=work_date,
            punch_type=punch_type,
            punch_time=dt,
            reason=reason,
            user=request.user,
        )
        return Response(status_data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def punches(self, request, pk=None):
        """``GET /hrms/attendance/<id>/punches/`` -- punch timeline for attendance row."""
        att = self.get_object()
        punches_qs = att.punches.all().order_by("punch_time")
        return Response(AttendancePunchSerializer(punches_qs, many=True).data)

    @action(detail=False, methods=["get"], url_path="punch-timeline")
    def punch_timeline(self, request):
        """``GET /hrms/attendance/punch-timeline/?employeeId=...&date=...``."""
        employee_id = request.query_params.get("employeeId")
        date_str = request.query_params.get("date") or request.query_params.get("work_date")
        employee = self._resolve_employee(request, employee_id)
        if not employee:
            return Response([])

        if date_str:
            try:
                work_date = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
            except ValueError:
                work_date = timezone.localdate()
        else:
            work_date = timezone.localdate()

        punches = AttendancePunch.objects.filter(
            client_id=request.client_id,
            employee=employee,
            work_date=work_date,
            deleted_at__isnull=True,
        ).order_by("punch_time")
        return Response(AttendancePunchSerializer(punches, many=True).data)


class RegularizationViewSet(OwnEmployeeScopeMixin, TenantModelViewSet):
    queryset = AttendanceRegularization.objects.select_related("employee")
    serializer_class = RegularizationSerializer
    audit_entity_type = "AttendanceRegularization"
    status_field = "status"
    default_date_field = "work_date"
    ordering = ["-work_date"]
    filter_map = {"employeeId": "employee_id"}
    team_scope_permissions = ("view_team_attendance",)
    permission_map = {"read": [], "create": [], "write": ["regularize_attendance"]}

    def perform_create(self, serializer):
        employee = serializer.validated_data.get("employee")
        if not employee:
            resolved = self._resolve_employee(self.request)
            if resolved:
                serializer.validated_data["employee"] = resolved
                employee = resolved
        self.check_own_employee(employee)
        return super().perform_create(serializer)

    @transaction.atomic
    def perform_update(self, serializer):
        """``{ approved, remark }`` -- an approval rewrites the attendance row."""
        approved = self.request.data.get("approved")
        instance = serializer.instance
        row = super().perform_update(serializer)

        if approved is not None:
            row.status = "Approved" if approved else "Rejected"
            row.approver = self.request.user
            row.decided_at = timezone.now()
            row.remark = self.request.data.get("remark", row.remark)
            row.save(update_fields=["status", "approver", "decided_at", "remark", "updated_at"])

            if approved:
                services.mark_attendance(
                    client=self.request.user.client,
                    employee=row.employee,
                    work_date=row.work_date,
                    check_in=row.requested_check_in,
                    check_out=row.requested_check_out,
                    status=row.requested_status,
                    remark=f"Regularized: {row.reason}",
                    source="regularization",
                    user=self.request.user,
                )
        return row


class FlexibilityPolicyView(APIView):
    """``GET/PUT /hrms/attendance/flexibility/``."""

    permission_classes = [HasModulePermission]
    permission_map = {"read": ["view_team_attendance"], "write": ["mark_attendance"]}

    def get(self, request):
        return Response(services.flexibility_policy(request.client_id))

    def put(self, request):
        from apps.core.models import Setting

        policy = services.flexibility_policy(request.client_id)
        policy.update(request.data if isinstance(request.data, dict) else {})
        Setting.objects.update_or_create(
            client_id=request.client_id,
            key="attendance_flexibility",
            defaults={"value": policy, "updated_by": request.user},
        )
        return Response(policy)


# ---------------------------------------------------------------------------
# Leave (api.md §11.3)
# ---------------------------------------------------------------------------
class LeaveTypeViewSet(TenantModelViewSet):
    queryset = LeaveType.objects.all()
    serializer_class = LeaveTypeSerializer
    audit_entity_type = "LeaveType"
    audit_label_field = "name"
    status_field = None
    ordering = ["name"]
    permission_map = {"read": [LEAVE_READERS], "write": ["approve_leave"]}


class LeaveRequestViewSet(OwnEmployeeScopeMixin, TenantModelViewSet):
    queryset = LeaveRequest.objects.select_related(
        "employee", "leave_type", "delegate_employee"
    )
    serializer_class = LeaveRequestSerializer
    audit_entity_type = "LeaveRequest"
    status_field = "status"
    default_date_field = "from_date"
    ordering = ["-from_date"]
    search_fields = ["employee__name", "reason"]
    filter_map = {"employeeId": "employee_id", "type": "leave_type__name", "leaveTypeId": "leave_type_id"}
    team_scope_permissions = ("approve_leave",)
    permission_map = {"read": [LEAVE_READERS], "create": ["apply_leave"], "write": ["approve_leave"]}

    def get_aggregates(self, queryset):
        return queryset.aggregate(
            total=Count("id"),
            pending=Count("id", filter=Q(status__in=["Pending Review", "Delegate Confirmed"])),
            approved=Count("id", filter=Q(status="Approved")),
            rejected=Count("id", filter=Q(status="Rejected")),
        )

    def perform_create(self, serializer):
        self.check_own_employee(serializer.validated_data.get("employee"))
        request_row = super().perform_create(serializer)
        services.assert_no_overlap(request_row)
        return request_row

    @transaction.atomic
    def perform_update(self, serializer):
        """``{ approved, remark }`` or ``{ status }`` (api.md §11.3)."""
        approved = self.request.data.get("approved")
        target_status = self.request.data.get("status")
        instance = serializer.instance

        if approved is True or target_status == "Approved":
            from apps.core.permissions import require_permission

            require_permission(self.request.user, "approve_leave")
            row = services.approve_leave(
                instance, user=self.request.user, remark=self.request.data.get("remark")
            )
            self._concurrency_instance = row
            self.write_audit("approve", row, description="Leave approved")
            return row

        if approved is False or target_status == "Rejected":
            from apps.core.permissions import require_permission

            require_permission(self.request.user, "approve_leave")
            instance.status = "Rejected"
            instance.approver = self.request.user
            instance.decided_at = timezone.now()
            instance.remark = self.request.data.get("remark")
            instance.save()
            self._concurrency_instance = instance
            self.write_audit("reject", instance, description=instance.remark)
            return instance

        return super().perform_update(serializer)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        row = services.cancel_leave(
            self.get_object(), user=request.user, reason=request.data.get("reason")
        )
        self.write_audit("cancel", row, description=request.data.get("reason"))
        return Response(self.get_serializer(row).data)

    @action(detail=True, methods=["post"], url_path="confirm-delegate")
    def confirm_delegate(self, request, pk=None):
        row = self.get_object()
        row.delegate_confirmed_at = timezone.now()
        row.status = "Delegate Confirmed"
        row.save(update_fields=["delegate_confirmed_at", "status", "updated_at"])
        return Response(self.get_serializer(row).data)

    @action(detail=False, methods=["post"], url_path="calculate-days")
    def calculate_days(self, request):
        """Calculates days preview with sandwich-leave detection (Spec §2.5.2)."""
        import datetime
        from datetime import timedelta
        from .models import Holiday, LeaveType

        from_date_str = request.data.get("fromDate") or request.data.get("from_date")
        to_date_str = request.data.get("toDate") or request.data.get("to_date")
        leave_type_id = request.data.get("leaveTypeId") or request.data.get("leave_type_id")

        if not from_date_str or not to_date_str:
            return Response({"error": "fromDate and toDate are required"}, status=400)

        start = datetime.date.fromisoformat(str(from_date_str)[:10])
        end = datetime.date.fromisoformat(str(to_date_str)[:10])
        if end < start:
            return Response({"error": "toDate must be on or after fromDate"}, status=400)

        leave_type = LeaveType.objects.filter(id=leave_type_id, client_id=request.client_id).first() if leave_type_id else None
        enforce_sandwich = bool(leave_type and leave_type.enforce_sandwich_rule)

        holidays = set(Holiday.objects.filter(
            client_id=request.client_id,
            date__gte=start,
            date__lte=end
        ).values_list("date", flat=True))

        total_calendar_days = (end - start).days + 1
        day_breakdown = []
        working_days = 0
        sandwich_days = 0

        curr = start
        while curr <= end:
            is_weekend = curr.weekday() in (5, 6)
            is_holiday = curr in holidays
            is_off = is_weekend or is_holiday
            if not is_off:
                working_days += 1
                counted = True
            else:
                counted = enforce_sandwich
                if enforce_sandwich:
                    sandwich_days += 1

            day_breakdown.append({
                "date": curr.isoformat(),
                "day_of_week": curr.strftime("%A"),
                "is_weekend": is_weekend,
                "is_holiday": is_holiday,
                "is_counted": counted
            })
            curr += timedelta(days=1)

        counted_days = total_calendar_days if enforce_sandwich else working_days

        return Response({
            "enforce_sandwich_rule": enforce_sandwich,
            "total_calendar_days": total_calendar_days,
            "working_days": working_days,
            "sandwich_penalty_days": sandwich_days if enforce_sandwich else 0,
            "chargeable_days": counted_days,
            "day_breakdown": day_breakdown
        })



class LeaveBalanceViewSet(OwnEmployeeScopeMixin, ReadOnlyTenantViewSet):
    queryset = LeaveBalance.objects.select_related("employee", "leave_type")
    serializer_class = LeaveBalanceSerializer
    status_field = None
    filter_map = {"employeeId": "employee_id", "leaveTypeId": "leave_type_id", "year": "period_year"}
    ordering = ["employee__name"]
    team_scope_permissions = ("approve_leave",)
    permission_map = {"read": [LEAVE_READERS]}


class CompOffViewSet(OwnEmployeeScopeMixin, TenantModelViewSet):
    queryset = CompOff.objects.select_related("employee")
    serializer_class = CompOffSerializer
    audit_entity_type = "CompOff"
    status_field = None
    default_date_field = "worked_date"
    ordering = ["-worked_date"]
    filter_map = {"employeeId": "employee_id", "used": "used"}
    team_scope_permissions = ("approve_leave",)
    permission_map = {"read": [LEAVE_READERS], "write": ["approve_leave"]}


class LeaveEncashmentViewSet(OwnEmployeeScopeMixin, TenantModelViewSet):
    queryset = LeaveEncashment.objects.select_related("employee", "leave_type")
    serializer_class = LeaveEncashmentSerializer
    audit_entity_type = "LeaveEncashment"
    status_field = "status"
    ordering = ["-created_at"]
    filter_map = {"employeeId": "employee_id"}
    team_scope_permissions = ("approve_leave",)
    permission_map = {"read": [LEAVE_READERS], "write": ["approve_leave"]}


# ---------------------------------------------------------------------------
# Payroll (api.md §11.4)
# ---------------------------------------------------------------------------
class PayslipViewSet(TenantModelViewSet):
    queryset = Payslip.objects.select_related(
        "employee", "employee__department", "employee__designation", "bank_account"
    ).prefetch_related("components")
    serializer_class = PayslipSerializer
    audit_entity_type = "Payslip"
    status_field = "status"
    default_date_field = "period_month"
    ordering = ["-period_month", "employee__name"]
    search_fields = ["employee__name", "employee__employee_code"]
    filter_map = {
        "employeeId": "employee_id",
        "department": "employee__department__name",
        "month": "period_month",
    }
    permission_map = {
        "read": [PAY_READERS],
        "write": ["generate_payroll"],
        "approve": ["approve_payroll"],
        "mark_paid": ["approve_payroll"],
        # Tenant-wide totals.
        "summary": [("generate_payroll", "approve_payroll")],
    }
    http_method_names = ["get", "patch", "post", "head", "options"]

    def get_queryset(self):
        queryset = super().get_queryset()
        # Without the team-wide permission, a user sees only their own payslips.
        if not has_permission(self.request.user, "generate_payroll") and not has_permission(
            self.request.user, "approve_payroll"
        ):
            employee_id = getattr(self.request.user, "employee_id", None)
            queryset = queryset.filter(employee_id=employee_id) if employee_id else queryset.none()
        return queryset

    def get_aggregates(self, queryset):
        user = self.request.user
        if has_permission(user, ("generate_payroll", "approve_payroll")):
            return services.payroll_summary(self.get_client_id())
        # Totals of the caller's own payslips, not the company payroll.
        return services.payroll_summary(self.get_client_id(), queryset=queryset)

    def perform_update(self, serializer):
        """Adjust earnings / deductions before approval (api.md §11.4)."""
        payslip = super().perform_update(serializer)
        override = self.request.data.get("earnedSalary")
        services.recompute_payslip(
            payslip,
            earned_salary_override=override if override is not None else None,
            user=self.request.user,
        )
        payslip.refresh_from_db()
        return payslip

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        payslip = self.get_object()
        if payslip.status == "Paid":
            raise Conflict("This payslip is already paid.", code=Codes.ALREADY_DONE)
        payslip.status = "Approved"
        payslip.save(update_fields=["status", "updated_at"])
        self.write_audit("approve", payslip, description="Payslip approved")
        return Response(self.get_serializer(payslip).data)

    @action(detail=True, methods=["post"], url_path="mark-paid")
    def mark_paid(self, request, pk=None):
        from apps.accounting.models import BankAccount

        bank_account = None
        bank_account_id = request.data.get("bankAccountId")
        if bank_account_id:
            bank_account = BankAccount.objects.filter(
                pk=bank_account_id, client_id=request.client_id, deleted_at__isnull=True
            ).first()

        payslip = services.mark_payslip_paid(
            self.get_object(),
            payment_date=request.data.get("paymentDate"),
            bank_account=bank_account,
            user=request.user,
        )
        self.write_audit("mark_paid", payslip, description="Payslip marked paid")
        return Response(self.get_serializer(payslip).data)

    @action(detail=True, methods=["get"])
    def payslip(self, request, pk=None):
        row = self.get_object()
        return Response(
            print_payload(row, self.get_serializer_class(), request=request, title="Payslip")
        )

    @action(detail=False, methods=["post"])
    def process(self, request):
        """``POST /hrms/payroll/process/`` -- generate the run."""
        from apps.core.permissions import require_permission

        require_permission(request.user, "generate_payroll")

        serializer = ProcessPayrollSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = services.process_payroll(
            client=request.user.client,
            period_month=serializer.validated_data["month"],
            employee_ids=serializer.validated_data.get("employeeIds") or None,
            user=request.user,
        )
        return Response(
            {
                "run": PayrollRunSerializer(result["run"]).data,
                "payslips": PayslipSerializer(result["payslips"], many=True).data,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["get"])
    def summary(self, request):
        month = request.query_params.get("month")
        parsed = None
        if month:
            try:
                parsed = datetime.strptime(month[:7], "%Y-%m").date()
            except ValueError:
                raise ValidationFailed(
                    "Month must be YYYY-MM.", field_errors={"month": ["Expected YYYY-MM."]}
                )
        return Response(services.payroll_summary(request.client_id, parsed))


class SalaryStructureViewSet(TenantModelViewSet):
    queryset = SalaryStructure.objects.all()
    serializer_class = SalaryStructureSerializer
    audit_entity_type = "SalaryStructure"
    audit_label_field = "name"
    status_field = None
    ordering = ["name"]
    permission_map = {"read": [PAY_READERS], "write": ["edit_salary_structure"]}


class SalaryAdvanceViewSet(OwnEmployeeScopeMixin, TenantModelViewSet):
    queryset = SalaryAdvance.objects.select_related("employee")
    serializer_class = SalaryAdvanceSerializer
    audit_entity_type = "SalaryAdvance"
    status_field = "status"
    default_date_field = "issued_on"
    ordering = ["-issued_on"]
    filter_map = {"employeeId": "employee_id"}
    # Same rule as payslips: team-wide pay needs a payroll permission.
    team_scope_permissions = ("generate_payroll", "approve_payroll")
    permission_map = {"read": [PAY_READERS], "write": ["generate_payroll"]}


# ---------------------------------------------------------------------------
# Recruitment (api.md §11.5)
# ---------------------------------------------------------------------------
class JobViewSet(TenantModelViewSet):
    queryset = Job.objects.select_related("department", "designation", "location")
    serializer_class = JobSerializer
    audit_entity_type = "Job"
    audit_label_field = "title"
    status_field = "status"
    search_fields = ["title", "description"]
    ordering = ["-created_at"]
    filter_map = {"departmentId": "department_id", "isPublished": "is_published"}
    permission_map = {"read": ["view_staff"], "write": ["create_staff"]}

    def get_queryset(self):
        return super().get_queryset().annotate(
            applicant_count=Count("applications", distinct=True)
        )

    def perform_create(self, serializer):
        from django.utils.text import slugify

        job = super().perform_create(serializer)
        if not job.slug:
            base = slugify(job.title)[:120] or "role"
            slug, suffix = base, 1
            while Job.objects.filter(
                client_id=self.get_client_id(), slug=slug, deleted_at__isnull=True
            ).exclude(pk=job.pk).exists():
                suffix += 1
                slug = f"{base}-{suffix}"
            job.slug = slug
            job.save(update_fields=["slug"])
        return job

    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        job = self.get_object()
        job.is_published = True
        job.published_at = timezone.now()
        job.save(update_fields=["is_published", "published_at", "updated_at"])
        return Response(self.get_serializer(job).data)


class CandidateViewSet(TenantModelViewSet):
    queryset = Candidate.objects.select_related("resume_file", "employee")
    serializer_class = CandidateSerializer
    audit_entity_type = "Candidate"
    audit_label_field = "name"
    status_field = "stage"
    search_fields = ["name", "email", "phone"]
    ordering = ["-created_at"]
    filter_map = {"stage": "stage", "source": "source"}
    permission_map = {"read": ["view_staff"], "write": ["create_staff"]}


class ApplicationViewSet(TenantModelViewSet):
    queryset = Application.objects.select_related("candidate", "job")
    serializer_class = ApplicationSerializer
    audit_entity_type = "Application"
    status_field = "stage"
    ordering = ["-applied_at"]
    filter_map = {"jobId": "job_id", "candidateId": "candidate_id"}
    permission_map = {"read": ["view_staff"], "write": ["create_staff"]}


class InterviewViewSet(TenantModelViewSet):
    queryset = Interview.objects.select_related("application", "application__candidate")
    serializer_class = InterviewSerializer
    audit_entity_type = "Interview"
    status_field = "status"
    ordering = ["scheduled_at"]
    filter_map = {"applicationId": "application_id"}
    permission_map = {"read": ["view_staff"], "write": ["create_staff"]}

    @action(detail=True, methods=["post"], url_path="generate-zoom-meeting")
    def generate_zoom_meeting(self, request, pk=None):
        """Generates or attaches a Zoom meeting link to the interview (Spec §2.5.5)."""
        import random
        interview = self.get_object()
        meeting_id = f"{random.randint(100, 999)} {random.randint(1000, 9999)} {random.randint(1000, 9999)}"
        passcode = "".join(random.choices("0123456789abcdef", k=6))
        zoom_link = f"https://zoom.us/j/{meeting_id.replace(' ', '')}?pwd={passcode}"

        interview.location = zoom_link
        note_text = f"Zoom Meeting ID: {meeting_id} | Passcode: {passcode} | Link: {zoom_link}"
        if interview.notes:
            interview.notes = f"{interview.notes}\n{note_text}"
        else:
            interview.notes = note_text
        interview.save(update_fields=["location", "notes", "updated_at"])

        return Response({
            "zoom_link": zoom_link,
            "meeting_id": meeting_id,
            "passcode": passcode,
            "interview": self.get_serializer(interview).data
        })



class OfferViewSet(TenantModelViewSet):
    queryset = Offer.objects.select_related("application", "application__candidate")
    serializer_class = OfferSerializer
    audit_entity_type = "Offer"
    status_field = "status"
    ordering = ["-created_at"]
    filter_map = {"applicationId": "application_id"}
    permission_map = {"read": ["view_staff"], "write": ["create_staff"]}

    def perform_update(self, serializer):
        previous = serializer.instance.status
        offer = super().perform_update(serializer)
        if previous != offer.status:
            if offer.status == "Sent" and offer.sent_at is None:
                offer.sent_at = timezone.now()
            if offer.status in ("Accepted", "Declined") and offer.responded_at is None:
                offer.responded_at = timezone.now()
            offer.save(update_fields=["sent_at", "responded_at", "updated_at"])

            if offer.status == "Accepted":
                candidate = offer.application.candidate
                candidate.stage = "Offer"
                candidate.save(update_fields=["stage", "updated_at"])
        return offer

    @action(detail=True, methods=["get"])
    def letter(self, request, pk=None):
        offer = self.get_object()
        return Response(
            print_payload(
                offer, self.get_serializer_class(), request=request, title="Offer Letter"
            )
        )


class OnboardingViewSet(TenantModelViewSet):
    queryset = OnboardingTask.objects.select_related("candidate", "owner")
    serializer_class = OnboardingTaskSerializer
    audit_entity_type = "OnboardingTask"
    status_field = None
    ordering = ["due_date"]
    filter_map = {"candidateId": "candidate_id"}
    permission_map = {"read": ["view_staff"], "write": ["create_staff"]}

    @action(
        detail=False, methods=["post"], url_path=r"(?P<candidate_id>[^/.]+)/complete"
    )
    @transaction.atomic
    def complete(self, request, candidate_id=None):
        """Convert candidate -> employee, keeping the hiring trail (db.md §11.5)."""
        candidate = Candidate.objects.filter(
            pk=candidate_id, client_id=request.client_id, deleted_at__isnull=True
        ).first()
        if candidate is None:
            raise NotFound("That candidate no longer exists.")
        if candidate.employee_id:
            raise Conflict(
                "This candidate has already been onboarded.",
                code=Codes.ALREADY_DONE,
                payload={"employeeId": str(candidate.employee_id)},
            )

        offer = (
            Offer.objects.filter(
                application__candidate=candidate, status="Accepted", deleted_at__isnull=True
            )
            .order_by("-created_at")
            .first()
        )
        application = (
            Application.objects.filter(candidate=candidate, deleted_at__isnull=True)
            .select_related("job")
            .order_by("-applied_at")
            .first()
        )

        employee = Employee.objects.create(
            client_id=request.client_id,
            employee_code=allocate_number(request.user.client, "EMP"),
            name=candidate.name,
            email=candidate.email,
            phone=candidate.phone,
            joining_date=(
                offer.joining_date if offer and offer.joining_date else timezone.localdate()
            ),
            department=application.job.department if application else None,
            designation=application.job.designation if application else None,
            location=application.job.location if application else None,
            employment_type=application.job.employment_type if application else None,
            standard_salary=(offer.offered_ctc / 12) if offer and offer.offered_ctc else ZERO,
            status="Probation",
            created_by=request.user,
        )
        candidate.employee = employee
        candidate.stage = "Hired"
        candidate.save(update_fields=["employee", "stage", "updated_at"])

        if employee.email:
            from apps.accounts.models import User

            login = User.objects.filter(
                client_id=request.client_id, email=employee.email.strip().lower(),
                deleted_at__isnull=True,
            ).first()
            if login is not None and not user_link.linked_employee(login):
                login.employee = employee
                login.save(update_fields=["employee", "updated_at"])
                user_link.reconcile(login, employee)

        record_audit(
            client=request.client_id,
            actor=request.user,
            action="onboard",
            entity_type="Employee",
            entity_id=employee.id,
            entity_label=employee.employee_code,
            description=f"Onboarded from candidate {candidate.name}",
        )
        return Response(
            EmployeeSerializer(employee).data, status=status.HTTP_201_CREATED
        )

    @action(
        detail=False,
        methods=["post"],
        url_path=r"(?P<candidate_id>[^/.]+)/verify-documents",
    )
    def verify_documents(self, request, candidate_id=None):
        candidate = Candidate.objects.filter(
            pk=candidate_id, client_id=request.client_id, deleted_at__isnull=True
        ).first()
        if candidate is None:
            raise NotFound("That candidate no longer exists.")
        OnboardingTask.objects.filter(
            client_id=request.client_id, candidate=candidate,
            title__icontains="document", completed_at__isnull=True,
        ).update(completed_at=timezone.now())
        return Response({"verified": True})


# Hidden: Screening Questions out of scope (Sweven spec) -- restore by uncommenting this block.
# class ScreeningQuestionViewSet(TenantModelViewSet):
#     queryset = ScreeningQuestion.objects.select_related("job")
#     serializer_class = ScreeningQuestionSerializer
#     audit_entity_type = "ScreeningQuestion"
#     status_field = None
#     ordering = ["sort_order"]
#     filter_map = {"jobId": "job_id", "isActive": "is_active"}
#     permission_map = {"read": ["view_staff"], "write": ["create_staff"]}


class RecruitmentFunnelView(APIView):
    permission_classes = [HasModulePermission]
    required_permissions = ["view_staff"]

    def get(self, request, section=None):
        client_id = request.client_id
        applications = Application.objects.filter(
            client_id=client_id, deleted_at__isnull=True
        )
        rows = applications.values("stage").annotate(count=Count("id"))
        funnel = {row["stage"]: row["count"] for row in rows}

        if section == "funnel":
            return Response(
                envelope(
                    [
                        {"stage": stage, "count": funnel.get(stage, 0)}
                        for stage in [
                            "Applied", "Screening", "Interview", "Offer", "Hired", "Rejected",
                        ]
                    ]
                )
            )

        jobs = Job.objects.filter(client_id=client_id, deleted_at__isnull=True)
        return Response(
            {
                "openJobs": jobs.filter(status="Open").count(),
                "totalApplications": applications.count(),
                "interviewsScheduled": Interview.objects.filter(
                    client_id=client_id, status="Scheduled", deleted_at__isnull=True
                ).count(),
                "offersOut": Offer.objects.filter(
                    client_id=client_id, status="Sent", deleted_at__isnull=True
                ).count(),
                "hired": funnel.get("Hired", 0),
                "funnel": funnel,
            }
        )


# ---------------------------------------------------------------------------
# Performance (api.md §11.6)
# ---------------------------------------------------------------------------
class AppraisalCycleViewSet(TenantModelViewSet):
    queryset = AppraisalCycle.objects.all()
    serializer_class = AppraisalCycleSerializer
    audit_entity_type = "AppraisalCycle"
    audit_label_field = "name"
    status_field = "status"
    ordering = ["-period_start"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class PerformanceIndicatorViewSet(TenantModelViewSet):
    queryset = PerformanceIndicator.objects.select_related("department")
    serializer_class = PerformanceIndicatorSerializer
    audit_entity_type = "PerformanceIndicator"
    audit_label_field = "name"
    status_field = None
    ordering = ["name"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class KpiViewSet(TenantModelViewSet):
    queryset = Kpi.objects.select_related("indicator")
    serializer_class = KpiSerializer
    audit_entity_type = "Kpi"
    audit_label_field = "name"
    status_field = None
    ordering = ["name"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class AppraisalViewSet(TenantModelViewSet):
    queryset = Appraisal.objects.select_related(
        "cycle", "employee", "manager"
    ).prefetch_related("kpi_scores__kpi", "history__actor")
    serializer_class = AppraisalSerializer
    audit_entity_type = "Appraisal"
    status_field = "status"
    ordering = ["-created_at"]
    filter_map = {"cycleId": "cycle_id", "employeeId": "employee_id", "stage": "stage"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def _transition(self, appraisal, *, to_stage, to_status, action_name, comment=None,
                    require=None):
        """Every transition appends to ``history[]`` with actor, stage, timestamp
        and comment (api.md §11.6)."""
        if require and not has_permission(self.request.user, require):
            raise PermissionDenied(
                "You don't have permission to do that.", code=require
            )

        from_stage = appraisal.stage
        appraisal.stage = to_stage
        appraisal.status = to_status
        appraisal.save(update_fields=["stage", "status", "updated_at"])

        AppraisalHistory.objects.create(
            client_id=appraisal.client_id,
            appraisal=appraisal,
            from_stage=from_stage,
            to_stage=to_stage,
            actor=self.request.user,
            action=action_name,
            comment=comment,
        )
        return appraisal

    @action(detail=True, methods=["post"], url_path="self-review")
    def self_review(self, request, pk=None):
        appraisal = self.get_object()
        appraisal.self_rating = request.data.get("rating", appraisal.self_rating)
        appraisal.self_comments = request.data.get("comments", appraisal.self_comments)
        appraisal.strengths = request.data.get("strengths", appraisal.strengths)
        appraisal.areas_for_improvement = request.data.get(
            "areasForImprovement", appraisal.areas_for_improvement
        )
        appraisal.save()
        self._transition(
            appraisal, to_stage="Manager Review", to_status="Submitted",
            action_name="self_review_submitted",
            comment=request.data.get("comments"),
        )
        return Response(self.get_serializer(appraisal).data)

    @action(detail=True, methods=["post"], url_path="manager-review")
    def manager_review(self, request, pk=None):
        appraisal = self.get_object()
        appraisal.manager_rating = request.data.get("rating", appraisal.manager_rating)
        appraisal.development_feedback = request.data.get(
            "developmentFeedback", appraisal.development_feedback
        )
        appraisal.save()

        from .models import AppraisalKpiScore

        for score in request.data.get("kpiScores") or []:
            kpi_id = score.get("kpiId")
            if not kpi_id:
                continue
            AppraisalKpiScore.objects.update_or_create(
                appraisal=appraisal,
                kpi_id=kpi_id,
                defaults={
                    "client_id": appraisal.client_id,
                    "target": score.get("target"),
                    "achieved": score.get("achieved"),
                    "score": score.get("score"),
                    "weight": score.get("weight"),
                },
            )

        self._transition(
            appraisal, to_stage="HR Review", to_status="In Progress",
            action_name="manager_review_submitted",
            comment=request.data.get("comments"),
        )
        return Response(self.get_serializer(appraisal).data)

    @action(detail=True, methods=["post"], url_path="return")
    def return_to_stage(self, request, pk=None):
        appraisal = self.get_object()
        target = request.data.get("returnToStage") or "Self Review"
        self._transition(
            appraisal, to_stage=target, to_status="Returned",
            action_name="returned", comment=request.data.get("reason"),
        )
        return Response(self.get_serializer(appraisal).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        appraisal = self.get_object()
        appraisal.hr_comments = request.data.get("hrComments", appraisal.hr_comments)
        appraisal.save(update_fields=["hr_comments", "updated_at"])
        self._transition(
            appraisal, to_stage="Finalization", to_status="Approved",
            action_name="hr_approved", comment=request.data.get("hrComments"),
            require="edit_staff",
        )
        return Response(self.get_serializer(appraisal).data)

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def finalize(self, request, pk=None):
        """Close and sync the rating to the profile (api.md §11.6)."""
        appraisal = self.get_object()
        appraisal.final_rating = request.data.get(
            "finalRating", appraisal.manager_rating or appraisal.self_rating
        )
        appraisal.save(update_fields=["final_rating", "updated_at"])
        self._transition(
            appraisal, to_stage="Finalization", to_status="Completed",
            action_name="finalized", require="edit_staff",
        )
        return Response(self.get_serializer(appraisal).data)

    @action(detail=True, methods=["get"])
    def history(self, request, pk=None):
        from .serializers import AppraisalHistorySerializer

        appraisal = self.get_object()
        return Response(
            envelope(
                AppraisalHistorySerializer(appraisal.history.all(), many=True).data
            )
        )


class GoalViewSet(TenantModelViewSet):
    queryset = Goal.objects.select_related("employee", "cycle")
    serializer_class = GoalSerializer
    audit_entity_type = "Goal"
    audit_label_field = "title"
    status_field = "status"
    ordering = ["target_date"]
    filter_map = {"employeeId": "employee_id", "cycleId": "cycle_id"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class PerformanceDashboardView(APIView):
    permission_classes = [HasModulePermission]
    required_permissions = ["view_staff"]

    def get(self, request):
        appraisals = Appraisal.objects.filter(
            client_id=request.client_id, deleted_at__isnull=True
        )
        by_stage = appraisals.values("stage").annotate(count=Count("id"))
        by_status = appraisals.values("status").annotate(count=Count("id"))
        return Response(
            {
                "total": appraisals.count(),
                "completed": appraisals.filter(status="Completed").count(),
                "byStage": {row["stage"]: row["count"] for row in by_stage},
                "byStatus": {row["status"]: row["count"] for row in by_status},
                "goalsOnTrack": Goal.objects.filter(
                    client_id=request.client_id, deleted_at__isnull=True,
                    progress_pct__gte=50,
                ).count(),
            }
        )


# ---------------------------------------------------------------------------
# Training (api.md §11.7)
# ---------------------------------------------------------------------------
class TrainerViewSet(TenantModelViewSet):
    queryset = Trainer.objects.select_related("employee")
    serializer_class = TrainerSerializer
    audit_entity_type = "Trainer"
    audit_label_field = "name"
    status_field = None
    ordering = ["name"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class TrainingViewSet(TenantModelViewSet):
    queryset = Training.objects.select_related("trainer", "department").prefetch_related(
        "participants__employee"
    )
    serializer_class = TrainingSerializer
    audit_entity_type = "Training"
    audit_label_field = "title"
    status_field = "stage"
    default_date_field = "start_date"
    ordering = ["-start_date"]
    search_fields = ["title", "description", "venue"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    @action(detail=True, methods=["post"], url_path="assign-trainer")
    def assign_trainer(self, request, pk=None):
        training = self.get_object()
        trainer_id = request.data.get("trainerId")
        trainer = Trainer.objects.filter(
            pk=trainer_id, client_id=request.client_id, deleted_at__isnull=True
        ).first()
        if trainer is None:
            raise ValidationFailed(
                "Unknown trainer.", field_errors={"trainerId": ["Not found."]}
            )
        training.trainer = trainer
        training.type = request.data.get("trainerType") or trainer.kind
        if training.stage == "Requested":
            training.stage = "Trainer Assigned"
        training.save(update_fields=["trainer", "type", "stage", "updated_at"])
        return Response(self.get_serializer(training).data)

    @action(detail=True, methods=["post"])
    def participants(self, request, pk=None):
        training = self.get_object()
        employee_ids = request.data.get("employeeIds") or []
        rows = TrainingParticipant.objects.bulk_create(
            [
                TrainingParticipant(
                    client_id=request.client_id, training=training, employee_id=employee_id
                )
                for employee_id in employee_ids
            ],
            ignore_conflicts=True,
        )
        return Response({"enrolled": len(rows)}, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def evaluate(self, request, pk=None):
        training = self.get_object()
        participant = training.participants.filter(
            pk=request.data.get("participantId"), deleted_at__isnull=True
        ).first()
        if participant is None:
            raise NotFound("That participant is not on this training.")
        participant.rating = request.data.get("rating")
        participant.feedback = request.data.get("feedback")
        participant.evaluated_at = timezone.now()
        participant.save()

        if not training.participants.filter(evaluated_at__isnull=True).exists():
            training.stage = "Evaluated"
            training.save(update_fields=["stage", "updated_at"])
        return Response(TrainingParticipantSerializer(participant).data)

    @action(detail=False, methods=["get"])
    def funnel(self, request):
        rows = self.get_queryset().values("stage").annotate(count=Count("id"))
        return Response(
            envelope([{"stage": row["stage"], "count": row["count"]} for row in rows])
        )

    @action(detail=False, methods=["get"])
    def dashboard(self, request):
        queryset = self.get_queryset()
        return Response(
            {
                "total": queryset.count(),
                "ongoing": queryset.filter(stage="Ongoing").count(),
                "completed": queryset.filter(stage__in=["Completed", "Evaluated"]).count(),
                "participants": TrainingParticipant.objects.filter(
                    client_id=request.client_id, deleted_at__isnull=True
                ).count(),
            }
        )


# ---------------------------------------------------------------------------
# Assets, documents, policies, calendar, HR admin (api.md §11.8)
# ---------------------------------------------------------------------------
class AssetViewSet(TenantModelViewSet):
    queryset = Asset.objects.select_related(
        "category", "assigned_employee", "assigned_employee__department", "location"
    ).prefetch_related("history__employee")
    serializer_class = AssetSerializer
    audit_entity_type = "Asset"
    audit_label_field = "asset_code"
    status_field = "status"
    search_fields = ["name", "asset_code", "serial_number"]
    ordering = ["name"]
    filter_map = {"categoryId": "category_id", "employeeId": "assigned_employee_id"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def perform_create(self, serializer):
        if not serializer.validated_data.get("asset_code"):
            serializer.validated_data["asset_code"] = allocate_number(
                self.request.user.client, "ASSET"
            )
        return super().perform_create(serializer)

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def assign(self, request, pk=None):
        asset = self.get_object()
        employee = Employee.objects.filter(
            pk=request.data.get("employeeId"), client_id=request.client_id,
            deleted_at__isnull=True,
        ).first()
        if employee is None:
            raise ValidationFailed(
                "Unknown employee.", field_errors={"employeeId": ["Not found."]}
            )
        if asset.history.filter(returned_at__isnull=True).exists():
            raise Conflict(
                "This asset is already assigned. Record a return first.",
                code=Codes.IN_USE,
            )

        AssetAssignment.objects.create(
            client_id=request.client_id,
            asset=asset,
            employee=employee,
            assigned_by=request.user,
            notes=request.data.get("notes"),
        )
        asset.assigned_employee = employee
        asset.status = "Assigned"
        asset.save(update_fields=["assigned_employee", "status", "updated_at"])
        self.write_audit("assign", asset, description=f"Assigned to {employee.name}")
        return Response(self.get_serializer(asset).data)

    @action(detail=True, methods=["post"], url_path="return")
    @transaction.atomic
    def return_asset(self, request, pk=None):
        asset = self.get_object()
        assignment = asset.history.filter(returned_at__isnull=True).first()
        if assignment is None:
            raise Conflict("This asset is not currently assigned.", code=Codes.BAD_TARGET)

        assignment.returned_at = timezone.now()
        assignment.returned_condition = request.data.get("condition")
        assignment.notes = request.data.get("notes", assignment.notes)
        assignment.save()

        asset.assigned_employee = None
        asset.condition = request.data.get("condition") or asset.condition
        asset.status = (
            "Under Maintenance"
            if request.data.get("condition") == "Needs Repair"
            else "Available"
        )
        asset.save(update_fields=["assigned_employee", "condition", "status", "updated_at"])
        self.write_audit("return", asset, description=request.data.get("notes"))
        return Response(self.get_serializer(asset).data)


class AssetCategoryViewSet(TenantModelViewSet):
    queryset = AssetCategory.objects.all()
    serializer_class = AssetCategorySerializer
    audit_entity_type = "AssetCategory"
    audit_label_field = "name"
    status_field = None
    ordering = ["name"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class AssetRequestViewSet(TenantModelViewSet):
    queryset = AssetRequest.objects.select_related("employee", "category", "fulfilled_asset")
    serializer_class = AssetRequestSerializer
    audit_entity_type = "AssetRequest"
    status_field = "status"
    ordering = ["-created_at"]
    filter_map = {"employeeId": "employee_id"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class HrDocumentViewSet(TenantModelViewSet):
    queryset = HrDocument.objects.select_related("employee", "file", "created_by")
    serializer_class = HrDocumentSerializer
    audit_entity_type = "HrDocument"
    audit_label_field = "title"
    status_field = None
    search_fields = ["title", "description", "category"]
    ordering = ["-created_at"]
    filter_map = {"category": "category", "employeeId": "employee_id"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        # `status` is derived, so it is filtered after the query rather than in SQL.
        requested = self.request.query_params.getlist("status")
        if requested:
            today = timezone.localdate()
            if "Expired" in requested and len(requested) == 1:
                queryset = queryset.filter(valid_until__lt=today)
            elif "Valid" in requested and len(requested) == 1:
                queryset = queryset.filter(
                    Q(valid_until__isnull=True) | Q(valid_until__gte=today)
                )
        return queryset

    @action(detail=True, methods=["get"])
    def download(self, request, pk=None):
        from apps.core.files import public_url

        document = self.get_object()
        if document.file_id is None:
            raise NotFound("No file is attached to this document.")
        return Response({"downloadUrl": public_url(document.file, request)})


class PolicyViewSet(TenantModelViewSet):
    queryset = Policy.objects.select_related("category", "owner_department").prefetch_related(
        "version_history"
    )
    serializer_class = PolicySerializer
    audit_entity_type = "Policy"
    audit_label_field = "name"
    status_field = "status"
    search_fields = ["name", "summary"]
    ordering = ["name"]
    filter_map = {"categoryId": "category_id"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    @transaction.atomic
    def perform_update(self, serializer):
        """An edit bumps ``version`` and appends to ``versionHistory[]``."""
        instance = serializer.instance
        previous_body = instance.body
        policy = super().perform_update(serializer)

        if policy.body != previous_body:
            policy.version += 1
            policy.save(update_fields=["version", "updated_at"])
            PolicyVersion.objects.create(
                client_id=policy.client_id,
                policy=policy,
                version=policy.version,
                body=policy.body,
                file=policy.file,
                changed_by=self.request.user,
                change_note=self.request.data.get("changeNote"),
            )
        return policy

    @action(detail=True, methods=["post"], url_path="submit-for-approval")
    def submit_for_approval(self, request, pk=None):
        policy = self.get_object()
        policy.status = "Pending Approval"
        policy.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(policy).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        policy = self.get_object()
        policy.status = "Active"
        policy.approved_by = request.user
        policy.approved_at = timezone.now()
        policy.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])

        if policy.ack_required:
            employees = Employee.objects.filter(
                client_id=request.client_id, status="Active", deleted_at__isnull=True
            )
            PolicyAcknowledgement.objects.bulk_create(
                [
                    PolicyAcknowledgement(
                        client_id=request.client_id,
                        policy=policy,
                        policy_version=policy.version,
                        employee=employee,
                    )
                    for employee in employees
                ],
                ignore_conflicts=True,
            )
        self.write_audit("approve", policy, description="Policy approved")
        return Response(self.get_serializer(policy).data)

    @action(detail=True, methods=["post"])
    def archive(self, request, pk=None):
        policy = self.get_object()
        policy.status = "Archived"
        policy.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(policy).data)

    @action(detail=True, methods=["get"])
    def acknowledgements(self, request, pk=None):
        policy = self.get_object()
        rows = policy.acknowledgements.filter(deleted_at__isnull=True).select_related(
            "employee"
        )
        return Response(
            envelope(
                PolicyAcknowledgementSerializer(
                    rows, many=True, context={"policy": policy}
                ).data
            )
        )

    @action(detail=True, methods=["post"])
    def acknowledge(self, request, pk=None):
        policy = self.get_object()
        employee_id = getattr(request.user, "employee_id", None)
        if employee_id is None:
            raise BusinessRuleViolation(
                "Your account is not linked to an employee record.",
                code="NO_EMPLOYEE_RECORD",
            )
        row, _ = PolicyAcknowledgement.objects.get_or_create(
            client_id=request.client_id,
            policy=policy,
            policy_version=policy.version,
            employee_id=employee_id,
        )
        row.acknowledged_at = timezone.now()
        row.save(update_fields=["acknowledged_at", "updated_at"])
        return Response(
            PolicyAcknowledgementSerializer(row, context={"policy": policy}).data
        )


class PolicyCategoryViewSet(TenantModelViewSet):
    queryset = PolicyCategory.objects.all()
    serializer_class = PolicyCategorySerializer
    audit_entity_type = "PolicyCategory"
    audit_label_field = "name"
    status_field = None
    ordering = ["name"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class CalendarEventViewSet(TenantModelViewSet):
    queryset = CalendarEvent.objects.select_related("department")
    serializer_class = CalendarEventSerializer
    audit_entity_type = "CalendarEvent"
    audit_label_field = "title"
    status_field = None
    default_date_field = "starts_at"
    ordering = ["starts_at"]
    search_fields = ["title", "description", "location"]
    filter_map = {"type": "type", "dept": "department__name", "departmentId": "department_id"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        params = self.request.query_params
        if params.get("from"):
            queryset = queryset.filter(starts_at__gte=params["from"])
        if params.get("to"):
            queryset = queryset.filter(starts_at__lte=params["to"])
        return queryset

    def perform_destroy(self, instance):
        if instance.source_type == "LeaveRequest":
            raise Conflict(
                "Leave events are generated from the leave request.",
                code="GENERATED_EVENT",
                detail="Cancel the leave request instead.",
            )
        super().perform_destroy(instance)


class HolidayViewSet(TenantModelViewSet):
    queryset = Holiday.objects.select_related("location")
    serializer_class = HolidaySerializer
    audit_entity_type = "Holiday"
    audit_label_field = "name"
    status_field = None
    default_date_field = "date"
    ordering = ["date"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class WorkingDayView(APIView):
    """``GET/PUT /hrms/working-days/``."""

    permission_classes = [HasModulePermission]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def get(self, request):
        rows = WorkingDay.objects.filter(
            client_id=request.client_id, deleted_at__isnull=True
        ).order_by("weekday")
        return Response(envelope(WorkingDaySerializer(rows, many=True).data))

    @transaction.atomic
    def put(self, request):
        days = request.data.get("days") or request.data
        if not isinstance(days, list):
            raise ValidationFailed(
                "Provide a list of weekday configurations.",
                field_errors={"days": ["Expected a list."]},
            )
        for row in days:
            WorkingDay.objects.update_or_create(
                client_id=request.client_id,
                weekday=row.get("weekday"),
                location_id=row.get("locationId"),
                defaults={
                    "is_working": row.get("isWorking", True),
                    "shift_start": row.get("shiftStart"),
                    "shift_end": row.get("shiftEnd"),
                },
            )
        return self.get(request)


class TeamViewSet(TenantModelViewSet):
    queryset = Team.objects.select_related("department", "lead").prefetch_related("members")
    serializer_class = TeamSerializer
    audit_entity_type = "Team"
    audit_label_field = "name"
    status_field = "status"
    ordering = ["name"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class ApprovalChainViewSet(TenantModelViewSet):
    queryset = ApprovalChain.objects.all()
    serializer_class = ApprovalChainSerializer
    audit_entity_type = "ApprovalChain"
    audit_label_field = "name"
    status_field = "status"
    ordering = ["name"]
    filter_map = {"appliesTo": "applies_to"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class TerminationViewSet(TenantModelViewSet):
    queryset = Termination.objects.select_related("employee")
    serializer_class = TerminationSerializer
    audit_entity_type = "Termination"
    status_field = "status"
    ordering = ["-created_at"]
    filter_map = {"employeeId": "employee_id"}
    permission_map = {"read": ["view_staff"], "write": ["delete_staff"]}


class ResignationViewSet(TenantModelViewSet):
    queryset = Resignation.objects.select_related("employee").prefetch_related("checklist")
    serializer_class = ResignationSerializer
    audit_entity_type = "Resignation"
    status_field = "status"
    default_date_field = "submitted_on"
    ordering = ["-submitted_on"]
    filter_map = {"employeeId": "employee_id"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class ComplaintViewSet(TenantModelViewSet):
    """HR-only. db.md §11.8 asks for restricted access rather than relying on
    endpoint checks alone; this is the endpoint half."""

    queryset = Complaint.objects.select_related(
        "raised_by_employee", "against_employee", "assigned_to"
    )
    serializer_class = ComplaintSerializer
    audit_entity_type = "Complaint"
    status_field = "status"
    ordering = ["-created_at"]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def perform_create(self, serializer):
        if not serializer.validated_data.get("raised_by_employee"):
            employee_id = getattr(self.request.user, "employee_id", None)
            if employee_id:
                serializer.validated_data["raised_by_employee_id"] = employee_id
        return super().perform_create(serializer)

    def perform_update(self, serializer):
        complaint = super().perform_update(serializer)
        if complaint.status in ("Resolved", "Closed") and complaint.resolved_at is None:
            complaint.resolved_at = timezone.now()
            complaint.save(update_fields=["resolved_at", "updated_at"])
        return complaint


class HrmsSettingsView(APIView):
    """``GET/PUT /hrms/settings/`` -- the HRMS setup screen."""

    permission_classes = [HasModulePermission]
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def get(self, request):
        from apps.core.models import Setting

        row = Setting.objects.filter(client_id=request.client_id, key="hrms").first()
        return Response(row.value if row else {})

    def put(self, request):
        from apps.core.models import Setting

        row, _ = Setting.objects.update_or_create(
            client_id=request.client_id,
            key="hrms",
            defaults={"value": request.data, "updated_by": request.user},
        )
        return Response(row.value)


# ---------------------------------------------------------------------------
# Upgradation Scope: Transfers, Promotions, Warnings, Awards, Travel, Announcements
# ---------------------------------------------------------------------------
class EmployeeTransferViewSet(TenantModelViewSet):
    queryset = EmployeeTransfer.objects.select_related("employee", "from_department", "to_department", "from_location", "to_location")
    serializer_class = EmployeeTransferSerializer
    audit_entity_type = "EmployeeTransfer"
    audit_label_field = "transfer_number"
    status_field = "status"
    search_fields = ["transfer_number", "employee__name", "reason"]
    ordering = ["-effective_date", "-created_at"]
    filter_map = {"employeeId": "employee_id", "status": "status"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def perform_create(self, serializer):
        serializer.validated_data["transfer_number"] = allocate_number(self.request.user.client, "TRF")
        return super().perform_create(serializer)


class EmployeePromotionViewSet(TenantModelViewSet):
    queryset = EmployeePromotion.objects.select_related("employee", "from_designation", "to_designation")
    serializer_class = EmployeePromotionSerializer
    audit_entity_type = "EmployeePromotion"
    audit_label_field = "promotion_number"
    status_field = "status"
    search_fields = ["promotion_number", "employee__name", "justification"]
    ordering = ["-effective_date", "-created_at"]
    filter_map = {"employeeId": "employee_id", "status": "status"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def perform_create(self, serializer):
        serializer.validated_data["promotion_number"] = allocate_number(self.request.user.client, "PRM")
        return super().perform_create(serializer)


class EmployeeWarningViewSet(TenantModelViewSet):
    queryset = EmployeeWarning.objects.select_related("employee")
    serializer_class = EmployeeWarningSerializer
    audit_entity_type = "EmployeeWarning"
    audit_label_field = "warning_number"
    status_field = "status"
    search_fields = ["warning_number", "employee__name", "subject", "description"]
    ordering = ["-issue_date", "-created_at"]
    filter_map = {"employeeId": "employee_id", "status": "status", "severity": "severity"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def perform_create(self, serializer):
        serializer.validated_data["warning_number"] = allocate_number(self.request.user.client, "WRN")
        return super().perform_create(serializer)


class EmployeeAwardViewSet(TenantModelViewSet):
    queryset = EmployeeAward.objects.select_related("employee")
    serializer_class = EmployeeAwardSerializer
    audit_entity_type = "EmployeeAward"
    audit_label_field = "award_number"
    status_field = None
    search_fields = ["award_number", "employee__name", "award_name", "category"]
    ordering = ["-award_date", "-created_at"]
    filter_map = {"employeeId": "employee_id", "category": "category"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def perform_create(self, serializer):
        serializer.validated_data["award_number"] = allocate_number(self.request.user.client, "AWD")
        return super().perform_create(serializer)


class TravelRequestViewSet(TenantModelViewSet):
    queryset = TravelRequest.objects.select_related("employee")
    serializer_class = TravelRequestSerializer
    audit_entity_type = "TravelRequest"
    audit_label_field = "travel_number"
    status_field = "status"
    search_fields = ["travel_number", "employee__name", "destination", "purpose"]
    ordering = ["-start_date", "-created_at"]
    filter_map = {"employeeId": "employee_id", "status": "status"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}

    def perform_create(self, serializer):
        serializer.validated_data["travel_number"] = allocate_number(self.request.user.client, "TRV")
        return super().perform_create(serializer)


class AnnouncementViewSet(TenantModelViewSet):
    queryset = Announcement.objects.select_related("target_department")
    serializer_class = AnnouncementSerializer
    audit_entity_type = "Announcement"
    audit_label_field = "title"
    status_field = None
    search_fields = ["title", "content", "author_name"]
    ordering = ["-is_pinned", "-publish_date", "-created_at"]
    filter_map = {"targetDepartmentId": "target_department_id", "priority": "priority"}
    permission_map = {"read": ["view_staff"], "write": ["edit_staff"]}


class ScreeningQuestionViewSet(TenantModelViewSet):
    queryset = ScreeningQuestion.objects.select_related("job")
    serializer_class = ScreeningQuestionSerializer
    audit_entity_type = "ScreeningQuestion"
    status_field = None
    ordering = ["sort_order", "created_at"]
    filter_map = {"jobId": "job_id", "isActive": "is_active"}
    permission_map = {"read": ["view_staff"], "write": ["create_staff"]}


class ScreeningAnswerViewSet(TenantModelViewSet):
    queryset = ScreeningAnswer.objects.select_related("application", "question")
    serializer_class = ScreeningAnswerSerializer
    audit_entity_type = "ScreeningAnswer"
    status_field = None
    ordering = ["created_at"]
    filter_map = {"applicationId": "application_id", "questionId": "question_id"}
    permission_map = {"read": ["view_staff"], "write": ["create_staff"]}

