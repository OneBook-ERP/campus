"""P1-T28 (M17-07) tests: import templates, pre-import validator error classes, import idempotency by register no."""

import csv
import io
import os
import tempfile

import frappe
from frappe.core.doctype.data_import.importer import Importer

from campus.admissions import legacy_import
from campus.admissions.aadhaar import verhoeff_valid
from campus.admissions.legacy_import import TEMPLATES, template_columns, validate_rows
from campus.campus_finance import api as finance
from campus.campus_finance.fee_heads import category_for_type
from campus.tests.factories import make_company, make_user
from campus.tests.finance_factories import make_company_with_accounts, make_finance_company
from campus.tests.test_regulation import make_program, make_regulation, make_year, regulation_name
from campus.tests.utils import CampusTestCase

COLLEGE, OTHER_COLLEGE = "Legacy Import College", "Legacy Import Other College"
PROGRAM = "Legacy Import BE"
SCOPED_USER = "legacy.import.scoped@example.com"


def valid_aadhaar(seed):
	base = f"{2000000000 + seed:010d}0"[:11]
	return next(base + str(d) for d in range(10) if verhoeff_valid(base + str(d)))


class LegacyFixture(CampusTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_company(COLLEGE, "LIC")
		make_company(OTHER_COLLEGE, "LIO")
		make_year("2022")
		make_program(PROGRAM, COLLEGE)
		cls.regulation = regulation_name("LI22", COLLEGE)
		make_regulation("LI22", COLLEGE, [PROGRAM], "2022")

	def setUp(self):
		frappe.db.savepoint("legacy_import")
		self.addCleanup(frappe.db.rollback, save_point="legacy_import")

	def guardian(self, guardian_name):
		"""ID of the Guardian called `guardian_name` (Guardian is named by series)."""
		return (
			frappe.db.get_value("Guardian", {"guardian_name": guardian_name})
			or frappe.get_doc({"doctype": "Guardian", "guardian_name": guardian_name}).insert().name
		)

	def student_row(self, number, **over):
		"""A valid Students sheet row, keyed by template label."""
		row = {
			"Register No": number, "First Name": f"Legacy{number}", "Student Email Address": f"{number.lower()}@legacy.example.com",
			"Company": COLLEGE, "Regulation": self.regulation, "Aadhaar Number": valid_aadhaar(int(number[-3:])),
			"Guardian (Guardians)": self.guardian("Legacy Parent"),
		}  # fmt: skip
		return {**row, **over}


def sheet(template, *rows):
	"""Header plus rows for the given label dicts; columns are the template's, blank when a row omits one."""
	labels = [c["label"] for c in template_columns(template)]
	return [labels] + [[row.get(label, "") for label in labels] for row in rows]


def checks(issues):
	return sorted((i["row"], i["check"]) for i in issues)


class TestTemplates(CampusTestCase):
	def test_every_mapped_field_is_a_template_column(self):
		for name, spec in TEMPLATES.items():
			listed = sum(len(fields) for fields in spec["fields"].values())
			self.assertEqual(len(template_columns(name)), listed, f"{name}: a mapped field is not exportable")
			columns = {c["fieldname"] for c in template_columns(name)}
			self.assertTrue(set(spec["required"]) <= columns, f"{name}: required field outside the template")

	def test_students_template_carries_guardians_regulation_and_aadhaar(self):
		labels = [c["label"] for c in template_columns("Students")]
		for label in ("Register No", "Guardian (Guardians)", "Regulation", "Aadhaar Number"):
			self.assertIn(label, labels)
		enrolment = {c["fieldname"] for c in template_columns("Student Enrolments")}
		self.assertTrue({"student_category", "oc_admission_quota"} <= enrolment)

	def test_every_template_doctype_allows_data_import(self):
		for name, spec in TEMPLATES.items():
			self.assertTrue(
				frappe.get_meta(spec["doctype"]).allow_import, f"{name}: {spec['doctype']} is not importable"
			)

	def test_opening_balances_are_opening_sales_invoices(self):
		columns = {c["fieldname"] for c in template_columns("Opening Fee Balances")}
		self.assertTrue({"is_opening", "student", "income_account"} <= columns)


class TestValidator(LegacyFixture):
	def validate(self, *rows, company=COLLEGE):
		return validate_rows("Students", sheet("Students", *rows), company)

	def test_a_clean_sheet_has_no_issues(self):
		self.assertEqual(self.validate(self.student_row("LI001"), self.student_row("LI002")), [])

	def test_missing_masters(self):
		issues = self.validate(
			self.student_row(
				"LI001", Regulation="No Such Regulation", **{"Guardian (Guardians)": "No Such Guardian"}
			)
		)
		self.assertEqual(checks(issues), [(2, "missing_master"), (2, "missing_master")])
		self.assertEqual({i["column"] for i in issues}, {"Regulation", "Guardian (Guardians)"})

	def test_bad_aadhaar_checksum_is_masked_in_full(self):
		good = valid_aadhaar(7)
		bad = good[:-1] + str((int(good[-1]) + 1) % 10)
		issues = self.validate(self.student_row("LI001", **{"Aadhaar Number": bad}))
		self.assertEqual(checks(issues), [(2, "bad_aadhaar")])
		self.assertIn("XXXX-XXXX-" + bad[-4:], issues[0]["message"])
		self.assertNotIn(bad, repr(issues))
		self.assertNotIn(bad[:8], repr(issues))

	def test_excel_numeric_aadhaar_cells_are_read_as_integers(self):
		good = valid_aadhaar(9)
		self.assertEqual(self.validate(self.student_row("LI001", **{"Aadhaar Number": float(good)})), [])

	def test_duplicate_register_numbers_in_the_file(self):
		issues = self.validate(
			self.student_row("LI001"), self.student_row("LI002"), self.student_row("LI001")
		)
		self.assertEqual(checks(issues), [(4, "duplicate_in_file")])
		self.assertIn("row 2", issues[0]["message"])

	def test_register_number_already_in_the_database(self):
		frappe.get_doc({"doctype": "Student", "first_name": "Stored", "student_email_id": "stored@legacy.example.com",
			"oc_company": COLLEGE, "oc_register_no": "LI009"}).insert()  # fmt: skip
		self.assertEqual(
			checks(self.validate(self.student_row("LI009"), self.student_row("LI010"))),
			[(2, "already_imported")],
		)

	def test_missing_guardian_and_regulation(self):
		issues = self.validate(self.student_row("LI001", **{"Guardian (Guardians)": "", "Regulation": ""}))
		self.assertEqual(checks(issues), [(2, "missing_value"), (2, "missing_value")])

	def test_an_other_company_row_gets_only_the_company_message(self):
		bad = valid_aadhaar(3)[:-1] + str((int(valid_aadhaar(3)[-1]) + 1) % 10)
		row = self.student_row(
			"LI001", **{"Company": OTHER_COLLEGE, "Regulation": "No Such Regulation", "Aadhaar Number": bad}
		)
		issues = self.validate(row, self.student_row("LI002"))
		self.assertEqual(checks(issues), [(2, "other_company")])

	def test_masters_of_another_company_count_as_missing(self):
		other_program = make_program("Legacy Import Other BE", OTHER_COLLEGE)
		outsider = self.stored_student("LI090", OTHER_COLLEGE)
		row = {"Student": outsider, "Program": other_program, "Academic Year": "RT-2022", "Company": COLLEGE}
		issues = validate_rows("Student Enrolments", sheet("Student Enrolments", row), COLLEGE)
		self.assertEqual(
			{i["column"] for i in issues if i["check"] == "missing_master"}, {"Student", "Program"}
		)

	def test_master_names_match_case_insensitively(self):
		self.assertEqual(self.validate(self.student_row("LI001", Regulation=self.regulation.lower())), [])

	def test_register_number_of_another_company_is_in_use_not_imported(self):
		self.stored_student("LI009", OTHER_COLLEGE)
		self.assertEqual(checks(self.validate(self.student_row("LI009"))), [(2, "register_in_use")])

	def stored_student(self, register_no, company):
		return frappe.get_doc({"doctype": "Student", "first_name": f"Stored{register_no}", "oc_company": company,
			"student_email_id": f"stored-{register_no.lower()}@legacy.example.com", "oc_register_no": register_no}).insert().name  # fmt: skip

	def test_child_required_values_are_checked_on_every_child_line(self):
		course = {"Program": PROGRAM, "Regulation": self.regulation, "Status": "Draft", "Company": COLLEGE,
			"Course (Courses)": "LI Course", "Sem (Courses)": "1", "Category (Courses)": "PCC"}  # fmt: skip
		second = {"Course (Courses)": "LI Course 2", "Category (Courses)": "PCC"}  # no semester
		issues = validate_rows("Curriculum Rows", sheet("Curriculum Rows", course, second), COLLEGE)
		self.assertIn((3, "missing_value"), checks(issues))
		self.assertEqual([i["column"] for i in issues if i["check"] == "missing_value"], ["Sem (Courses)"])

	def test_enrolment_sheet_flags_missing_quota_and_category(self):
		row = {"Student": "EDU-STU-1", "Program": PROGRAM, "Academic Year": "RT-2022", "Company": COLLEGE}
		issues = validate_rows("Student Enrolments", sheet("Student Enrolments", row), COLLEGE)
		self.assertEqual(
			{i["column"] for i in issues if i["check"] == "missing_value"},
			{"Student Category", "Admission Quota"},
		)

	def test_unknown_and_missing_columns_are_reported_on_the_header_row(self):
		rows = [["Register No", "Shoe Size"], ["LI001", "9"]]
		found = {(i["row"], i["check"]) for i in validate_rows("Students", rows, COLLEGE)}
		self.assertIn((1, "unknown_column"), found)
		self.assertIn((1, "missing_column"), found)

	def test_second_guardian_row_belongs_to_the_same_student(self):
		second = {"Guardian (Guardians)": self.guardian("Legacy Parent Two")}
		self.assertEqual(self.validate(self.student_row("LI001"), second, self.student_row("LI002")), [])

	def test_masters_are_checked_in_other_templates(self):
		issues = validate_rows(
			"Instructors", sheet("Instructors", {"Instructor Name": "X", "Employee": "HR-EMP-NOPE"}), COLLEGE
		)
		self.assertEqual(checks(issues), [(2, "missing_master")])

	def test_validate_import_reads_the_file_attached_to_a_data_import(self):
		data_import = self.data_import(
			self.student_row("LI001"),
			self.student_row("LI001", **{"Student Email Address": "x@legacy.example.com"}),
		)
		issues = legacy_import.validate_import(data_import, "Students", COLLEGE)
		self.assertEqual(checks(issues), [(3, "duplicate_in_file")])
		with self.assertRaises(frappe.ValidationError):
			legacy_import.validate_import(data_import, "Programs", COLLEGE)  # the Data Import is for Student

	def test_validate_import_needs_a_permitted_company(self):
		data_import = self.data_import(self.student_row("LI001"))
		self.as_scoped_user(OTHER_COLLEGE)
		with self.assertRaises(frappe.PermissionError):
			legacy_import.validate_import(data_import, "Students", COLLEGE)

	def test_validate_import_needs_data_import_read(self):
		data_import = self.data_import(self.student_row("LI001"))
		self.as_scoped_user(COLLEGE)  # permitted company, but no Data Import role
		with self.assertRaises(frappe.PermissionError):
			legacy_import.validate_import(data_import, "Students", COLLEGE)

	def as_scoped_user(self, company):
		make_user(SCOPED_USER, [company])
		self.addCleanup(frappe.set_user, frappe.session.user)
		frappe.set_user(SCOPED_USER)

	def data_import(self, *rows):
		"""A Data Import of Student with the sheet attached as a private CSV File."""
		file = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "legacy-students.csv",
				"is_private": 1,
				"content": csv_text(sheet("Students", *rows)),
			}
		).insert()
		self.addCleanup(remove_from_disk, file.get_full_path())
		return (
			frappe.get_doc(
				{
					"doctype": "Data Import",
					"reference_doctype": "Student",
					"import_type": "Insert New Records",
					"import_file": file.file_url,
				}
			)
			.insert()
			.name
		)


def csv_text(rows):
	out = io.StringIO()
	csv.writer(out).writerows(rows)
	return out.getvalue()


def remove_from_disk(path):
	if os.path.exists(path):
		os.remove(path)


class TestImportIdempotency(LegacyFixture):
	"""The acceptance flow on a generated sheet: validate, import, fix the flagged rows, import again."""

	def setUp(self):
		super().setUp()
		was_importing, frappe.flags.in_import = (
			frappe.flags.in_import,
			True,
		)  # skip the user-creation throttle
		self.addCleanup(setattr, frappe.flags, "in_import", was_importing)

	ROWS = 50
	BAD_AADHAAR, BAD_REGULATION, REPEATED = 11, 21, 31  # sheet rows (the header is row 1)

	def legacy_rows(self):
		return [self.student_row(f"LI{n:03d}") for n in range(1, self.ROWS + 1)]

	def flawed_rows(self):
		rows = self.legacy_rows()
		good = rows[self.BAD_AADHAAR - 2]["Aadhaar Number"]
		rows[self.BAD_AADHAAR - 2]["Aadhaar Number"] = good[:-1] + str((int(good[-1]) + 1) % 10)
		rows[self.BAD_REGULATION - 2]["Regulation"] = "No Such Regulation"
		rows[self.REPEATED - 2]["Register No"] = "LI005"
		return rows

	def write_sheet(self, rows):
		handle, path = tempfile.mkstemp(suffix=".csv")
		self.addCleanup(os.remove, path)
		with os.fdopen(handle, "w", newline="") as out:
			out.write(csv_text(sheet("Students", *rows)))
		return path

	def validate_file(self, path):
		with open(path) as handle:
			return validate_rows("Students", legacy_import.read_csv_content(handle.read()), COLLEGE)

	def run_import(self, path):
		"""Data Import's own steps (Importer.import_data commits per row, so a savepoint stands in for it here).

		Returns (created, {sheet row: error text}) for Insert New Records, or None when the importer refuses the
		whole file over template warnings (a Link value that does not exist, for one).
		"""
		# an unsaved Data Import is passed because Frappe 16.53's Importer reads custom_delimiters off the argument itself
		data_import = frappe.get_doc(doctype="Data Import", import_type="Insert New Records")
		importer = Importer("Student", data_import=data_import, file_path=path, console=True)
		importer.before_import()  # 16.53 sets the importer's per-run state here (import flags, tree-alias lookup)
		self.addCleanup(importer.after_import)
		if [w for w in importer.import_file.get_warnings() if w.get("type") != "info"]:
			return None
		created, failed = 0, {}
		for payload in importer.import_file.get_payloads_for_import():
			frappe.db.savepoint("legacy_row")
			try:
				importer.insert_record(payload.doc)
				created += 1
			except frappe.ValidationError as error:
				frappe.db.rollback(save_point="legacy_row")
				failed[payload.rows[0].row_number] = str(error)
		frappe.clear_messages()
		return created, failed

	def count(self):
		return frappe.db.count("Student", {"oc_register_no": ["like", "LI0%"]})

	def test_flagged_rows_are_fixed_and_the_sheet_reimports_without_duplicates(self):
		rows = self.flawed_rows()
		self.assertEqual(
			checks(self.validate_file(self.write_sheet(rows))),
			[
				(self.BAD_AADHAAR, "bad_aadhaar"),
				(self.BAD_REGULATION, "missing_master"),
				(self.REPEATED, "duplicate_in_file"),
			],
		)
		self.assertIsNone(self.run_import(self.write_sheet(rows)))  # a missing master stops the whole file

		rows[self.BAD_REGULATION - 2]["Regulation"] = (
			self.regulation
		)  # only the master fixed: the other two fail alone
		created, failed = self.run_import(self.write_sheet(rows))
		self.assertEqual((created, sorted(failed)), (self.ROWS - 2, [self.BAD_AADHAAR, self.REPEATED]))

		fixed_rows = self.legacy_rows()
		fixed_rows[self.REPEATED - 2]["Register No"] = "LI051"  # the repeated register number corrected
		fixed = self.write_sheet(fixed_rows)
		again = self.validate_file(fixed)
		self.assertEqual({i["check"] for i in again}, {"already_imported"})
		self.assertEqual(len(again), self.ROWS - 2)
		self.assertEqual(self.run_import(fixed)[0], 2)
		self.assertEqual(self.count(), self.ROWS)
		self.assertEqual(frappe.db.count("Student", {"oc_register_no": "LI005"}), 1)

	def test_reimport_is_idempotent_by_register_number(self):
		self.assertEqual(self.run_import(self.write_sheet(self.legacy_rows())), (self.ROWS, {}))
		# a re-export with corrected e-mails: only the register number still matches the stored Students
		rows = [
			{**row, "Student Email Address": "re-" + row["Student Email Address"]}
			for row in self.legacy_rows()
		]
		created, failed = self.run_import(self.write_sheet(rows))
		self.assertEqual((created, len(failed)), (0, self.ROWS))
		self.assertTrue(
			all("oc_register_no" in error for error in failed.values()), next(iter(failed.values()))
		)
		self.assertEqual(self.count(), self.ROWS)

	def test_the_aadhaar_of_an_imported_student_is_stored_masked(self):
		self.run_import(self.write_sheet(self.legacy_rows()[:1]))
		stored = frappe.db.get_value("Student", {"oc_register_no": "LI001"}, "oc_aadhaar_masked")
		self.assertEqual(stored, "XXXX-XXXX-" + valid_aadhaar(1)[-4:])


class TestOpeningBalances(LegacyFixture):
	"""Opening Fee Balances: opening invoices only, once per student, into the company's opening and receivable accounts."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_finance_company(COLLEGE, "LIC")
		make_company_with_accounts(OTHER_COLLEGE, "LIO")

	def setUp(self):
		super().setUp()
		self.a, self.b = (self.student(n) for n in ("A", "B"))

	def student(self, suffix):
		return frappe.get_doc({"doctype": "Student", "first_name": f"Opening{suffix}", "oc_company": COLLEGE,
			"student_email_id": f"opening-{suffix.lower()}-{frappe.generate_hash(length=6)}@legacy.example.com"}).insert().name  # fmt: skip

	def row(self, student, **over):
		"""An opening row keyed by fieldname; the sheet uses the template labels."""
		values = {"customer": frappe.db.get_value("Student", student, "customer"), "student": student, "company": COLLEGE,
			"posting_date": "2026-04-01", "is_opening": "Yes", "item_code": category_for_type("Tuition"), "qty": "1",
			"rate": "25000", "income_account": "Temporary Opening - LIC"}  # fmt: skip
		return {**values, **over}

	def validate(self, *rows):
		labels = {c["fieldname"]: c["label"] for c in template_columns("Opening Fee Balances")}
		labelled = [{labels[f]: v for f, v in row.items()} for row in rows]
		return validate_rows("Opening Fee Balances", sheet("Opening Fee Balances", *labelled), COLLEGE)

	def test_a_clean_sheet_has_no_issues(self):
		self.assertEqual(self.validate(self.row(self.a), self.row(self.b, debit_to="Debtors - LIC")), [])

	def test_a_row_must_be_an_opening_entry(self):
		self.assertEqual(checks(self.validate(self.row(self.a, is_opening="No"))), [(2, "not_opening")])

	def test_a_caution_deposit_is_not_an_opening_fee_balance(self):
		deposit = {"item_code": category_for_type("Caution Deposit"), "qty": "1", "rate": "5000", "income_account": "Temporary Opening - LIC"}
		issues = self.validate(self.row(self.a), deposit, self.row(self.b, item_code=category_for_type("Caution Deposit")))
		self.assertEqual(checks(issues), [(3, "caution_deposit"), (4, "caution_deposit")])

	def test_a_student_already_given_an_opening_balance(self):
		opened = finance.raise_fee_invoice(self.a, "Tuition", 1000, COLLEGE, "Legacy opening")
		frappe.db.set_value("Sales Invoice", opened.name, "is_opening", "Yes")
		cancelled = finance.raise_fee_invoice(self.b, "Tuition", 1000, COLLEGE, "Legacy opening")
		frappe.db.set_value("Sales Invoice", cancelled.name, {"is_opening": "Yes", "docstatus": 2})
		finance.raise_fee_invoice(self.b, "Tuition", 1000, COLLEGE, "A regular fee invoice")
		self.assertEqual(checks(self.validate(self.row(self.a), self.row(self.b))), [(2, "already_imported")])

	def test_a_student_repeated_in_the_file(self):
		issues = self.validate(self.row(self.a), self.row(self.b), self.row(self.a))
		self.assertEqual(checks(issues), [(4, "duplicate_in_file")])
		self.assertIn("row 2", issues[0]["message"])

	def test_income_account_must_be_a_temporary_account_of_the_company(self):
		issues = self.validate(
			self.row(self.a, income_account="Sales - LIC"),
			self.row(self.b, income_account="Temporary Opening - LIO"),
		)
		self.assertEqual(
			checks(issues), [(2, "bad_income_account"), (3, "missing_master")]
		)  # LIO's account is not LIC's

	def test_every_item_line_is_checked(self):
		second_line = {
			"item_code": category_for_type("Tuition"),
			"qty": "1",
			"income_account": "Sales - LIC",
		}  # no rate
		issues = self.validate(self.row(self.a), second_line)
		self.assertEqual(checks(issues), [(3, "bad_income_account"), (3, "missing_value")])

	def test_debit_to_must_be_a_receivable_of_the_company(self):
		issues = self.validate(
			self.row(self.a, debit_to="Sales - LIC"), self.row(self.b, debit_to="Debtors - LIO")
		)
		self.assertEqual(checks(issues), [(2, "bad_debit_to"), (3, "missing_master")])

	def test_customer_must_be_the_students_customer(self):
		other_customer = frappe.db.get_value("Student", self.b, "customer")
		issues = self.validate(self.row(self.a, customer=other_customer))
		self.assertEqual(checks(issues), [(2, "customer_mismatch")])

	def test_a_student_of_another_company_is_a_missing_master_and_nothing_more(self):
		outsider = frappe.get_doc({"doctype": "Student", "first_name": "OpeningOutsider", "oc_company": OTHER_COLLEGE,
			"student_email_id": f"opening-out-{frappe.generate_hash(length=6)}@legacy.example.com"}).insert().name  # fmt: skip
		issues = self.validate(
			self.row(outsider, customer=frappe.db.get_value("Student", self.a, "customer"))
		)
		self.assertEqual(checks(issues), [(2, "missing_master")])
		self.assertNotIn(frappe.db.get_value("Student", outsider, "customer"), repr(issues))


class TestImportCheckAccess(CampusTestCase):
	"""UAT admission-officer step 3: the Admission Officer opens Import Check on their own Data Imports only."""

	def setUp(self):
		frappe.db.savepoint("import_check_access")
		self.addCleanup(frappe.db.rollback, save_point="import_check_access")
		self.addCleanup(frappe.set_user, "Administrator")
		self.officer, self.other = "legacy.import.officer@example.com", "legacy.import.other-officer@example.com"
		for user in (self.officer, self.other):
			frappe.get_doc(
				{"doctype": "User", "email": user, "first_name": "officer", "send_welcome_email": 0, "roles": [{"role": legacy_import.IMPORT_CHECK_ROLE}]}
			).insert()  # fmt: skip

	def test_the_officer_opens_the_report_and_only_their_own_imports(self):
		from frappe.desk.query_report import get_report_doc

		# made by Administrator, then owned by the officer: creating one needs import rights on the template's doctype
		own = frappe.get_doc({"doctype": "Data Import", "reference_doctype": "Student", "import_type": "Insert New Records"}).insert()
		frappe.db.set_value("Data Import", own.name, "owner", self.officer)
		frappe.set_user(self.officer)
		self.assertEqual(get_report_doc("Import Check").name, "Import Check")
		self.assertTrue(frappe.has_permission("Data Import", "read", own.name))
		self.assertEqual(frappe.get_list("Data Import", pluck="name"), [own.name])
		frappe.set_user(self.other)
		self.assertEqual(get_report_doc("Import Check").name, "Import Check")
		self.assertFalse(frappe.has_permission("Data Import", "read", own.name))
		self.assertEqual(frappe.get_list("Data Import", pluck="name"), [])
		frappe.set_user("Administrator")
		self.assertIn(own.name, frappe.get_list("Data Import", pluck="name"))  # System Manager reads every import
