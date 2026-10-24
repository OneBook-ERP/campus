"""Privacy hardening: the Student/Guardian roles read only rows linked to their own / guarded students.

Upstream Education grants those roles company-wide read on Guardian, Program Enrollment, Student Attendance,
Student Leave Application, Assessment Result, Sales Invoice and Course Schedule; campus narrows each one
(campus.examinations.access.PORTAL_SCOPES). Staff roles keep their role permissions.
"""

import frappe
from frappe.utils import add_days, today

from campus.tests.broadcast_fixtures import make_guardian
from campus.tests.test_results import (
	COLLEGE, FACULTY, GUARDIAN_USER, STUDENT_22_USER, STUDENT_26_USER, ExamFixture, make_login,
)  # fmt: skip
from campus.tests.test_student_portal import make_invoice

ACADEMICS_USER = "privacy-academics@example.com"
STUDENT_ROW_DOCTYPES = (
	"Program Enrollment", "Student Attendance", "Student Leave Application", "Assessment Result", "Sales Invoice",
)  # fmt: skip
ALL_DOCTYPES = ("Student", "Guardian", "Course Schedule", *STUDENT_ROW_DOCTYPES)


def insert_row(doctype, **values):
	doc = frappe.new_doc(doctype)
	doc.update(values)
	doc.name = values.get("name") or frappe.generate_hash(length=10)
	doc.db_insert()
	return doc.name


class TestPortalPrivacy(ExamFixture):
	def setUp(self):
		super().setUp()
		make_login(ACADEMICS_USER, "Academics User")
		self.rows = {dt: {} for dt in ALL_DOCTYPES}
		self.group22 = self.offerings22["EXM TH"]
		self.group26 = self.offerings26["EXM TH"]
		self.guardian22 = frappe.db.get_value("Guardian", {"guardian_name": "Examguardian"})
		self.guardian26 = make_guardian("Examother", [self.s26.name])
		for student, group in ((self.s22, self.group22), (self.s26, self.group26)):
			self.add_rows(student, group)

	def add_rows(self, student, group):
		rows = self.rows
		rows["Student Attendance"][student.name] = insert_row(
			"Student Attendance", student=student.name, student_group=group, date=today(), status="Present", docstatus=1
		)
		rows["Student Leave Application"][student.name] = insert_row(
			"Student Leave Application", student=student.name, student_group=group, from_date=today(),
			to_date=add_days(today(), 1), docstatus=1,
		)  # fmt: skip
		rows["Assessment Result"][student.name] = insert_row(
			"Assessment Result", student=student.name, student_group=group, docstatus=1
		)
		rows["Sales Invoice"][student.name] = f"PRIV-INV-{student.name}"
		make_invoice(student, COLLEGE, 100, 100, rows["Sales Invoice"][student.name])
		rows["Program Enrollment"][student.name] = frappe.db.get_value("Program Enrollment", {"student": student.name})
		rows["Course Schedule"][student.name] = insert_row(
			"Course Schedule", student_group=group, schedule_date=today(), course="EXM TH"
		)
		rows["Student"][student.name] = student.name
		rows["Guardian"][student.name] = self.guardian22 if student is self.s22 else self.guardian26

	def listed(self, user, doctype):
		frappe.set_user(user)
		return set(frappe.get_list(doctype, pluck="name", limit_page_length=0))

	def can_get_doc(self, user, doctype, name):
		frappe.set_user(user)
		try:
			frappe.get_doc(doctype, name).check_permission("read")
			return True
		except frappe.PermissionError:
			return False

	def test_student_reads_only_own_rows_by_list_and_get_doc(self):
		for doctype in ALL_DOCTYPES:
			with self.subTest(doctype=doctype):
				if not frappe.has_permission(doctype, "read", user=STUDENT_22_USER):
					continue  # upstream grants Guardian read to the Guardian role only
				mine, other = self.rows[doctype][self.s22.name], self.rows[doctype][self.s26.name]
				listed = self.listed(STUDENT_22_USER, doctype)
				self.assertIn(mine, listed)
				self.assertNotIn(other, listed)
				self.assertTrue(self.can_get_doc(STUDENT_22_USER, doctype, mine))
				self.assertFalse(self.can_get_doc(STUDENT_22_USER, doctype, other))

	def test_other_student_is_symmetric(self):
		for doctype in ALL_DOCTYPES:
			with self.subTest(doctype=doctype):
				if not frappe.has_permission(doctype, "read", user=STUDENT_26_USER):
					continue
				self.assertNotIn(self.rows[doctype][self.s22.name], self.listed(STUDENT_26_USER, doctype))
				self.assertFalse(self.can_get_doc(STUDENT_26_USER, doctype, self.rows[doctype][self.s22.name]))

	def test_guardian_reads_linked_student_rows_only(self):
		for doctype in ALL_DOCTYPES:
			with self.subTest(doctype=doctype):
				mine, other = self.rows[doctype][self.s22.name], self.rows[doctype][self.s26.name]
				if not frappe.has_permission(doctype, "read", user=GUARDIAN_USER):
					continue  # the Guardian role reads Sales Invoice too since the gateway fix (campus_finance.setup)
				self.assertIn(mine, self.listed(GUARDIAN_USER, doctype))
				self.assertNotIn(other, self.listed(GUARDIAN_USER, doctype))
				self.assertTrue(self.can_get_doc(GUARDIAN_USER, doctype, mine))
				self.assertFalse(self.can_get_doc(GUARDIAN_USER, doctype, other))

	def test_guardian_scope_is_own_record_plus_guardians_of_linked_students(self):
		self.assertEqual(self.listed(GUARDIAN_USER, "Guardian"), {self.guardian22})
		frappe.set_user("Administrator")
		second = make_guardian("Examsecond", [self.s22.name])
		self.assertEqual(self.listed(GUARDIAN_USER, "Guardian"), {self.guardian22, second})
		self.assertNotIn(self.guardian26, self.listed(GUARDIAN_USER, "Guardian"))

	def test_course_schedule_follows_the_groups_of_linked_students(self):
		schedule22 = self.rows["Course Schedule"][self.s22.name]
		self.assertEqual(self.listed(STUDENT_22_USER, "Course Schedule"), {schedule22})
		self.assertEqual(self.listed(GUARDIAN_USER, "Course Schedule"), {schedule22})

	def test_staff_still_see_every_row(self):
		for user in ("Administrator", ACADEMICS_USER, FACULTY):
			frappe.set_user("Administrator")
			readable = [dt for dt in ALL_DOCTYPES if frappe.has_permission(dt, "read", user=user)]
			self.assertIn("Student", readable, user)
			for doctype in readable:
				with self.subTest(user=user, doctype=doctype):
					listed = self.listed(user, doctype)
					for student in (self.s22, self.s26):
						self.assertIn(self.rows[doctype][student.name], listed)
						self.assertTrue(self.can_get_doc(user, doctype, self.rows[doctype][student.name]))
