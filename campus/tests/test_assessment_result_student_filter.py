import frappe
from frappe.tests import IntegrationTestCase

from campus.examinations.api import get_students_for_assessment_result

COLLEGE_A = "T11 College"
COLLEGE_B = "T11 Other College"


class TestAssessmentResultStudentFilter(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.year = "2026-2027"
		if not frappe.db.exists("Academic Year", cls.year):
			frappe.get_doc({"doctype": "Academic Year", "academic_year_name": cls.year}).insert()

		cls.program = frappe.get_doc(
			{
				"doctype": "Program",
				"program_name": f"Test Program {frappe.generate_hash(length=4)}",
				"program_code": f"TP-{frappe.generate_hash(length=4)}",
				"oc_company": COLLEGE_A,
			}
		).insert()

		cls.student1 = frappe.get_doc(
			{
				"doctype": "Student",
				"first_name": "Alice",
				"last_name": "FilterTest",
				"student_email_id": f"alice_{frappe.generate_hash(length=4)}@example.com",
				"enabled": 1,
				"oc_company": COLLEGE_A,
			}
		).insert()

		cls.student2 = frappe.get_doc(
			{
				"doctype": "Student",
				"first_name": "Bob",
				"last_name": "FilterTest",
				"student_email_id": f"bob_{frappe.generate_hash(length=4)}@example.com",
				"enabled": 1,
				"oc_company": COLLEGE_A,
			}
		).insert()

		cls.student3 = frappe.get_doc(
			{
				"doctype": "Student",
				"first_name": "Charlie",
				"last_name": "FilterTest",
				"student_email_id": f"charlie_{frappe.generate_hash(length=4)}@example.com",
				"enabled": 1,
				"oc_company": COLLEGE_A,
			}
		).insert()

		cls.group = frappe.get_doc(
			{
				"doctype": "Student Group",
				"student_group_name": f"SG {frappe.generate_hash(length=6)}",
				"group_based_on": "Activity",
				"academic_year": cls.year,
				"program": cls.program.name,
				"oc_company": COLLEGE_A,
				"students": [
					{"student": cls.student1.name, "active": 1},
					{"student": cls.student3.name, "active": 0},
				],
			}
		).insert()

	def test_filters_by_student_group(self):
		results = get_students_for_assessment_result(
			"Student", "", "name", 0, 20, {"student_group": self.group.name}
		)
		student_names = [r[0] for r in results]
		self.assertIn(self.student1.name, student_names)
		self.assertNotIn(self.student2.name, student_names)
		self.assertNotIn(self.student3.name, student_names)

	def test_filters_by_search_text(self):
		results = get_students_for_assessment_result(
			"Student", "Alice", "name", 0, 20, {"student_group": self.group.name}
		)
		student_names = [r[0] for r in results]
		self.assertIn(self.student1.name, student_names)

		results_none = get_students_for_assessment_result(
			"Student", "NonExistent", "name", 0, 20, {"student_group": self.group.name}
		)
		self.assertEqual(len(results_none), 0)

	def test_empty_when_no_group_or_program(self):
		results = get_students_for_assessment_result("Student", "", "name", 0, 20, {})
		self.assertEqual(results, [])

	def test_filter_by_program(self):
		frappe.get_doc(
			{
				"doctype": "Program Enrollment",
				"student": self.student2.name,
				"program": self.program.name,
				"academic_year": self.year,
			}
		).insert()
		results = get_students_for_assessment_result(
			"Student", "", "name", 0, 20, {"program": self.program.name}
		)
		student_names = [r[0] for r in results]
		self.assertIn(self.student2.name, student_names)

	def test_cross_company_students_are_excluded(self):
		other_student = frappe.get_doc(
			{
				"doctype": "Student",
				"first_name": "Dave",
				"last_name": "OtherCo",
				"student_email_id": f"dave_{frappe.generate_hash(length=4)}@example.com",
				"enabled": 1,
				"oc_company": COLLEGE_B,
			}
		).insert()

		frappe.get_doc(
			{
				"doctype": "Student Group Student",
				"parent": self.group.name,
				"parenttype": "Student Group",
				"parentfield": "students",
				"student": other_student.name,
				"active": 1,
			}
		).db_insert()

		results = get_students_for_assessment_result(
			"Student", "", "name", 0, 20, {"student_group": self.group.name}
		)
		student_names = [r[0] for r in results]
		self.assertNotIn(other_student.name, student_names)

	def test_field_order_layout(self):
		import json
		meta = frappe.get_meta("Assessment Result")
		field_order = json.loads(meta.field_order) if isinstance(meta.field_order, str) else meta.field_order
		cb_idx = field_order.index("column_break_3")
		col1 = field_order[:cb_idx]
		col2 = field_order[cb_idx + 1 :]
		# Row 1: Assessment Plan & Program
		self.assertEqual(col1[0], "assessment_plan")
		self.assertEqual(col2[0], "program")
		# Row 2: Student Group & Student
		self.assertEqual(col1[1], "student_group")
		self.assertEqual(col2[1], "student")
