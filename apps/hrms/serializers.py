"""HRMS serializers (api.md §11)."""
from django.utils import timezone
from rest_framework import serializers

from apps.core.exceptions import ValidationFailed
from apps.core.serializers import (
    BaseModelSerializer,
    BaseSerializer,
    MoneyField,
    TenantPrimaryKeyRelatedField,
)
from apps.accounts.models import User

from . import services
from .models import (
    Appraisal,
    AppraisalCycle,
    AppraisalHistory,
    AppraisalKpiScore,
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
    PayslipComponent,
    PerformanceIndicator,
    Policy,
    PolicyAcknowledgement,
    PolicyCategory,
    PolicyVersion,
    Resignation,
    ResignationChecklistItem,
    SalaryAdvance,
    SalaryStructure,
    ScreeningAnswer,
    ScreeningQuestion,
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
    MeetingRoom,
    CompanyMeeting,
)


# ---------------------------------------------------------------------------
# Organisation (api.md §11.1)
# ---------------------------------------------------------------------------
class UniqueNameMixin:
    """Names are unique per tenant (a DB constraint); say so as a field error
    rather than letting the insert fail."""

    def validate_name(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("A name is required.")
        request = self.context.get("request")
        client_id = getattr(request, "client_id", None)
        taken = self.Meta.model.objects.filter(
            client_id=client_id, name__iexact=value, deleted_at__isnull=True
        )
        if self.instance is not None:
            taken = taken.exclude(pk=self.instance.pk)
        if client_id and taken.exists():
            raise serializers.ValidationError(f"'{value}' already exists.")
        return value


class DepartmentSerializer(UniqueNameMixin, BaseModelSerializer):
    head = serializers.CharField(source="head_employee.name", read_only=True)
    headAvatar = serializers.CharField(source="head_employee.avatar_url", read_only=True)
    headEmployeeId = TenantPrimaryKeyRelatedField(
        source="head_employee", model="hrms.Employee", required=False, allow_null=True
    )
    parentId = TenantPrimaryKeyRelatedField(
        source="parent", model="hrms.Department", required=False, allow_null=True
    )
    budget = serializers.DecimalField(
        max_digits=18, decimal_places=2, required=False, allow_null=True
    )
    #: Headcount, teams and open positions, annotated by the viewset.
    employees = serializers.SerializerMethodField()
    teams = serializers.SerializerMethodField()
    openRoles = serializers.SerializerMethodField()

    class Meta:
        model = Department
        fields = [
            "id", "name", "code", "head", "headAvatar", "headEmployeeId", "parent",
            "parentId", "status", "description", "budget", "employees", "teams",
            "openRoles", "created_at", "updated_at",
        ]
        read_only_fields = ["parent", "created_at", "updated_at"]

    def get_employees(self, department):
        return getattr(department, "employee_count", 0) or 0

    def get_teams(self, department):
        return getattr(department, "team_count", 0) or 0

    def get_openRoles(self, department):
        return getattr(department, "open_roles", 0) or 0

    def validate_headEmployeeId(self, head):
        # The Org Chart hangs the department's people under this person.
        if head is not None and head.status in ("Resigned", "Terminated"):
            raise serializers.ValidationError(f"{head.name} has left and cannot head a department.")
        return head

    def validate_parentId(self, parent):
        if parent is not None and self.instance is not None and parent.pk == self.instance.pk:
            raise serializers.ValidationError("A department cannot sit under itself.")
        return parent


class DesignationSerializer(UniqueNameMixin, BaseModelSerializer):
    department = serializers.CharField(source="department.name", read_only=True)
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )
    level = serializers.IntegerField(required=False, allow_null=True, min_value=1, max_value=7)
    employees = serializers.SerializerMethodField()

    class Meta:
        model = Designation
        fields = [
            "id", "name", "level", "department", "departmentId", "employees",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_employees(self, designation):
        return getattr(designation, "employee_count", 0) or 0


class LocationAddressField(serializers.Field):
    """The screens edit an address as one line; older rows hold a dict of
    parts. Read as text either way, stored as ``{"line": ...}``."""

    def to_representation(self, value):
        if isinstance(value, dict):
            if value.get("line"):
                return value["line"]
            parts = [value.get(k) for k in ("line1", "line2", "city", "state", "pincode", "country")]
            return ", ".join(str(p) for p in parts if p)
        return value or ""

    def to_internal_value(self, data):
        if isinstance(data, dict):
            return data
        text = str(data or "").strip()
        return {"line": text} if text else {}


class LocationSerializer(UniqueNameMixin, BaseModelSerializer):
    type = serializers.ChoiceField(
        source="location_type", choices=Location.TYPES, required=False
    )
    address = LocationAddressField(required=False)
    employees = serializers.SerializerMethodField()

    class Meta:
        model = Location
        fields = [
            "id", "name", "type", "address", "timezone", "employees",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_employees(self, location):
        return getattr(location, "employee_count", 0) or 0


class EmployeeSerializer(BaseModelSerializer):
    employeeCode = serializers.CharField(source="employee_code", read_only=True)
    designation = serializers.CharField(source="designation.name", read_only=True)
    designationId = TenantPrimaryKeyRelatedField(
        source="designation", model="hrms.Designation", required=False, allow_null=True
    )
    department = serializers.CharField(source="department.name", read_only=True)
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )
    manager = serializers.CharField(source="manager.name", read_only=True)
    managerId = TenantPrimaryKeyRelatedField(
        source="manager", model="hrms.Employee", required=False, allow_null=True
    )
    location = serializers.CharField(source="location.name", read_only=True)
    locationId = TenantPrimaryKeyRelatedField(
        source="location", model="hrms.Location", required=False, allow_null=True
    )
    joining = serializers.DateField(source="joining_date", required=False, allow_null=True)
    employmentType = serializers.CharField(
        source="employment_type", required=False, allow_null=True
    )
    salaryStructureId = TenantPrimaryKeyRelatedField(
        source="salary_structure", model="hrms.SalaryStructure",
        required=False, allow_null=True,
    )
    avatar = serializers.CharField(source="avatar_url", required=False, allow_null=True)
    #: The Administration login backed by this record (read-only; the link is
    #: made from either side's create, or from the user's "Linked employee").
    login = serializers.SerializerMethodField()

    class Meta:
        model = Employee
        fields = [
            "id", "employeeCode", "name", "email", "phone", "avatar", "login",
            "designation", "designationId", "department", "departmentId",
            "manager", "managerId", "location", "locationId", "joining",
            "employmentType", "shift", "salaryStructureId", "standard_salary",
            "status", "date_of_birth", "gender", "blood_group", "personal_email",
            "emergency_contact", "address", "bank_account_number", "ifsc_code",
            "pan", "uan", "aadhaar_last4", "last_working_day",
            "termination_reason", "created_at", "updated_at",
        ]
        read_only_fields = ["employeeCode", "created_at", "updated_at"]

    def get_login(self, employee):
        if hasattr(employee, "linked_user_id"):
            if not employee.linked_user_id:
                return None
            return {
                "id": str(employee.linked_user_id),
                "email": employee.linked_user_email,
                "status": employee.linked_user_status,
                "role": employee.linked_user_role,
            }
        # Rows not read through the viewset's annotated queryset (a create).
        from .user_link import linked_user

        user = linked_user(employee)
        if user is None:
            return None
        return {
            "id": str(user.id),
            "email": user.email,
            "status": user.status,
            "role": user.role.name if user.role_id else None,
        }

    #: Pay on the employee record. Only payroll managers see colleagues' pay;
    #: everyone still sees their own (`/hrms/employees/me/`).
    PAY_FIELDS = ("standard_salary", "salaryStructureId")

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user is None or getattr(user, "employee_id", None) == instance.pk:
            return data
        from apps.core.permissions import has_permission

        if not has_permission(user, ("generate_payroll", "approve_payroll", "edit_salary_structure")):
            for field in self.PAY_FIELDS:
                data.pop(field, None)
        return data

    def to_internal_value(self, data):
        if isinstance(data, dict):
            data = data.copy()
            if "joining" not in data and "joiningDate" in data:
                data["joining"] = data["joiningDate"]
            elif "joining" not in data and "doj" in data:
                data["joining"] = data["doj"]
        return super().to_internal_value(data)

    def validate(self, attrs):
        # Setting someone's pay is a payroll action, not a staff-record edit.
        pay_keys = {"standard_salary", "salary_structure"} & set(attrs)
        user = getattr(self.context.get("request"), "user", None)
        if pay_keys and user is not None:
            from apps.core.exceptions import PermissionDenied
            from apps.core.permissions import has_permission

            if not has_permission(user, ("generate_payroll", "approve_payroll", "edit_salary_structure")):
                raise PermissionDenied(
                    "Only payroll managers can change salary details.", code="generate_payroll"
                )

        if not attrs.get("joining_date") and not self.instance:
            attrs["joining_date"] = timezone.now().date()

        # Resolve department / designation / location if names were passed instead of IDs
        request = self.context.get("request")
        client_id = getattr(request, "client_id", None) or (
            getattr(getattr(request, "user", None), "client_id", None)
        )
        if client_id and hasattr(self, "initial_data") and isinstance(self.initial_data, dict):
            if not attrs.get("department") and self.initial_data.get("department"):
                d_name = str(self.initial_data["department"]).strip()
                if d_name and d_name != "All":
                    dept = Department.objects.filter(
                        client_id=client_id, name__iexact=d_name, deleted_at__isnull=True
                    ).first()
                    if not dept:
                        dept = Department.objects.create(client_id=client_id, name=d_name)
                    attrs["department"] = dept

            if not attrs.get("designation") and self.initial_data.get("designation"):
                des_name = str(self.initial_data["designation"]).strip()
                if des_name:
                    desig = Designation.objects.filter(
                        client_id=client_id, name__iexact=des_name, deleted_at__isnull=True
                    ).first()
                    if not desig:
                        desig = Designation.objects.create(client_id=client_id, name=des_name)
                    attrs["designation"] = desig

            if not attrs.get("location") and self.initial_data.get("location"):
                loc_name = str(self.initial_data["location"]).strip()
                if loc_name:
                    loc = Location.objects.filter(
                        client_id=client_id, name__iexact=loc_name, deleted_at__isnull=True
                    ).first()
                    if not loc:
                        loc = Location.objects.create(client_id=client_id, name=loc_name)
                    attrs["location"] = loc

        manager = attrs.get("manager")
        if manager is not None and manager.status in ("Resigned", "Terminated"):
            raise ValidationFailed(
                f"{manager.name} has left and cannot be a reporting manager.",
                field_errors={"managerId": ["Not an active employee."]},
            )
        if manager is not None and self.instance is not None:
            services.assert_no_manager_cycle(self.instance, manager.id)
        return attrs


# ---------------------------------------------------------------------------
# Attendance (api.md §11.2)
# ---------------------------------------------------------------------------
class AttendancePunchSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee", read_only=True)
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)
    punchType = serializers.CharField(source="punch_type")
    punchTime = serializers.DateTimeField(source="punch_time")
    timeDisplay = serializers.SerializerMethodField()
    date = serializers.DateField(source="work_date", read_only=True)

    class Meta:
        model = AttendancePunch
        fields = [
            "id", "employeeId", "employeeName", "employeeCode", "date",
            "punchType", "punchTime", "timeDisplay", "source", "remark", "created_at"
        ]
        read_only_fields = ["id", "employeeId", "date", "created_at"]

    def get_timeDisplay(self, obj):
        local_dt = timezone.localtime(obj.punch_time) if timezone.is_aware(obj.punch_time) else obj.punch_time
        return local_dt.strftime("%I:%M %p")


class AttendanceSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    name = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)
    empId = serializers.CharField(source="employee.employee_code", read_only=True)
    department = serializers.CharField(source="employee.department.name", read_only=True)
    dept = serializers.CharField(source="employee.department.name", read_only=True)
    shift = serializers.CharField(source="employee.shift", read_only=True)
    date = serializers.DateField(source="work_date")
    checkIn = serializers.DateTimeField(source="check_in", required=False, allow_null=True)
    checkOut = serializers.DateTimeField(source="check_out", required=False, allow_null=True)
    firstPunch = serializers.DateTimeField(source="first_punch", required=False, allow_null=True)
    lastPunch = serializers.DateTimeField(source="last_punch", required=False, allow_null=True)
    workingHours = serializers.DecimalField(source="working_hours", max_digits=6, decimal_places=2, required=False, allow_null=True)
    lateMinutes = serializers.IntegerField(source="late_minutes", required=False)
    earlyLeavingMinutes = serializers.IntegerField(source="early_leaving_minutes", required=False)
    overtimeHours = serializers.DecimalField(source="overtime_hours", max_digits=6, decimal_places=2, required=False)
    punches = serializers.SerializerMethodField()
    formattedWorkingHours = serializers.SerializerMethodField()
    lateDisplay = serializers.SerializerMethodField()
    earlyDisplay = serializers.SerializerMethodField()
    isEarlyOut = serializers.SerializerMethodField()
    overtimeDisplay = serializers.SerializerMethodField()

    class Meta:
        model = Attendance
        fields = [
            "id", "employeeId", "employeeName", "name", "employeeCode", "empId",
            "department", "dept", "shift",
            "date", "checkIn", "checkOut", "firstPunch", "lastPunch",
            "hours", "workingHours", "lateMinutes", "earlyLeavingMinutes", "overtimeHours",
            "status", "source", "remark", "leave_request",
            "punches", "formattedWorkingHours", "lateDisplay", "earlyDisplay", "isEarlyOut", "overtimeDisplay",
            "created_at", "updated_at",
        ]
        # `hours` and the Late / Half Day verdict come from the flexibility
        # policy, applied server-side (api.md §11.2).
        read_only_fields = [
            "hours", "workingHours", "firstPunch", "lastPunch", "lateMinutes",
            "earlyLeavingMinutes", "overtimeHours", "punches", "formattedWorkingHours",
            "lateDisplay", "earlyDisplay", "isEarlyOut", "overtimeDisplay",
            "source", "leave_request", "created_at", "updated_at"
        ]

    def get_punches(self, obj):
        qs = obj.punches.all().order_by("punch_time")
        return [
            {
                "id": str(p.id),
                "punchType": p.punch_type,
                "punchTime": p.punch_time.isoformat(),
                "timeDisplay": (
                    timezone.localtime(p.punch_time)
                    if timezone.is_aware(p.punch_time)
                    else p.punch_time
                ).strftime("%I:%M %p"),
                "source": p.source,
                "remark": p.remark,
            }
            for p in qs
        ]

    def get_formattedWorkingHours(self, obj):
        hours = float(obj.working_hours or obj.hours or 0)
        if obj.work_date == timezone.localdate() and obj.check_out is None:
            last_p = obj.punches.order_by("-punch_time").first()
            if last_p and last_p.punch_type == "IN":
                active_secs = max(0, (timezone.now() - last_p.punch_time).total_seconds())
                total_minutes = int(hours * 60 + (active_secs // 60))
                h = total_minutes // 60
                m = total_minutes % 60
                return f"{h:02d}h {m:02d}m"
        total_minutes = int(hours * 60)
        h = total_minutes // 60
        m = total_minutes % 60
        return f"{h:02d}h {m:02d}m"

    def get_lateDisplay(self, obj):
        if obj.late_minutes and obj.late_minutes > 0:
            return f"{obj.late_minutes} min late"
        return "On time"

    def get_overtimeDisplay(self, obj):
        if obj.overtime_hours and float(obj.overtime_hours) > 0:
            total_minutes = int(float(obj.overtime_hours) * 60)
            h = total_minutes // 60
            m = total_minutes % 60
            return f"+{h:02d}h {m:02d}m"
        return "0h 00m"

    def get_earlyDisplay(self, obj):
        mins = getattr(obj, "early_leaving_minutes", 0) or 0
        if mins > 0:
            return f"{mins} min early"
        return "-"

    def get_isEarlyOut(self, obj):
        mins = getattr(obj, "early_leaving_minutes", 0) or 0
        if mins > 0:
            return True
        if obj.check_out is not None and obj.working_hours is not None and obj.working_hours < 8:
            return True
        return False

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, "copy") else dict(data)
        work_date = data.get("date") or data.get("work_date")
        if work_date:
            for time_field in ("checkIn", "check_in"):
                val = data.get(time_field)
                if val and isinstance(val, str) and len(val) <= 8 and ":" in val:
                    data[time_field] = f"{work_date}T{val}:00" if len(val) == 5 else f"{work_date}T{val}"
            for time_field in ("checkOut", "check_out"):
                val = data.get(time_field)
                if val and isinstance(val, str) and len(val) <= 8 and ":" in val:
                    data[time_field] = f"{work_date}T{val}:00" if len(val) == 5 else f"{work_date}T{val}"

        emp_id = data.get("employeeId") or data.get("employee_id")
        if emp_id and isinstance(emp_id, str):
            import uuid
            try:
                uuid.UUID(emp_id)
            except ValueError:
                from apps.hrms.models import Employee
                request = self.context.get("request")
                client_id = getattr(request, "client_id", None) or (request.user.client_id if request and hasattr(request, "user") else None)
                if client_id:
                    emp = Employee.objects.filter(
                        employee_code=emp_id, client_id=client_id, deleted_at__isnull=True
                    ).first()
                    if emp:
                        data["employeeId"] = str(emp.id)
        return super().to_internal_value(data)



class BulkAttendanceSerializer(BaseSerializer):
    date = serializers.DateField(required=False)
    records = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    ids = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    status = serializers.CharField(required=False, allow_null=True)


class RegularizationSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    date = serializers.DateField(source="work_date")
    requestedCheckIn = serializers.DateTimeField(
        source="requested_check_in", required=False, allow_null=True
    )
    requestedCheckOut = serializers.DateTimeField(
        source="requested_check_out", required=False, allow_null=True
    )
    requestedStatus = serializers.CharField(
        source="requested_status", required=False, allow_null=True
    )

    class Meta:
        model = AttendanceRegularization
        fields = [
            "id", "employeeId", "employeeName", "date", "requestedCheckIn",
            "requestedCheckOut", "requestedStatus", "reason", "status",
            "approver", "decided_at", "remark", "created_at",
        ]
        read_only_fields = ["status", "approver", "decided_at", "created_at"]


# ---------------------------------------------------------------------------
# Leave (api.md §11.3)
# ---------------------------------------------------------------------------
class LeaveTypeSerializer(BaseModelSerializer):
    class Meta:
        model = LeaveType
        fields = [
            "id", "name", "code", "annual_entitlement", "accrual",
            "carry_forward_cap", "is_encashable", "is_paid", "enforce_sandwich_rule",
            "created_at",
        ]


class LeaveRequestSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)
    type = serializers.CharField(source="leave_type.name", read_only=True)
    leaveTypeId = TenantPrimaryKeyRelatedField(
        source="leave_type", model="hrms.LeaveType"
    )
    fromDate = serializers.DateField(source="from_date")
    toDate = serializers.DateField(source="to_date")
    delegateId = TenantPrimaryKeyRelatedField(
        source="delegate_employee", model="hrms.Employee",
        required=False, allow_null=True,
    )
    # Who covers the work, by name — the handover tables show it.
    delegate = serializers.CharField(
        source="delegate_employee.name", read_only=True, default=""
    )

    class Meta:
        model = LeaveRequest
        fields = [
            "id", "employeeId", "employeeName", "employeeCode", "type",
            "leaveTypeId", "fromDate", "toDate", "days", "reason", "delegateId", "delegate",
            "delegate_confirmed_at", "status", "approver", "decided_at",
            "remark", "created_at", "updated_at",
        ]
        read_only_fields = [
            "status", "approver", "decided_at", "delegate_confirmed_at",
            "created_at", "updated_at",
        ]

    def validate(self, attrs):
        start = attrs.get("from_date") or getattr(self.instance, "from_date", None)
        end = attrs.get("to_date") or getattr(self.instance, "to_date", None)
        if start and end and end < start:
            raise serializers.ValidationError({"toDate": ["Must be on or after the start date."]})
        return attrs


class LeaveBalanceSerializer(BaseModelSerializer):
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)
    leaveType = serializers.CharField(source="leave_type.name", read_only=True)
    # GeneratedField (computed by Postgres) -- DRF falls back to ModelField
    # for it, which drf-spectacular cannot map (DecimalField() with no args).
    # Declared explicitly so schema generation sees a real DecimalField.
    balance = serializers.DecimalField(
        max_digits=6, decimal_places=2, coerce_to_string=False, read_only=True
    )

    class Meta:
        model = LeaveBalance
        fields = [
            "id", "employee", "employeeName", "employeeCode", "leave_type",
            "leaveType", "period_year", "entitlement", "carried_forward",
            "used", "encashed", "balance",
        ]
        read_only_fields = ["balance", "used"]


class CompOffSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    workedDate = serializers.DateField(source="worked_date")
    workType = serializers.CharField(source="work_type", required=False, allow_null=True)
    expiryDate = serializers.DateField(source="expiry_date", required=False, allow_null=True)

    class Meta:
        model = CompOff
        fields = [
            "id", "employeeId", "workedDate", "workType", "days", "expiryDate",
            "used", "created_at",
        ]


class LeaveEncashmentSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    leaveTypeId = TenantPrimaryKeyRelatedField(source="leave_type", model="hrms.LeaveType")
    perDayRate = MoneyField(source="per_day_rate")
    # GeneratedField (days * per_day_rate) -- see LeaveBalanceSerializer.balance.
    amount = MoneyField(read_only=True)

    class Meta:
        model = LeaveEncashment
        fields = [
            "id", "employeeId", "leaveTypeId", "days", "perDayRate", "amount",
            "status", "created_at",
        ]
        read_only_fields = ["amount"]


# ---------------------------------------------------------------------------
# Payroll (api.md §11.4)
# ---------------------------------------------------------------------------
class SalaryStructureSerializer(BaseModelSerializer):
    class Meta:
        model = SalaryStructure
        fields = [
            "id", "name", "basic_pct", "hra_pct", "components",
            "max_overtime_hours_month", "overtime_rate_multiplier", "is_active",
            "created_at", "updated_at",
        ]



class PayslipComponentSerializer(BaseModelSerializer):
    class Meta:
        model = PayslipComponent
        fields = ["id", "name", "kind", "amount"]


class PayslipSerializer(BaseModelSerializer):
    """The payroll row api.md §11.4 documents."""

    empId = serializers.CharField(source="employee.employee_code", read_only=True)
    employeeId = serializers.CharField(source="employee_id", read_only=True)
    name = serializers.CharField(source="employee.name", read_only=True)
    role = serializers.CharField(source="employee.designation.name", read_only=True)
    department = serializers.CharField(source="employee.department.name", read_only=True)
    month = serializers.DateField(source="period_month", read_only=True)
    standardSalary = MoneyField(source="standard_salary", read_only=True)
    earnedSalary = MoneyField(source="earned_salary", read_only=True)
    additionalEarnings = MoneyField(source="additional_earnings", required=False)
    advance = MoneyField(source="advance_recovery", required=False)
    paidLeaves = serializers.DecimalField(
        source="paid_leaves", max_digits=6, decimal_places=2,
        coerce_to_string=False, read_only=True,
    )
    attendedDays = serializers.DecimalField(
        source="attended_days", max_digits=6, decimal_places=2,
        coerce_to_string=False, read_only=True,
    )
    totalDays = serializers.DecimalField(
        source="total_days", max_digits=6, decimal_places=2,
        coerce_to_string=False, read_only=True,
    )
    paidAmount = MoneyField(source="paid_amount", read_only=True)
    paymentDate = serializers.DateField(source="payment_date", read_only=True)
    bank = serializers.CharField(source="bank_account.name", read_only=True)
    components = PayslipComponentSerializer(many=True, read_only=True)
    netPayable = MoneyField(source="net_payable", read_only=True)

    class Meta:
        model = Payslip
        fields = [
            "id", "empId", "employeeId", "name", "role", "department", "month",
            "standardSalary", "earnedSalary", "additionalEarnings", "deductions",
            "advance", "paidLeaves", "basic", "hra", "allowances", "status",
            "paidAmount", "paymentDate", "bank", "attendedDays", "totalDays",
            "netPayable", "earned_salary_overridden", "components",
            "created_at", "updated_at",
        ]
        read_only_fields = ["status", "earned_salary_overridden", "created_at", "updated_at"]


class PayrollRunSerializer(BaseModelSerializer):
    payslipCount = serializers.SerializerMethodField()

    class Meta:
        model = PayrollRun
        fields = [
            "id", "period_month", "status", "processed_by", "processed_at",
            "approved_by", "approved_at", "payslipCount", "created_at",
        ]

    def get_payslipCount(self, run):
        return run.payslips.filter(deleted_at__isnull=True).count()


class ProcessPayrollSerializer(BaseSerializer):
    month = serializers.DateField()
    employeeIds = serializers.ListField(
        child=serializers.CharField(), required=False, default=list
    )


class SalaryAdvanceSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    issuedOn = serializers.DateField(source="issued_on")
    recoveredAmount = MoneyField(source="recovered_amount", read_only=True)
    outstanding = serializers.SerializerMethodField()

    class Meta:
        model = SalaryAdvance
        fields = [
            "id", "employeeId", "employeeName", "amount", "issuedOn",
            "installments", "recoveredAmount", "outstanding", "status",
            "notes", "created_at",
        ]
        read_only_fields = ["status", "created_at"]

    def get_outstanding(self, advance):
        return advance.outstanding


# ---------------------------------------------------------------------------
# Recruitment (api.md §11.5)
# ---------------------------------------------------------------------------
class JobSerializer(BaseModelSerializer):
    department = serializers.CharField(source="department.name", read_only=True)
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )
    designationId = TenantPrimaryKeyRelatedField(
        source="designation", model="hrms.Designation", required=False, allow_null=True
    )
    locationId = TenantPrimaryKeyRelatedField(
        source="location", model="hrms.Location", required=False, allow_null=True
    )
    applicantCount = serializers.SerializerMethodField()

    class Meta:
        model = Job
        fields = [
            "id", "title", "department", "departmentId", "designationId",
            "locationId", "openings", "experience_range", "salary_range",
            "employment_type", "description", "status", "is_published",
            "published_at", "slug", "applicantCount", "created_at", "updated_at",
        ]
        read_only_fields = ["published_at", "slug", "created_at", "updated_at"]

    def get_applicantCount(self, job):
        return getattr(job, "applicant_count", None)


class CandidateSerializer(BaseModelSerializer):
    resumeFileId = TenantPrimaryKeyRelatedField(
        source="resume_file", model="core.File", required=False, allow_null=True
    )

    class Meta:
        model = Candidate
        fields = [
            "id", "name", "email", "phone", "resumeFileId", "source",
            "current_ctc", "expected_ctc", "notice_period_days", "stage",
            "rating", "employee", "notes", "created_at", "updated_at",
        ]
        read_only_fields = ["employee", "created_at", "updated_at"]


class ApplicationSerializer(BaseModelSerializer):
    candidateId = TenantPrimaryKeyRelatedField(source="candidate", model="hrms.Candidate")
    candidateName = serializers.CharField(source="candidate.name", read_only=True)
    jobId = TenantPrimaryKeyRelatedField(source="job", model="hrms.Job")
    jobTitle = serializers.CharField(source="job.title", read_only=True)

    class Meta:
        model = Application
        fields = [
            "id", "candidateId", "candidateName", "jobId", "jobTitle",
            "applied_at", "stage", "rejection_reason",
        ]


class InterviewSerializer(BaseModelSerializer):
    applicationId = TenantPrimaryKeyRelatedField(
        source="application", model="hrms.Application"
    )
    candidateName = serializers.CharField(
        source="application.candidate.name", read_only=True
    )
    scheduledAt = serializers.DateTimeField(source="scheduled_at")
    panelUserIds = serializers.JSONField(source="panel_user_ids", required=False)

    class Meta:
        model = Interview
        fields = [
            "id", "applicationId", "candidateName", "round", "scheduledAt",
            "mode", "panelUserIds", "status", "rating", "notes",
            "recommendation", "created_at",
        ]


class OfferSerializer(BaseModelSerializer):
    applicationId = TenantPrimaryKeyRelatedField(
        source="application", model="hrms.Application"
    )
    candidateName = serializers.CharField(
        source="application.candidate.name", read_only=True
    )
    offeredCtc = MoneyField(source="offered_ctc", required=False, allow_null=True)
    joiningDate = serializers.DateField(
        source="joining_date", required=False, allow_null=True
    )

    class Meta:
        model = Offer
        fields = [
            "id", "applicationId", "candidateName", "offeredCtc", "joiningDate",
            "status", "letter_file", "sent_at", "responded_at", "notes",
            "created_at",
        ]
        read_only_fields = ["sent_at", "responded_at", "created_at"]


class OnboardingTaskSerializer(BaseModelSerializer):
    candidateId = TenantPrimaryKeyRelatedField(source="candidate", model="hrms.Candidate")

    class Meta:
        model = OnboardingTask
        fields = ["id", "candidateId", "title", "owner", "due_date", "completed_at"]


class ScreeningQuestionSerializer(BaseModelSerializer):
    jobId = TenantPrimaryKeyRelatedField(
        source="job", model="hrms.Job", required=False, allow_null=True
    )
    jobTitle = serializers.CharField(source="job.title", read_only=True)

    class Meta:
        model = ScreeningQuestion
        fields = ["id", "jobId", "jobTitle", "question", "type", "options", "is_active", "sort_order", "created_at"]


class ScreeningAnswerSerializer(BaseModelSerializer):
    applicationId = TenantPrimaryKeyRelatedField(source="application", model="hrms.Application")
    questionId = TenantPrimaryKeyRelatedField(source="question", model="hrms.ScreeningQuestion")
    questionText = serializers.CharField(source="question.question", read_only=True)

    class Meta:
        model = ScreeningAnswer
        fields = ["id", "applicationId", "questionId", "questionText", "answer", "created_at"]



# ---------------------------------------------------------------------------
# Performance (api.md §11.6)
# ---------------------------------------------------------------------------
class AppraisalCycleSerializer(BaseModelSerializer):
    class Meta:
        model = AppraisalCycle
        fields = [
            "id", "name", "period_start", "period_end", "status",
            "participants_scope", "created_at",
        ]


class PerformanceIndicatorSerializer(BaseModelSerializer):
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )

    class Meta:
        model = PerformanceIndicator
        fields = ["id", "name", "category", "weight", "departmentId", "created_at"]


class KpiSerializer(BaseModelSerializer):
    indicatorId = TenantPrimaryKeyRelatedField(
        source="indicator", model="hrms.PerformanceIndicator",
        required=False, allow_null=True,
    )

    class Meta:
        model = Kpi
        fields = ["id", "indicatorId", "name", "target", "unit", "applies_to", "created_at"]


class AppraisalKpiScoreSerializer(BaseModelSerializer):
    kpiId = TenantPrimaryKeyRelatedField(source="kpi", model="hrms.Kpi")
    kpiName = serializers.CharField(source="kpi.name", read_only=True)

    class Meta:
        model = AppraisalKpiScore
        fields = ["id", "kpiId", "kpiName", "target", "achieved", "score", "weight"]


class AppraisalHistorySerializer(BaseModelSerializer):
    actorName = serializers.CharField(source="actor.name", read_only=True)

    class Meta:
        model = AppraisalHistory
        fields = [
            "id", "from_stage", "to_stage", "actor", "actorName", "action",
            "comment", "created_at",
        ]


class AppraisalSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    managerId = TenantPrimaryKeyRelatedField(
        source="manager", model="hrms.Employee", required=False, allow_null=True
    )
    cycleId = TenantPrimaryKeyRelatedField(source="cycle", model="hrms.AppraisalCycle")
    kpiScores = AppraisalKpiScoreSerializer(source="kpi_scores", many=True, read_only=True)
    history = AppraisalHistorySerializer(many=True, read_only=True)

    class Meta:
        model = Appraisal
        fields = [
            "id", "cycleId", "employeeId", "employeeName", "managerId", "stage",
            "status", "self_rating", "manager_rating", "final_rating",
            "strengths", "areas_for_improvement", "development_feedback",
            "hr_comments", "self_comments", "kpiScores", "history",
            "created_at", "updated_at",
        ]
        read_only_fields = ["stage", "status", "created_at", "updated_at"]


class GoalSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")

    class Meta:
        model = Goal
        fields = [
            "id", "employeeId", "cycle", "title", "description", "target_date",
            "progress_pct", "status", "created_at",
        ]


# ---------------------------------------------------------------------------
# Training (api.md §11.7)
# ---------------------------------------------------------------------------
class TrainerSerializer(BaseModelSerializer):
    class Meta:
        model = Trainer
        fields = [
            "id", "name", "kind", "employee", "organisation", "expertise",
            "rate", "contact", "created_at",
        ]


class TrainingParticipantSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)

    class Meta:
        model = TrainingParticipant
        fields = [
            "id", "employeeId", "employeeName", "attendance_status", "rating",
            "feedback", "evaluated_at",
        ]


class TrainingSerializer(BaseModelSerializer):
    trainerId = TenantPrimaryKeyRelatedField(
        source="trainer", model="hrms.Trainer", required=False, allow_null=True
    )
    trainerName = serializers.CharField(source="trainer.name", read_only=True)
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )
    participants = TrainingParticipantSerializer(many=True, read_only=True)

    class Meta:
        model = Training
        fields = [
            "id", "title", "description", "type", "trainerId", "trainerName",
            "departmentId", "start_date", "end_date", "venue", "cost", "stage",
            "participants", "created_at", "updated_at",
        ]


# ---------------------------------------------------------------------------
# Assets, documents, policies, calendar, HR admin (api.md §11.8)
# ---------------------------------------------------------------------------
class AssetCategorySerializer(BaseModelSerializer):
    class Meta:
        model = AssetCategory
        fields = ["id", "name", "depreciation_pct", "default_warranty_months"]


class AssetAssignmentSerializer(BaseModelSerializer):
    employeeName = serializers.CharField(source="employee.name", read_only=True)

    class Meta:
        model = AssetAssignment
        fields = [
            "id", "employee", "employeeName", "assigned_at", "assigned_by",
            "returned_at", "returned_condition", "notes",
        ]


class AssetSerializer(BaseModelSerializer):
    assetCode = serializers.CharField(source="asset_code", read_only=True)
    category = serializers.CharField(source="category.name", read_only=True)
    categoryId = TenantPrimaryKeyRelatedField(
        source="category", model="hrms.AssetCategory", required=False, allow_null=True
    )
    assignedTo = serializers.CharField(source="assigned_employee.name", read_only=True)
    employeeId = TenantPrimaryKeyRelatedField(
        source="assigned_employee", model="hrms.Employee", required=False, allow_null=True
    )
    dept = serializers.CharField(
        source="assigned_employee.department.name", read_only=True
    )
    serialNumber = serializers.CharField(
        source="serial_number", required=False, allow_null=True, allow_blank=True
    )
    purchaseDate = serializers.DateField(
        source="purchase_date", required=False, allow_null=True
    )
    purchaseCost = MoneyField(source="purchase_cost", required=False, allow_null=True)
    warrantyExpiry = serializers.DateField(
        source="warranty_expiry", required=False, allow_null=True
    )
    history = AssetAssignmentSerializer(many=True, read_only=True)

    class Meta:
        model = Asset
        fields = [
            "id", "assetCode", "name", "category", "categoryId", "serialNumber",
            "assignedTo", "employeeId", "dept", "status", "condition",
            "purchaseDate", "purchaseCost", "warrantyExpiry", "location",
            "notes", "history", "created_at", "updated_at",
        ]
        read_only_fields = ["assetCode", "created_at", "updated_at"]


class AssetRequestSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    categoryId = TenantPrimaryKeyRelatedField(
        source="category", model="hrms.AssetCategory", required=False, allow_null=True
    )

    class Meta:
        model = AssetRequest
        fields = [
            "id", "employeeId", "employeeName", "categoryId", "justification",
            "status", "approver", "fulfilled_asset", "created_at",
        ]


class HrDocumentSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(
        source="employee", model="hrms.Employee", required=False, allow_null=True
    )
    employee = serializers.CharField(source="employee.name", read_only=True)
    fileId = TenantPrimaryKeyRelatedField(
        source="file", model="core.File", required=False, allow_null=True
    )
    fileSize = serializers.IntegerField(source="file.file_size", read_only=True)
    fileType = serializers.CharField(source="file.content_type", read_only=True)
    expiry = serializers.DateField(source="valid_until", required=False, allow_null=True)
    #: DERIVED from ``valid_until`` at read time (api.md §11.8).
    status = serializers.SerializerMethodField()
    uploadedBy = serializers.CharField(source="created_by.name", read_only=True)
    updatedOn = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = HrDocument
        fields = [
            "id", "title", "category", "employee", "employeeId", "version",
            "expiry", "status", "fileId", "fileSize", "fileType", "uploadedBy",
            "updatedOn", "tags", "description", "valid_from", "is_confidential",
            "created_at",
        ]

    def get_status(self, document):
        return services.document_status(document)


class PolicyVersionSerializer(BaseModelSerializer):
    changedByName = serializers.CharField(source="changed_by.name", read_only=True)

    class Meta:
        model = PolicyVersion
        fields = ["id", "version", "change_note", "changedByName", "created_at"]


class PolicySerializer(BaseModelSerializer):
    category = serializers.CharField(source="category.name", read_only=True)
    categoryId = TenantPrimaryKeyRelatedField(
        source="category", model="hrms.PolicyCategory", required=False, allow_null=True
    )
    ownerDept = serializers.CharField(source="owner_department.name", read_only=True)
    ownerDepartmentId = TenantPrimaryKeyRelatedField(
        source="owner_department", model="hrms.Department",
        required=False, allow_null=True,
    )
    applicableTo = serializers.CharField(
        source="applicable_to", required=False, allow_null=True, allow_blank=True
    )
    effectiveDate = serializers.DateField(
        source="effective_date", required=False, allow_null=True
    )
    reviewDate = serializers.DateField(
        source="review_date", required=False, allow_null=True
    )
    approvalRequired = serializers.BooleanField(source="approval_required", required=False)
    ackRequired = serializers.BooleanField(source="ack_required", required=False)
    versionHistory = PolicyVersionSerializer(
        source="version_history", many=True, read_only=True
    )

    class Meta:
        model = Policy
        fields = [
            "id", "name", "category", "categoryId", "ownerDept",
            "ownerDepartmentId", "applicableTo", "version", "effectiveDate",
            "reviewDate", "approvalRequired", "ackRequired", "ack_window_days",
            "status", "summary", "body", "file", "versionHistory",
            "approved_by", "approved_at", "created_at", "updated_at",
        ]
        read_only_fields = [
            "version", "status", "approved_by", "approved_at", "created_at", "updated_at",
        ]


class PolicyAcknowledgementSerializer(BaseModelSerializer):
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)
    status = serializers.SerializerMethodField()

    class Meta:
        model = PolicyAcknowledgement
        fields = [
            "id", "employee", "employeeName", "employeeCode", "policy_version",
            "acknowledged_at", "status",
        ]

    def get_status(self, acknowledgement):
        policy = self.context.get("policy") or acknowledgement.policy
        return services.acknowledgement_status(acknowledgement, policy)


class PolicyCategorySerializer(BaseModelSerializer):
    class Meta:
        model = PolicyCategory
        fields = ["id", "name", "description"]


class CalendarEventSerializer(BaseModelSerializer):
    startDate = serializers.DateTimeField(source="starts_at")
    endDate = serializers.DateTimeField(source="ends_at", required=False, allow_null=True)
    date = serializers.SerializerMethodField()
    dept = serializers.CharField(source="department.name", read_only=True)
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )
    virtualLink = serializers.CharField(
        source="virtual_link", required=False, allow_null=True, allow_blank=True
    )
    #: ``time`` is a display string the server derives (api.md §11.8).
    time = serializers.SerializerMethodField()

    class Meta:
        model = CalendarEvent
        fields = [
            "id", "title", "date", "startDate", "endDate", "type", "time",
            "location", "dept", "departmentId", "organizer", "description",
            "virtualLink", "all_day", "source_type", "source_id", "created_at",
        ]
        read_only_fields = ["source_type", "source_id", "created_at"]

    def get_date(self, event):
        return event.starts_at.date() if event.starts_at else None

    def get_time(self, event):
        return services.calendar_time_label(event)


class HolidaySerializer(BaseModelSerializer):
    locationId = TenantPrimaryKeyRelatedField(
        source="location", model="hrms.Location", required=False, allow_null=True
    )

    class Meta:
        model = Holiday
        fields = ["id", "date", "name", "locationId", "is_optional", "created_at"]


class WorkingDaySerializer(BaseModelSerializer):
    class Meta:
        model = WorkingDay
        fields = ["id", "weekday", "is_working", "shift_start", "shift_end", "location"]


class TeamSerializer(BaseModelSerializer):
    department = serializers.CharField(source="department.name", read_only=True)
    departmentId = TenantPrimaryKeyRelatedField(
        source="department", model="hrms.Department", required=False, allow_null=True
    )
    leadName = serializers.CharField(source="lead.name", read_only=True)
    memberCount = serializers.SerializerMethodField()

    class Meta:
        model = Team
        fields = [
            "id", "name", "department", "departmentId", "lead", "leadName",
            "members", "memberCount", "description", "status", "created_at",
        ]

    def get_memberCount(self, team):
        return team.members.count()


class ApprovalChainSerializer(BaseModelSerializer):
    class Meta:
        model = ApprovalChain
        fields = ["id", "name", "applies_to", "steps", "status", "created_at"]


class TerminationSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    lastWorkingDay = serializers.DateField(
        source="last_working_day", required=False, allow_null=True
    )

    class Meta:
        model = Termination
        fields = [
            "id", "employeeId", "employeeName", "reason", "lastWorkingDay",
            "letter_file", "settlement_amount", "status", "created_at",
        ]


class ResignationChecklistItemSerializer(BaseModelSerializer):
    class Meta:
        model = ResignationChecklistItem
        fields = ["id", "title", "owner", "completed_at"]


class ResignationSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    submittedOn = serializers.DateField(source="submitted_on")
    noticePeriodDays = serializers.IntegerField(source="notice_period_days", required=False)
    lastWorkingDay = serializers.DateField(
        source="last_working_day", required=False, allow_null=True
    )
    checklist = ResignationChecklistItemSerializer(many=True, read_only=True)

    class Meta:
        model = Resignation
        fields = [
            "id", "employeeId", "employeeName", "submittedOn", "noticePeriodDays",
            "lastWorkingDay", "exit_interview_at", "reason", "status",
            "checklist", "created_at",
        ]


class ComplaintSerializer(BaseModelSerializer):
    """``is_anonymous`` is honoured by nulling the raiser in every serialiser --
    including exports (db.md §11.8)."""

    raisedBy = serializers.SerializerMethodField()
    raisedByEmployeeId = TenantPrimaryKeyRelatedField(
        source="raised_by_employee", model="hrms.Employee",
        required=False, allow_null=True, write_only=True,
    )
    againstEmployeeId = TenantPrimaryKeyRelatedField(
        source="against_employee", model="hrms.Employee",
        required=False, allow_null=True,
    )
    againstName = serializers.SerializerMethodField()
    isAnonymous = serializers.BooleanField(source="is_anonymous", required=False)

    class Meta:
        model = Complaint
        fields = [
            "id", "raisedBy", "raisedByEmployeeId", "againstEmployeeId",
            "againstName", "category", "description", "isAnonymous", "status",
            "assigned_to", "resolution", "resolved_at", "created_at",
        ]
        read_only_fields = ["resolved_at", "created_at"]

    def get_raisedBy(self, complaint):
        if complaint.is_anonymous:
            return None
        return complaint.raised_by_employee.name if complaint.raised_by_employee_id else None

    def get_againstName(self, complaint):
        return complaint.against_employee.name if complaint.against_employee_id else None


# ---------------------------------------------------------------------------
# Upgradation Scope: Transfers, Promotions, Warnings, Awards, Travel, Announcements
# ---------------------------------------------------------------------------
class EmployeeTransferSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)
    fromDepartmentId = TenantPrimaryKeyRelatedField(source="from_department", model="hrms.Department", required=False, allow_null=True)
    fromDepartmentName = serializers.CharField(source="from_department.name", read_only=True)
    toDepartmentId = TenantPrimaryKeyRelatedField(source="to_department", model="hrms.Department", required=False, allow_null=True)
    toDepartmentName = serializers.CharField(source="to_department.name", read_only=True)
    fromLocationId = TenantPrimaryKeyRelatedField(source="from_location", model="hrms.Location", required=False, allow_null=True)
    fromLocationName = serializers.CharField(source="from_location.name", read_only=True)
    toLocationId = TenantPrimaryKeyRelatedField(source="to_location", model="hrms.Location", required=False, allow_null=True)
    toLocationName = serializers.CharField(source="to_location.name", read_only=True)

    class Meta:
        model = EmployeeTransfer
        fields = [
            "id", "transfer_number", "employeeId", "employeeName", "employeeCode",
            "fromDepartmentId", "fromDepartmentName", "toDepartmentId", "toDepartmentName",
            "fromLocationId", "fromLocationName", "toLocationId", "toLocationName",
            "effective_date", "reason", "status", "created_at", "updated_at",
        ]
        read_only_fields = ["transfer_number", "created_at", "updated_at"]


class EmployeePromotionSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)
    fromDesignationId = TenantPrimaryKeyRelatedField(source="from_designation", model="hrms.Designation", required=False, allow_null=True)
    fromDesignationTitle = serializers.CharField(source="from_designation.title", read_only=True)
    toDesignationId = TenantPrimaryKeyRelatedField(source="to_designation", model="hrms.Designation", required=False, allow_null=True)
    toDesignationTitle = serializers.CharField(source="to_designation.title", read_only=True)

    class Meta:
        model = EmployeePromotion
        fields = [
            "id", "promotion_number", "employeeId", "employeeName", "employeeCode",
            "fromDesignationId", "fromDesignationTitle", "toDesignationId", "toDesignationTitle",
            "previous_salary", "new_salary", "effective_date", "justification", "status",
            "created_at", "updated_at",
        ]
        read_only_fields = ["promotion_number", "created_at", "updated_at"]


class EmployeeWarningSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)

    class Meta:
        model = EmployeeWarning
        fields = [
            "id", "warning_number", "employeeId", "employeeName", "employeeCode",
            "issue_date", "severity", "incident_date", "subject", "description",
            "corrective_action", "status", "employee_explanation", "created_at", "updated_at",
        ]
        read_only_fields = ["warning_number", "created_at", "updated_at"]


class EmployeeAwardSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)

    class Meta:
        model = EmployeeAward
        fields = [
            "id", "award_number", "employeeId", "employeeName", "employeeCode",
            "award_name", "category", "award_date", "gift_amount", "citation",
            "badge_icon", "created_at",
        ]
        read_only_fields = ["award_number", "created_at"]


class TravelRequestSerializer(BaseModelSerializer):
    employeeId = TenantPrimaryKeyRelatedField(source="employee", model="hrms.Employee")
    employeeName = serializers.CharField(source="employee.name", read_only=True)
    employeeCode = serializers.CharField(source="employee.employee_code", read_only=True)

    class Meta:
        model = TravelRequest
        fields = [
            "id", "travel_number", "employeeId", "employeeName", "employeeCode",
            "purpose", "destination", "start_date", "end_date", "advance_requested",
            "advance_disbursed", "actual_expenses", "settlement_notes", "status",
            "created_at", "updated_at",
        ]
        read_only_fields = ["travel_number", "created_at", "updated_at"]


class AnnouncementSerializer(BaseModelSerializer):
    targetDepartmentId = TenantPrimaryKeyRelatedField(source="target_department", model="hrms.Department", required=False, allow_null=True)
    targetDepartmentName = serializers.CharField(source="target_department.name", read_only=True)

    class Meta:
        model = Announcement
        fields = [
            "id", "title", "content", "priority", "targetDepartmentId",
            "targetDepartmentName", "is_pinned", "publish_date", "expiry_date",
            "author_name", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


class MeetingRoomSerializer(BaseModelSerializer):
    class Meta:
        model = MeetingRoom
        fields = [
            "id", "name", "code", "capacity", "location", "amenities", "is_active", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


class CompanyMeetingSerializer(BaseModelSerializer):
    roomId = TenantPrimaryKeyRelatedField(
        source="room", model="hrms.MeetingRoom", required=False, allow_null=True
    )
    roomName = serializers.CharField(source="room.name", read_only=True, allow_null=True)
    roomCode = serializers.CharField(source="room.code", read_only=True, allow_null=True)
    hostUserId = TenantPrimaryKeyRelatedField(
        source="host_user", model="accounts.User", required=False, allow_null=True
    )
    hostUserName = serializers.CharField(source="host_user.get_full_name", read_only=True)
    attendeeIds = serializers.PrimaryKeyRelatedField(
        source="attendees", many=True, read_only=False, queryset=User.objects.all(), required=False
    )
    attendeesDetail = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = CompanyMeeting
        fields = [
            "id", "title", "description", "roomId", "roomName", "roomCode", "meeting_type",
            "start_time", "end_time", "video_provider", "join_url",
            "hostUserId", "hostUserName", "attendeeIds", "attendeesDetail",
            "external_attendees", "agenda", "minutes_of_meeting", "action_items",
            "status", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.accounts.models import User
        if "attendeeIds" in self.fields:
            self.fields["attendeeIds"].queryset = User.objects.all()

    def get_attendeesDetail(self, obj):
        return [
            {"id": str(u.id), "name": u.get_full_name() or u.username, "email": u.email}
            for u in obj.attendees.all()
        ]


