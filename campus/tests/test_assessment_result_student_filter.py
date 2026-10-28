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

	def test_boot_session_hides_education_and_assessment_links_for_pilot_roles(self):
		from campus.campus_platform.pack_visibility import (
			boot_session,
			hidden_workspaces,
		)

		test_user = "pilot_test_user@example.com"
		if not frappe.db.exists("User", test_user):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": test_user,
					"first_name": "Pilot",
					"roles": [{"role": "Academics User"}],
				}
			).insert(ignore_permissions=True)

		self.addCleanup(frappe.set_user, "Administrator")

		# Admin sees everything
		frappe.set_user("Administrator")
		self.assertNotIn("Education", hidden_workspaces("Administrator"))

		# Pilot user has Education in hidden_workspaces
		frappe.set_user(test_user)
		self.assertIn("Education", hidden_workspaces(test_user))

		# boot_session strips Education from pages and strips Assessment links from sidebars and pages
		bootinfo = frappe._dict(
			workspaces={
				"pages": [
					{
						"name": "Education",
						"links": [{"link_to": "Assessment Result"}, {"link_to": "Course"}],
						"shortcuts": [{"link_to": "Assessment Plan"}, {"link_to": "Student"}],
					},
					{
						"name": "Examinations",
						"links": [{"link_to": "Mark Sheet Entry"}],
						"shortcuts": [{"link_to": "Exam Session"}],
					},
				],
				"private_pages": [],
			},
			module_sidebars={
				"Education": {
					"module": "Education",
					"workspaces": ["Education"],
					"items": [
						{"type": "Link", "link_to": "Assessment Result", "label": "Assessment Result"},
						{"type": "Link", "link_to": "Assessment Plan", "label": "Assessment Plan"},
						{"type": "Link", "link_to": "Course", "label": "Course"},
					],
				},
				"Examinations": {
					"module": "Examinations",
					"workspaces": ["Examinations"],
					"items": [
						{"type": "Link", "link_to": "Mark Sheet Entry", "label": "Mark Sheet Entry"},
					],
				},
			},
			desktop_icons=[
				{"link_to": "Assessment Result"},
				{"link_to": "Mark Sheet Entry"},
			],
			dock={"campus": []},
			app_data=[],
		)

		boot_session(bootinfo)

		# Education workspace removed from pages
		page_names = [p["name"] for p in bootinfo.workspaces["pages"]]
		self.assertNotIn("Education", page_names)
		self.assertIn("Examinations", page_names)

		# Assessment links stripped from desktop_icons
		icon_links = [i["link_to"] for i in bootinfo.desktop_icons]
		self.assertNotIn("Assessment Result", icon_links)
		self.assertIn("Mark Sheet Entry", icon_links)

		# Assessment links stripped from any remaining sidebars
		for sidebar in bootinfo.module_sidebars.values():
			items = [i.get("link_to") for i in sidebar.get("items", [])]
			self.assertNotIn("Assessment Result", items)
			self.assertNotIn("Assessment Plan", items)
