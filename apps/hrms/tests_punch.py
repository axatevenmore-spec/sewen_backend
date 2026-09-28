"""Tests for Punch In / Punch Out, punch timeline, working hours and payroll integration."""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Client, Role, User
from apps.hrms.models import Attendance, AttendancePunch, Department, Employee, PayrollRun, Payslip
from apps.hrms import services


class PunchSystemTests(TestCase):
    def setUp(self):
        self.client_obj = Client.objects.create(
            name="Punch Test Client",
            slug="punch-client",
        )
        self.dept = Department.objects.create(
            client=self.client_obj,
            name="Engineering",
        )
        self.employee = Employee.objects.create(
            client=self.client_obj,
            employee_code="EMP-101",
            name="Marcus Vance",
            email="marcus@example.com",
            department=self.dept,
            joining_date=date(2025, 1, 1),
            shift="General",
            standard_salary=Decimal("60000"),
        )
        self.user = User.objects.create(
            client=self.client_obj,
            email="marcus@example.com",
            name="Marcus Vance",
            employee=self.employee,
        )

    def test_single_punch_in_and_out(self):
        work_date = date(2026, 9, 28)
        # 09:30 AM Punch In
        t1 = timezone.make_aware(datetime.combine(work_date, time(9, 30)))
        p1, status1 = services.record_punch(
            client=self.client_obj,
            employee=self.employee,
            punch_type="IN",
            punch_time=t1,
            user=self.user,
        )
        self.assertEqual(p1.punch_type, "IN")
        self.assertTrue(status1["is_punched_in"])
        self.assertEqual(status1["status"], "Punched In")

        # 06:00 PM Punch Out (8.5 hours later)
        t2 = timezone.make_aware(datetime.combine(work_date, time(18, 0)))
        p2, status2 = services.record_punch(
            client=self.client_obj,
            employee=self.employee,
            punch_type="OUT",
            punch_time=t2,
            user=self.user,
        )
        self.assertEqual(p2.punch_type, "OUT")
        self.assertFalse(status2["is_punched_in"])
        self.assertEqual(status2["status"], "Punched Out")
        self.assertEqual(status2["working_hours"], 8.5)
        self.assertEqual(status2["formatted_working_time"], "08h 30m")

        # Check Attendance row
        att = Attendance.objects.get(client=self.client_obj, employee=self.employee, work_date=work_date)
        self.assertEqual(att.hours, Decimal("8.50"))
        self.assertEqual(att.status, "Present")
        self.assertEqual(att.late_minutes, 0)
        self.assertEqual(att.punches.count(), 2)

    def test_single_punch_per_day_enforced(self):
        """Only one Punch In and Punch Out allowed per day."""
        work_date = date(2026, 9, 28)
        t1 = timezone.make_aware(datetime.combine(work_date, time(9, 30)))
        t2 = timezone.make_aware(datetime.combine(work_date, time(18, 30)))
        t3 = timezone.make_aware(datetime.combine(work_date, time(19, 0)))

        # 1. Punch In succeeds
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t1, user=self.user)
        # 2. Punch Out succeeds
        _, status_out = services.record_punch(client=self.client_obj, employee=self.employee, punch_type="OUT", punch_time=t2, user=self.user)
        self.assertTrue(status_out["day_completed"])
        self.assertFalse(status_out["can_punch_in"])
        self.assertFalse(status_out["can_punch_out"])

        # 3. Second Punch In on the same day must be rejected
        with self.assertRaises(Exception) as ctx:
            services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t3, user=self.user)
        self.assertIn("already completed your punch in and punch out for today", str(ctx.exception))

    def test_validation_prevents_in_in_and_out_out(self):
        work_date = date(2026, 9, 28)
        t1 = timezone.make_aware(datetime.combine(work_date, time(9, 30)))
        t2 = timezone.make_aware(datetime.combine(work_date, time(10, 0)))

        # 1. OUT without IN
        with self.assertRaises(Exception):
            services.record_punch(client=self.client_obj, employee=self.employee, punch_type="OUT", punch_time=t1, user=self.user)

        # 2. IN succeeds
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t1, user=self.user)

        # 3. Second IN fails
        with self.assertRaises(Exception):
            services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t2, user=self.user)

    def test_late_arrival_detection(self):
        work_date = date(2026, 9, 28)
        # Shift start 09:30 + 15 min grace = 09:45. Arrived at 10:15 (45 min late past 09:30)
        t1 = timezone.make_aware(datetime.combine(work_date, time(10, 15)))
        p1, status = services.record_punch(
            client=self.client_obj,
            employee=self.employee,
            punch_type="IN",
            punch_time=t1,
            user=self.user,
        )
        self.assertEqual(status["late_minutes"], 45)
        self.assertEqual(status["attendance_status"], "Late")

        att = Attendance.objects.get(client=self.client_obj, employee=self.employee, work_date=work_date)
        self.assertEqual(att.late_minutes, 45)
        self.assertEqual(att.status, "Late")

    def test_payroll_uses_attendance_punches(self):
        """Attendance derived from punches flows seamlessly into monthly payroll calculation."""
        work_date = date(2026, 9, 15)
        t1 = timezone.make_aware(datetime.combine(work_date, time(9, 30)))
        t2 = timezone.make_aware(datetime.combine(work_date, time(18, 0)))

        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t1, user=self.user)
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="OUT", punch_time=t2, user=self.user)

        # Process payroll for September 2026
        period_month = date(2026, 9, 1)
        payroll_result = services.process_payroll(
            client=self.client_obj,
            period_month=period_month,
            employee_ids=[self.employee.id],
            user=self.user,
        )
        self.assertIsNotNone(payroll_result)
        run = payroll_result["run"]
        payslip = Payslip.objects.filter(payroll_run=run, employee=self.employee).first()
        self.assertIsNotNone(payslip)
        # Present for 1 day
        self.assertEqual(payslip.attended_days, Decimal("1.00"))
        self.assertTrue(payslip.net_payable > 0)

    def test_punch_in_check_out_is_none_until_punch_out(self):
        work_date = date(2026, 9, 28)
        t1 = timezone.make_aware(datetime.combine(work_date, time(9, 30)))
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t1, user=self.user)
        att = Attendance.objects.get(client=self.client_obj, employee=self.employee, work_date=work_date)
        self.assertIsNotNone(att.check_in)
        self.assertIsNone(att.check_out)

        # After punch out, check_out is set
        t2 = timezone.make_aware(datetime.combine(work_date, time(18, 30)))
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="OUT", punch_time=t2, user=self.user)
        att.refresh_from_db()
        self.assertEqual(att.check_out, t2)

    def test_early_punch_out_marking_and_regularization_link(self):
        """Early punch out calculates early_leaving_minutes and links with AttendanceRegularization."""
        from apps.hrms.models import AttendanceRegularization
        work_date = date(2026, 9, 28)
        # Shift is 09:30 - 18:30 (9 hours total). Leaving at 15:00 = 3.5 hours (210 min) early. Worked 5.5 hours.
        t1 = timezone.make_aware(datetime.combine(work_date, time(9, 30)))
        t2 = timezone.make_aware(datetime.combine(work_date, time(15, 0)))

        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t1, user=self.user)
        p_out, status = services.record_punch(
            client=self.client_obj,
            employee=self.employee,
            punch_type="OUT",
            punch_time=t2,
            early_reason="Doctor appointment",
            request_regularization=True,
            user=self.user,
        )

        self.assertTrue(status["is_early_out"])
        self.assertEqual(status["early_leaving_minutes"], 210)
        self.assertTrue(status["has_pending_regularization"])

        att = Attendance.objects.get(client=self.client_obj, employee=self.employee, work_date=work_date)
        self.assertEqual(att.early_leaving_minutes, 210)
        self.assertIn("Doctor appointment", att.remark)

        # Verify AttendanceRegularization row was created in Pending status
        reg = AttendanceRegularization.objects.filter(client=self.client_obj, employee=self.employee, work_date=work_date).first()
        self.assertIsNotNone(reg)
        self.assertEqual(reg.status, "Pending")
        self.assertEqual(reg.requested_status, "Present")
        self.assertIn("Doctor appointment", reg.reason)

    def test_rapid_duplicate_punch_idempotent(self):
        work_date = date(2026, 9, 28)
        t1 = timezone.make_aware(datetime.combine(work_date, time(9, 30)))
        p1, s1 = services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t1, user=self.user)
        # 2 seconds later accidental duplicate click
        t2 = t1 + timedelta(seconds=2)
        p2, s2 = services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t2, user=self.user)
        self.assertEqual(p1.id, p2.id)
        self.assertEqual(AttendancePunch.objects.filter(employee=self.employee, work_date=work_date).count(), 1)

    def test_half_day_rule_under_threshold(self):
        work_date = date(2026, 9, 28)
        # Worked 2.5 hours (under 4h threshold)
        t1 = timezone.make_aware(datetime.combine(work_date, time(9, 30)))
        t2 = timezone.make_aware(datetime.combine(work_date, time(12, 0)))
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t1, user=self.user)
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="OUT", punch_time=t2, user=self.user)
        att = Attendance.objects.get(client=self.client_obj, employee=self.employee, work_date=work_date)
        self.assertEqual(att.status, "Half Day")
        self.assertEqual(att.hours, Decimal("2.50"))

    def test_overtime_calculation(self):
        work_date = date(2026, 9, 28)
        # Worked 9.5 hours (8h full day + 1.5h overtime)
        t1 = timezone.make_aware(datetime.combine(work_date, time(9, 0)))
        t2 = timezone.make_aware(datetime.combine(work_date, time(18, 30)))
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t1, user=self.user)
        p, status = services.record_punch(client=self.client_obj, employee=self.employee, punch_type="OUT", punch_time=t2, user=self.user)
        self.assertEqual(status["overtime_hours"], 1.5)
        att = Attendance.objects.get(client=self.client_obj, employee=self.employee, work_date=work_date)
        self.assertEqual(att.overtime_hours, Decimal("1.50"))

    def test_early_punch_out_payroll_and_regularization_approval(self):
        """Early punch out under 4 hours counts as Half Day in payroll; approved regularization restores full day."""
        from apps.hrms.models import AttendanceRegularization
        work_date = date(2026, 9, 20)
        # Worked 3 hours: 09:30 to 12:30 -> Under 4h half-day threshold
        t1 = timezone.make_aware(datetime.combine(work_date, time(9, 30)))
        t2 = timezone.make_aware(datetime.combine(work_date, time(12, 30)))

        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t1, user=self.user)
        services.record_punch(
            client=self.client_obj,
            employee=self.employee,
            punch_type="OUT",
            punch_time=t2,
            early_reason="Sudden illness",
            request_regularization=True,
            user=self.user,
        )

        att = Attendance.objects.get(client=self.client_obj, employee=self.employee, work_date=work_date)
        self.assertEqual(att.status, "Half Day")
        self.assertEqual(att.early_leaving_minutes, 360) # 18:30 - 12:30 = 6 hours = 360 min

        # 1. Before regularization approval: Payroll calculates 0.5 days attended
        payroll_1 = services.process_payroll(
            client=self.client_obj,
            period_month=date(2026, 9, 1),
            employee_ids=[self.employee.id],
            user=self.user,
        )
        payslip_1 = Payslip.objects.get(payroll_run=payroll_1["run"], employee=self.employee)
        self.assertEqual(payslip_1.attended_days, Decimal("0.50"))

        # 2. Manager approves regularization request
        reg = AttendanceRegularization.objects.get(client=self.client_obj, employee=self.employee, work_date=work_date)
        reg.status = "Approved"
        reg.approver = self.user
        reg.decided_at = timezone.now()
        reg.save()
        # Updating attendance via regularization service
        services.mark_attendance(
            client=self.client_obj,
            employee=self.employee,
            work_date=work_date,
            check_in=reg.requested_check_in,
            check_out=reg.requested_check_out,
            status=reg.requested_status, # "Present"
            remark=f"Regularized: {reg.reason}",
            source="regularization",
            user=self.user,
        )

        att.refresh_from_db()
        self.assertEqual(att.status, "Present")

        # 3. After regularization approval: Payroll calculates 1.0 day attended (full day pay restored)
        payroll_2 = services.process_payroll(
            client=self.client_obj,
            period_month=date(2026, 9, 1),
            employee_ids=[self.employee.id],
            user=self.user,
        )
        payslip_2 = Payslip.objects.get(payroll_run=payroll_2["run"], employee=self.employee)
        self.assertEqual(payslip_2.attended_days, Decimal("1.00"))
