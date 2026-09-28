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

    def test_multiple_punches_and_working_hours(self):
        """09:32 IN, 13:05 OUT, 13:42 IN, 18:21 OUT -> 8h 12m"""
        work_date = date(2026, 9, 28)
        t1 = timezone.make_aware(datetime.combine(work_date, time(9, 32)))
        t2 = timezone.make_aware(datetime.combine(work_date, time(13, 5)))
        t3 = timezone.make_aware(datetime.combine(work_date, time(13, 42)))
        t4 = timezone.make_aware(datetime.combine(work_date, time(18, 21)))

        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t1, user=self.user)
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="OUT", punch_time=t2, user=self.user)
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t3, user=self.user)
        p4, status = services.record_punch(client=self.client_obj, employee=self.employee, punch_type="OUT", punch_time=t4, user=self.user)

        self.assertEqual(len(status["punches"]), 4)
        self.assertEqual(len(status["pairs"]), 2)
        # Morning: 09:32 to 13:05 = 3h 33m = 213m
        # Afternoon: 13:42 to 18:21 = 4h 39m = 279m
        # Total: 492m = 8h 12m = 8.20 hours
        self.assertEqual(status["formatted_working_time"], "08h 12m")
        self.assertEqual(status["working_hours"], 8.2)

        att = Attendance.objects.get(client=self.client_obj, employee=self.employee, work_date=work_date)
        self.assertEqual(att.working_hours, Decimal("8.20"))
        # Grace is 15 min past 09:30 = 09:45, so 09:32 is on time
        self.assertEqual(att.late_minutes, 0)
        self.assertEqual(att.status, "Present")

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
        t2 = timezone.make_aware(datetime.combine(work_date, time(13, 0)))
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="OUT", punch_time=t2, user=self.user)
        att.refresh_from_db()
        self.assertEqual(att.check_out, t2)

        # After second punch in (e.g. back from lunch), check_out is None while on duty
        t3 = timezone.make_aware(datetime.combine(work_date, time(14, 0)))
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="IN", punch_time=t3, user=self.user)
        att.refresh_from_db()
        self.assertIsNone(att.check_out)

        # After final punch out, check_out is the latest out time
        t4 = timezone.make_aware(datetime.combine(work_date, time(18, 30)))
        services.record_punch(client=self.client_obj, employee=self.employee, punch_type="OUT", punch_time=t4, user=self.user)
        att.refresh_from_db()
        self.assertEqual(att.check_out, t4)

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
