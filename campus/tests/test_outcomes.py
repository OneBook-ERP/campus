"""P3-T09 tests (M03-02): PEO / PO / PSO / CO masters, the NBA PO seed, CO-PO matrix validation and HOD approval,
the coverage report, matrices versioned with the curriculum, and CO tags checked against the canonical Course Outcomes."""

import frappe
from frappe.utils import add_days, today

from campus.campus_platform.approvals import approve, request_approval
from campus.curriculum import api
from campus.curriculum.outcomes import (
	NBA_PROGRAM_OUTCOMES,
	seed_co_po_matrix_policy,
	seed_program_outcomes,
	validate_co_tags,
)
from campus.curriculum.report.co_po_coverage import co_po_coverage
from campus.curriculum.setup import seed_course_categories
from campus.tests.factories import make_company, make_user
from campus.tests.test_regulation import make_program, make_regulation, make_year, year_name
from campus.tests.utils import CampusTestCase

COLLEGE = "OBE Test College"
PROGRAM = "OBE Test BE"
COURSE = "OBE Data Structures"
OTHER_COURSE = "OBE Algorithms"
HOD_USER = "obe-hod@example.com"
OTHER_HOD_USER = "obe-other-hod@example.com"
CO_CODES = ("CO1", "CO2", "CO3")


def make_department(name):
	existing = frappe.db.get_value("Department", {"department_name": name, "company": COLLEGE})
	return (
		existing
		or frappe.get_doc({"doctype": "Department", "department_name": name, "company": COLLEGE})
		.insert()
		.name
	)


def make_course(name, department):
	if not frappe.db.exists("Course", name):
		frappe.get_doc(
			{"doctype": "Course", "course_name": name, "oc_company": COLLEGE, "department": department}
		).insert()
	return name


class TestOutcomes(CampusTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_company(COLLEGE, "OBTC")
		seed_course_categories()
		for year in ("2022", "2026"):
			make_year(year)
		cls.department = make_department("OBE Test Dept")
		cls.other_department = make_department("OBE Other Dept")
		make_program(PROGRAM, COLLEGE)
		frappe.db.set_value("Program", PROGRAM, {"oc_total_semesters": 8, "department": cls.department})
		make_course(COURSE, cls.department)
		make_course(OTHER_COURSE, cls.department)
		cls.r22 = make_regulation("R2022", COLLEGE, [PROGRAM], "2022").name
		cls.r26 = make_regulation("R2026", COLLEGE, [PROGRAM], "2026").name
		cls.c22 = cls.make_curriculum(cls.r22)
		cls.c26 = cls.make_curriculum(cls.r26)
		for regulation in (cls.r22, cls.r26):
			seed_program_outcomes(PROGRAM, regulation)
			for code in ("PSO1", "PSO2"):
				frappe.get_doc(
					{"doctype": "Program Specific Outcome", "program": PROGRAM, "regulation": regulation, "pso_code": code, "statement": f"Specific {code}"}
				).insert()  # fmt: skip
		for curriculum in (cls.c22, cls.c26):
			for n, code in enumerate(CO_CODES):
				frappe.get_doc(
					{"doctype": "Course Outcome", "course": COURSE, "curriculum": curriculum, "co_code": code,
					"statement": f"Outcome {code} of {curriculum}", "bloom_level": ("Remember", "Apply", "Analyse")[n]}
				).insert()  # fmt: skip
		for user, department in ((HOD_USER, cls.department), (OTHER_HOD_USER, cls.other_department)):
			make_user(user, [COLLEGE])
			frappe.get_doc("User", user).add_roles("HOD")
			if not frappe.db.exists("Position Holder", {"position": "HOD", "scope_value": department}):
				frappe.get_doc(
					{"doctype": "Position Holder", "position": "HOD", "scope_value": department, "user": user, "from_date": add_days(today(), -30)}
				).insert(ignore_permissions=True)  # fmt: skip
		seed_co_po_matrix_policy()

	@classmethod
	def make_curriculum(cls, regulation):
		name = frappe.db.get_value("Curriculum", {"program": PROGRAM, "regulation": regulation})
		if name:
			return name
		rows = [{"semester": 3, "course": COURSE, "category": "PCC", "lecture_periods": 3}]
		return (
			frappe.get_doc(
				{
					"doctype": "Curriculum",
					"program": PROGRAM,
					"regulation": regulation,
					"status": "Draft",
					"courses": rows,
				}
			)
			.insert()
			.name
		)

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint("outcomes_test")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="outcomes_test")

	def outcome(self, code, regulation=None):
		return frappe.db.get_value(
			"Program Outcome", {"program": PROGRAM, "regulation": regulation or self.r22, "po_code": code}
		)

	def course_outcome(self, code, curriculum=None):
		return frappe.db.get_value(
			"Course Outcome", {"course": COURSE, "curriculum": curriculum or self.c22, "co_code": code}
		)

	def matrix(self, curriculum=None, mapped=None):
		"""A matrix of COURSE with its grid filled; `mapped` is {(CO code, PO/PSO code): correlation}."""
		doc = frappe.get_doc(
			{"doctype": "CO-PO Matrix", "course": COURSE, "curriculum": curriculum or self.c22}
		).insert()
		for row in doc.correlations:
			row.correlation = (mapped or {}).get((row.co_code, row.outcome_code), 0)
		doc.save()
		return doc

	def fully_mapped(self, correlation=2):
		return {(co, "PO1"): correlation for co in CO_CODES}

	def submit_through_hod(self, doc):
		request = request_approval(doc.doctype, doc.name)
		frappe.set_user(HOD_USER)
		try:
			approve(request)
		finally:
			frappe.set_user("Administrator")
		doc.reload()
		return doc

	# masters
	def test_nba_program_outcomes_are_seeded_once_per_program_and_regulation(self):
		seed_program_outcomes(PROGRAM, self.r22)  # again: nothing new
		codes = frappe.get_all(
			"Program Outcome",
			{"program": PROGRAM, "regulation": self.r22},
			pluck="po_code",
			order_by="po_code",
		)
		self.assertEqual(sorted(codes, key=lambda c: int(c[2:])), [f"PO{n}" for n in range(1, 13)])
		self.assertEqual(len(NBA_PROGRAM_OUTCOMES), 12)
		po1 = frappe.get_doc("Program Outcome", self.outcome("PO1"))
		self.assertEqual(po1.title, "Engineering knowledge")
		self.assertIn("placeholder", po1.statement.lower())

	def test_a_course_outcome_is_of_a_course_of_its_curriculum_with_a_unique_code(self):
		with self.assertRaisesRegex(frappe.ValidationError, "not a course of"):
			frappe.get_doc({"doctype": "Course Outcome", "course": OTHER_COURSE, "curriculum": self.c22, "co_code": "CO1",
				"statement": "x", "bloom_level": "Apply"}).insert()  # fmt: skip
		with self.assertRaisesRegex(frappe.ValidationError, "already has"):
			frappe.get_doc({"doctype": "Course Outcome", "course": COURSE, "curriculum": self.c22, "co_code": " co1 ",
				"statement": "x", "bloom_level": "Apply"}).insert()  # fmt: skip
		co4 = frappe.get_doc({"doctype": "Course Outcome", "course": COURSE, "curriculum": self.c22, "co_code": "co4",
			"statement": "x", "bloom_level": "Create"}).insert()  # fmt: skip
		self.assertEqual(
			(co4.co_code, co4.program, co4.regulation, co4.company), ("CO4", PROGRAM, self.r22, COLLEGE)
		)

	def test_a_peo_maps_to_missions_of_its_programme_department(self):
		mission = frappe.get_doc(
			{
				"doctype": "Department Mission",
				"department": self.department,
				"mission_code": "M1",
				"statement": "Teach well",
			}
		).insert()
		other = frappe.get_doc(
			{
				"doctype": "Department Mission",
				"department": self.other_department,
				"mission_code": "M1",
				"statement": "Other",
			}
		).insert()
		peo = {
			"doctype": "Program Educational Objective",
			"program": PROGRAM,
			"regulation": self.r22,
			"peo_code": "PEO1",
			"statement": "Graduates will...",
		}
		with self.assertRaisesRegex(frappe.ValidationError, "not a mission of"):
			frappe.get_doc(
				{**peo, "mission_mappings": [{"mission": other.name, "correlation": "2"}]}
			).insert()
		doc = frappe.get_doc(
			{**peo, "mission_mappings": [{"mission": mission.name, "correlation": "3"}]}
		).insert()
		self.assertEqual(doc.mission_mappings[0].correlation, "3")

	# matrix
	def test_a_new_matrix_grids_every_co_against_every_po_and_pso_of_the_programme(self):
		doc = self.matrix()
		self.assertEqual(len(doc.correlations), 3 * 14)
		self.assertEqual({r.co_code for r in doc.correlations}, set(CO_CODES))
		self.assertEqual(
			{r.outcome_code for r in doc.correlations}, {*(f"PO{n}" for n in range(1, 13)), "PSO1", "PSO2"}
		)
		self.assertEqual(
			(doc.program, doc.regulation, doc.department, doc.company),
			(PROGRAM, self.r22, self.department, COLLEGE),
		)

	def test_cells_take_0_to_3_and_only_this_curriculums_cos_and_outcomes(self):
		doc = self.matrix()
		doc.correlations[0].correlation = 4
		with self.assertRaisesRegex(frappe.ValidationError, "0 to 3"):
			doc.save()
		doc.reload()
		doc.correlations[0].outcome = self.outcome("PO1", self.r26)
		with self.assertRaisesRegex(frappe.ValidationError, "not an outcome of"):
			doc.save()
		doc.reload()
		doc.correlations[0].course_outcome = self.course_outcome("CO1", self.c26)
		with self.assertRaisesRegex(frappe.ValidationError, "not a course outcome of"):
			doc.save()
		doc.reload()
		doc.append("correlations", {"course_outcome": doc.correlations[0].course_outcome, "outcome_type": doc.correlations[0].outcome_type,
			"outcome": doc.correlations[0].outcome, "correlation": 1})  # fmt: skip
		with self.assertRaisesRegex(frappe.ValidationError, "twice"):
			doc.save()

	def test_one_live_matrix_per_course_and_curriculum(self):
		self.matrix()
		with self.assertRaisesRegex(frappe.ValidationError, "already has"):
			self.matrix()
		self.assertTrue(self.matrix(self.c26).name)

	def test_saving_a_matrix_without_course_or_programme_outcomes_is_refused(self):
		"""Saving a matrix fails with a clear setup message when COs or POs / PSOs are missing, and only correlation is editable."""
		with self.assertRaisesRegex(frappe.ValidationError, "Define the course outcomes"):
			frappe.get_doc(
				{"doctype": "CO-PO Matrix", "course": OTHER_COURSE, "curriculum": self.c22}
			).insert()
		frappe.db.delete("Program Outcome", {"program": PROGRAM, "regulation": self.r26})
		frappe.db.delete("Program Specific Outcome", {"program": PROGRAM, "regulation": self.r26})
		with self.assertRaisesRegex(frappe.ValidationError, "Define the programme outcomes"):
			frappe.get_doc({"doctype": "CO-PO Matrix", "course": COURSE, "curriculum": self.c26}).insert()
		meta = frappe.get_meta("CO-PO Correlation")
		for fieldname in ("course_outcome", "co_code", "outcome_type", "outcome", "outcome_code"):
			self.assertEqual(meta.get_field(fieldname).read_only, 1, fieldname)
		self.assertEqual(meta.get_field("correlation").read_only, 0)

	def test_a_matrix_with_a_co_that_maps_to_nothing_cannot_be_approved(self):  # AC1
		doc = self.matrix(mapped={("CO1", "PO1"): 3, ("CO2", "PO2"): 2})
		with self.assertRaisesRegex(frappe.ValidationError, "CO3"):
			request_approval(doc.doctype, doc.name)
		with self.assertRaisesRegex(frappe.ValidationError, "CO3"):
			doc.submit()
		frappe.get_doc({"doctype": "Course Outcome", "course": COURSE, "curriculum": self.c22, "co_code": "CO4",
			"statement": "Added after the grid", "bloom_level": "Create"}).insert()  # fmt: skip
		doc = frappe.get_doc("CO-PO Matrix", doc.name)
		doc.correlations[[r.co_code for r in doc.correlations].index("CO3")].correlation = 1
		doc.save()
		with self.assertRaisesRegex(
			frappe.ValidationError, "CO4"
		):  # a CO missing from the grid maps to nothing too
			request_approval(doc.doctype, doc.name)

	def test_the_hod_of_the_course_department_approves_and_unmapped_pos_are_warned(self):  # AC1
		doc = self.matrix(mapped=self.fully_mapped())
		request = request_approval(doc.doctype, doc.name)
		frappe.set_user(OTHER_HOD_USER)
		try:
			with self.assertRaises(frappe.PermissionError):
				approve(request)
		finally:
			frappe.set_user("Administrator")
		frappe.clear_messages()
		frappe.set_user(HOD_USER)
		try:
			approve(request)
		finally:
			frappe.set_user("Administrator")
		doc.reload()
		self.assertEqual(doc.docstatus, 1)
		warned = " ".join(str(m) for m in frappe.get_message_log())
		self.assertIn("PO2", warned)
		self.assertIn("PSO1", warned)

	def test_the_coverage_report_lists_outcomes_with_zero_total_correlation_across_the_curriculum(
		self,
	):  # AC1
		self.submit_through_hod(self.matrix(mapped={**self.fully_mapped(3), ("CO1", "PSO1"): 1}))
		columns, rows, message = co_po_coverage.execute({"company": COLLEGE, "curriculum": self.c22})[:3]
		by_code = {r["outcome_code"]: r for r in rows}
		self.assertEqual(
			(by_code["PO1"]["total_correlation"], by_code["PO1"]["courses"], by_code["PO1"]["status"]),
			(9, 1, "Mapped"),
		)
		self.assertEqual(by_code["PSO1"]["total_correlation"], 1)
		self.assertEqual(by_code["PO2"]["status"], "Not mapped")
		self.assertIn("PO2", message)
		self.assertEqual(len(rows), 14)
		draft = self.matrix(self.c26, mapped={(co, "PO2"): 3 for co in CO_CODES})  # drafts do not count
		self.assertEqual(
			{
				r["outcome_code"]
				for r in co_po_coverage.execute({"company": COLLEGE, "curriculum": self.c26})[1]
				if r["total_correlation"]
			},
			set(),
		)
		self.assertTrue(draft.name)

	def test_the_coverage_report_is_for_permitted_colleges(self):
		make_company("OBE Outside College", "OBOC")
		make_user("obe-outsider@example.com", ["OBE Outside College"])
		frappe.get_doc("User", "obe-outsider@example.com").add_roles("Academics User")
		frappe.set_user("obe-outsider@example.com")
		with self.assertRaises(frappe.PermissionError):
			co_po_coverage.execute({"company": COLLEGE, "curriculum": self.c22})

	# versions
	def test_an_offering_uses_the_matrix_of_its_curriculum(self):  # AC2
		m22 = self.submit_through_hod(self.matrix(self.c22, self.fully_mapped(3)))
		self.assertIsNone(api.matrix_for(COURSE, self.c26))  # R2026 has no approved matrix yet
		m26 = self.submit_through_hod(self.matrix(self.c26, self.fully_mapped(1)))
		offering = self.offering(self.c22)
		self.assertEqual(api.matrix_for_offering(offering), m22.name)
		self.assertEqual(api.matrix_for_offering(self.offering(self.c26, "B")), m26.name)
		self.assertEqual(api.course_outcomes(COURSE, self.c22)[0].statement, f"Outcome CO1 of {self.c22}")
		self.assertIsNone(api.matrix_for_offering(self.offering(None, "C")))

	def offering(self, curriculum, section="A"):
		return frappe.get_doc(
			{"doctype": "Student Group", "student_group_name": f"OBE {curriculum} {section}", "group_based_on": "Course",
			"academic_year": year_name("2022"), "program": PROGRAM, "course": COURSE, "oc_company": COLLEGE, "oc_curriculum": curriculum}
		).insert().name  # fmt: skip

	# existing CO tags
	def test_co_tags_on_cia_questions_and_bank_items_are_cos_of_the_course(self):
		offering = self.offering(self.c22)
		schedule = frappe.get_doc({"doctype": "CIA Schedule", "course_offering": offering, "course": COURSE,
			"questions": [{"component": "CIA 1", "question": "1", "max_marks": 10, "course_outcome": "co9", "bloom_level": "Apply"}]})  # fmt: skip
		with self.assertRaisesRegex(frappe.ValidationError, "CO9"):
			validate_co_tags(schedule)
		schedule.questions[0].course_outcome = " co2"
		validate_co_tags(schedule)
		self.assertEqual(schedule.questions[0].course_outcome, "CO2")
		item = {"doctype": "Question Bank Item", "company": COLLEGE, "course": COURSE, "bloom_level": "Apply", "marks": 10,
			"difficulty": "Medium", "question": "Explain stacks"}  # fmt: skip
		with self.assertRaisesRegex(frappe.ValidationError, "CO9"):
			frappe.get_doc({**item, "course_outcome": "CO9"}).insert()
		self.assertTrue(frappe.get_doc({**item, "course_outcome": "CO3"}).insert().name)
		untagged = frappe.get_doc(
			{**item, "course": OTHER_COURSE, "course_outcome": "CO7"}
		).insert()  # no COs defined yet
		self.assertEqual(untagged.course_outcome, "CO7")

	# prints
	def test_matrix_and_course_outcome_sheet_print(self):
		doc = self.matrix(mapped=self.fully_mapped(3))
		grid = frappe.get_print("CO-PO Matrix", doc.name, "CO-PO Matrix")
		self.assertIn("PSO2", grid)
		self.assertIn("CO3", grid)
		sheet = frappe.get_print("CO-PO Matrix", doc.name, "Course Outcome Sheet")
		self.assertIn(f"Outcome CO2 of {self.c22}", sheet)
		self.assertIn("Analyse", sheet)
