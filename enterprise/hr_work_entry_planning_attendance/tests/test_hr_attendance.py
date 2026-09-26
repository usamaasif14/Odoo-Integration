from datetime import date, datetime

from odoo.tests.common import TransactionCase


class TestPlanningAttendance(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'Flexible 40 hours/week',
            'company_id': cls.env.company.id,
            'tz': 'UTC',
            'hours_per_day': 8,
            'hours_per_week': 40,
            'flexible_hours': True,
            'full_time_required_hours': 40,
        })

        cls.employee = cls.env['hr.employee'].create({
            'name': 'Planning Attendance Employee',
            'company_id': cls.env.company.id,
            'tz': 'UTC',
            'date_version': date(2026, 9, 1),
            'contract_date_start': date(2026, 9, 1),
            'resource_calendar_id': cls.calendar.id,
        })

        cls.employee.version_id.work_entry_source = 'planning'

    def test_attendance_with_overlapping_planning_allocations(self):
        self.env['planning.slot'].create([
            {
                'resource_id': self.employee.resource_id.id,
                'start_datetime': datetime(2026, 9, 8, 6, 0),
                'end_datetime': datetime(2026, 9, 8, 15, 0),
                'allocated_percentage': 100,
                'state': 'published',
            },
            {
                'resource_id': self.employee.resource_id.id,
                'start_datetime': datetime(2026, 9, 8, 6, 0),
                'end_datetime': datetime(2026, 9, 8, 15, 0),
                'allocated_percentage': 99,
                'state': 'published',
            },
        ])

        attendance = self.env['hr.attendance'].create({
            'employee_id': self.employee.id,
            'check_in': datetime(2026, 9, 8, 8, 0),
            'check_out': datetime(2026, 9, 8, 17, 0),
        })

        self.assertTrue(attendance.exists())
