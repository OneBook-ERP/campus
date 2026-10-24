"""M17-07 (partial) Initial data import: Data Import templates and a pre-import validator.

No new DocType: the load itself is Frappe's Data Import. `TEMPLATES` is the documented column map of each
sheet; `download_import_template` hands out the stock Data Import template for it (CSV or Excel), so the
header labels are exactly what the importer matches. A multi-row sheet follows the stock convention: a
child-table row (a second guardian, a second curriculum course) sits on the following line with the parent
cells blank.

Sheets and the order to load them
1. Programs, Courses, Employees, Instructors, then Curriculum Rows (Curriculum plus its courses). Fee Structures
   (one per programme and quota, a component per line) load on their own; the pilot kit's fee step makes the same.
2. Students: Register No, name, e-mail, Company, Batch, Regulation, Aadhaar and the Guardians rows. The
   Guardian masters must exist (Guardian is imported on its own or created at enquiry).
3. Student Enrolments: Program Enrollment rows carrying the Category and the Admission Quota. The Student
   column holds the Student ID that step 2 created (export the Students list to look it up).
4. Opening Fee Balances: Sales Invoices flagged Is Opening = Yes, one line per balance, income account the
   Temporary Opening account. A Data Import of Sales Invoice is thinner than the Opening Invoice Creation
   Tool, which cannot carry the student link (`student`).

`validate_import` (and the Import Check report over it) reads the file of a Data Import without importing
anything and lists, per sheet row: masters (Link values) that do not exist, required cells that are empty
(guardian, quota, category, regulation...), Aadhaar numbers failing the Verhoeff checksum, register numbers
repeated in the file or already on a Student, and rows of another company. Aadhaar values are never
echoed: messages carry only the masked number (last four digits). Masters with a company are looked up in the
checked company only (or with no company), names compare case-insensitively, and a row of another company gets
only the other-company issue. Child-table cells (a guardian, a curriculum course, an invoice line) are required
on every child line.

Opening Fee Balances also checks that every row is Is Opening = Yes, that no line is a Caution Deposit (a liability,
not dues), that a student appears once in the file and has no opening invoice (not cancelled) in the company yet, that the customer is the student's own, that each
line's income account is a Temporary account and the Debit To (when given) a Receivable account, both of the
company.

Data Import itself refuses a whole file whose Link cells name a missing master, so fix those first; a row
failing on its own (bad Aadhaar, repeated register number) fails alone and the rest go in. Curriculum is the
one campus DocType here and allows import for System Manager and Dean Academics.

Idempotency: `Student.oc_register_no` is unique. Importing a sheet again cannot create a second Student: a
row whose register number is already stored fails alone, and the validator lists it as "already imported" so
it can be taken out of the file; the fixed rows go in.

The 500-row run: validate the legacy sheet, fix the flagged rows, import (Insert New Records), and import the
fixed rows again; the Student count equals the number of distinct register numbers.
"""

import frappe
from frappe import _
from frappe.core.doctype.data_import.data_import import download_template
from frappe.core.doctype.data_import.exporter import Exporter
from frappe.utils import cstr
from frappe.utils.csvutils import read_csv_content
from frappe.utils.xlsxutils import read_xlsx_file_from_attached_file

from campus.admissions.aadhaar import AADHAAR_FIELD, is_valid_aadhaar, mask
from campus.campus_finance import api as finance
from campus.campus_platform.company_scope import permitted_companies

REGISTER_FIELD = "oc_register_no"
COMPANY_FIELDS = ("oc_company", "company")

TEMPLATES = {
	"Programs": {
		"doctype": "Program",
		"fields": {
			"Program": [
				"program_name",
				"program_abbreviation",
				"department",
				"oc_company",
				"oc_level",
				"oc_degree",
				"oc_discipline",
				"oc_duration_years",
				"oc_total_semesters",
				"oc_sanctioned_intake",
			]
		},
		"required": ["program_name", "oc_company"],
	},
	"Courses": {
		"doctype": "Course",
		"fields": {
			"Course": [
				"course_name",
				"department",
				"oc_company",
				"oc_course_code",
				"oc_course_type",
				"oc_lecture_hours",
				"oc_tutorial_hours",
				"oc_practical_hours",
				"oc_credits",
			]
		},
		"required": ["course_name", "oc_company"],
	},
	"Curriculum Rows": {
		"doctype": "Curriculum",
		"fields": {
			"Curriculum": ["program", "regulation", "version", "status", "company"],
			"courses": [
				"semester",
				"course",
				"category",
				"credits",
				"is_elective",
				"exam_type",
				"cia_max",
				"ese_max",
			],
		},
		"required": ["program", "regulation", "status", "company", "course", "semester", "category"],
	},
	"Students": {
		"doctype": "Student",
		"fields": {
			"Student": [
				REGISTER_FIELD,
				"first_name",
				"middle_name",
				"last_name",
				"student_email_id",
				"student_mobile_number",
				"date_of_birth",
				"gender",
				"joining_date",
				"oc_company",
				"oc_batch",
				"oc_regulation",
				AADHAAR_FIELD,
				"address_line_1",
				"city",
				"state",
				"pincode",
			],
			"guardians": ["guardian", "guardian_name", "relation"],
		},
		"required": [
			REGISTER_FIELD,
			"first_name",
			"student_email_id",
			"oc_company",
			"oc_regulation",
			"guardian",
		],
	},
	"Student Enrolments": {
		"doctype": "Program Enrollment",
		"fields": {
			"Program Enrollment": [
				"student",
				"program",
				"academic_year",
				"enrollment_date",
				"student_category",
				"oc_admission_quota",
				"oc_company",
			]
		},
		"required": [
			"student",
			"program",
			"academic_year",
			"student_category",
			"oc_admission_quota",
			"oc_company",
		],
	},
	"Employees": {
		"doctype": "Employee",
		"fields": {
			"Employee": [
				"employee_number",
				"first_name",
				"last_name",
				"gender",
				"date_of_birth",
				"date_of_joining",
				"company",
				"department",
				"designation",
				"status",
				"cell_number",
				"personal_email",
			]
		},
		"required": ["first_name", "gender", "date_of_birth", "date_of_joining", "company", "status"],
	},
	"Instructors": {
		"doctype": "Instructor",
		"fields": {"Instructor": ["instructor_name", "employee", "department", "status", "gender"]},
		"required": ["instructor_name", "employee"],
	},
	"Fee Structures": {
		"doctype": "Fee Structure",
		"fields": {
			"Fee Structure": [
				"company",
				"program",
				"academic_year",
				"oc_admission_quota",
				"receivable_account",
				"cost_center",
			],
			"components": ["fees_category", "amount"],
		},
		"required": ["company", "program", "academic_year", "receivable_account", "cost_center", "fees_category", "amount"],
	},
	"Opening Fee Balances": {
		"doctype": "Sales Invoice",
		"fields": {
			"Sales Invoice": [
				"customer",
				"student",
				"company",
				"posting_date",
				"due_date",
				"is_opening",
				"debit_to",
				"set_posting_time",
			],
			"items": ["item_code", "qty", "rate", "income_account", "cost_center"],
		},
		"required": [
			"customer",
			"student",
			"company",
			"posting_date",
			"is_opening",
			"item_code",
			"rate",
			"income_account",
		],
	},
}


@frappe.whitelist()
def download_import_template(template: str, file_type: str = "CSV"):
	"""Respond with the blank Data Import template of one sheet."""
	spec = template_spec(template)
	return download_template(spec["doctype"], spec["fields"], "blank_template", file_type)


# The Admission Officer checks the batch files they load (UAT admission-officer step 3). The Import Check report needs
# doctype-level report rights on Data Import, which an if_owner rule does not give, so the rule is plain and
# own_imports_query / own_import_has_permission narrow the officer to the imports they made: no other import or its
# file is opened.
IMPORT_CHECK_ROLE = "Admission Officer"
IMPORT_CHECK_RIGHTS = {"read": 1, "report": 1}  # no write/create: the officer cannot start or stop an import
FIRST_CUT_RIGHTS = {"read": 1, "write": 1, "create": 1, "report": 1}  # the earlier rule, which patch narrow_officer_rights trims
OWN_IMPORTS_EXEMPT = ("System Manager",)  # Data Import's own role reads every import


def grant_import_check_rights():
	"""Add the role's rule when it has none (a rule the college edited stays). The first cut of this rule was if_owner,
	which kept the report shut; that exact rule is turned into the plain one (the patch narrow_officer_rights takes
	write and create away from a rule that still has them)."""
	from frappe.permissions import setup_custom_perms

	rule = frappe.db.get_value(
		"Custom DocPerm", {"parent": "Data Import", "role": IMPORT_CHECK_ROLE, "permlevel": 0},
		["name", "if_owner", *FIRST_CUT_RIGHTS], as_dict=True,
	)  # fmt: skip
	if rule:
		if rule.if_owner and all(rule[right] == value for right, value in FIRST_CUT_RIGHTS.items()):
			frappe.db.set_value("Custom DocPerm", rule.name, {"if_owner": 0, "write": 0, "create": 0, "export": 0})
			frappe.clear_cache(doctype="Data Import")
		return
	setup_custom_perms("Data Import")  # copies the standard rules first, so System Manager keeps its own
	frappe.get_doc(
		{
			"doctype": "Custom DocPerm", "parent": "Data Import", "parenttype": "DocType", "parentfield": "permissions",
			"role": IMPORT_CHECK_ROLE, "permlevel": 0, "export": 0, **IMPORT_CHECK_RIGHTS,
		}
	).insert(ignore_permissions=True)  # fmt: skip
	frappe.clear_cache(doctype="Data Import")


def limited_to_own_imports(user):
	roles = set(frappe.get_roles(user))
	return IMPORT_CHECK_ROLE in roles and not roles & set(OWN_IMPORTS_EXEMPT)


def own_imports_query(user=None):
	"""permission_query_conditions for Data Import: an Admission Officer lists only the imports they made."""
	user = user or frappe.session.user
	if limited_to_own_imports(user):
		return f"`tabData Import`.owner = {frappe.db.escape(user)}"
	return ""


def own_import_has_permission(doc, ptype=None, user=None):
	"""Deny-only: an Admission Officer opens only the imports they made."""
	user = user or frappe.session.user
	return not limited_to_own_imports(user) or doc.owner == user


@frappe.whitelist()
def validate_import(data_import: str, template: str, company: str):
	"""Issues of the file attached to a Data Import, without importing it: [{row, column, check, message}]."""
	permitted = permitted_companies(frappe.session.user)
	if permitted is not None and company not in permitted:
		frappe.throw(
			_("You are not permitted to check imports for company {0}.").format(company),
			frappe.PermissionError,
		)
	frappe.has_permission("Data Import", "read", data_import, throw=True)
	spec = template_spec(template)
	doc = frappe.get_doc("Data Import", data_import)
	if doc.reference_doctype != spec["doctype"]:
		frappe.throw(
			_("Data Import {0} is for {1}, not {2}.").format(
				data_import, doc.reference_doctype, spec["doctype"]
			)
		)
	return validate_rows(template, read_rows(doc.import_file), company)


def template_spec(template):
	if template not in TEMPLATES:
		frappe.throw(_("Unknown import template {0}.").format(template))
	return TEMPLATES[template]


def read_rows(file_url):
	content = frappe.get_doc("File", {"file_url": file_url}).get_content()
	if file_url.lower().endswith(".xlsx"):
		return read_xlsx_file_from_attached_file(fcontent=content, read_only=True)
	return read_csv_content(content)


def template_columns(template):
	"""[{label, fieldname, link, child}] in template order, as the stock exporter labels them."""
	spec = template_spec(template)
	exporter = Exporter(spec["doctype"], export_fields=spec["fields"], export_data=False)
	return [
		{"label": label, "fieldname": df.fieldname, "link": df.options if df.fieldtype == "Link" else None,
			"child": bool(df.is_child_table_field)}
		for label, df in zip(exporter.csv_array[0], exporter.fields, strict=True)
	]  # fmt: skip


def validate_rows(template, rows, company):
	"""Issues of `rows` (header first, sheet rows numbered from 1) against the template, sorted by row."""
	columns = template_columns(template)
	by_label = {c["label"].strip().lower(): c for c in columns}
	issues = []
	header = [by_label.get(cstr(cell).strip().lower()) for cell in rows[0]] if rows else []
	for index, cell in enumerate(rows[0] if rows else []):
		if header[index] is None and cstr(cell).strip():
			issues.append(
				issue(1, cell, "unknown_column", _("Column is not in the {0} template.").format(template))
			)
	present = {c["fieldname"] for c in header if c}
	for fieldname in template_spec(template)["required"]:
		if fieldname not in present:
			issues.append(
				issue(1, fieldname, "missing_column", _("Required column {0} is absent.").format(fieldname))
			)
	records = group_records(rows[1:], header)
	checks_class = OpeningBalanceChecks if template == OPENING_TEMPLATE else RowChecks
	checks = checks_class(template, company, records, columns)
	for record in records:
		issues += checks.run(record)
	return sorted(issues + checks.masters(), key=lambda i: (i["row"], i["column"]))


def group_records(body, header):
	"""[{row, values, lines, link_cells}]: a row starts a record when any parent cell is filled.

	`values` maps a fieldname to every filled cell of the record; `lines` holds the child-table cells of each
	sheet row that has any ({row, values: {fieldname: cell}})."""
	records = []
	for offset, cells in enumerate(body, start=2):
		filled = [
			(col, cell_text(cell))
			for cell, col in zip(cells, header, strict=False)
			if col and cell_text(cell)
		]
		if any(not col["child"] for col, _text in filled) or not records:
			records.append({"row": offset, "values": {}, "lines": [], "link_cells": []})
		record = records[-1]
		line = {col["fieldname"]: text for col, text in filled if col["child"]}
		if line:
			record["lines"].append({"row": offset, "values": line})
		for col, text in filled:
			record["values"].setdefault(col["fieldname"], []).append(text)
			if col["link"]:
				record["link_cells"].append((offset, col, text))
	return records


def cell_text(cell):
	"""Cell as text: spreadsheets hand numbers back as floats (an Aadhaar or register number as 1.23e11)."""
	if isinstance(cell, float) and cell.is_integer():
		cell = int(cell)
	return cstr(cell).strip()


def issue(row, column, check, message):
	return {"row": row, "column": cstr(column), "check": check, "message": message}


def company_field(doctype):
	"""The company field of `doctype` (campus `oc_company` or the stock `company`), or None for shared masters."""
	meta = frappe.get_meta(doctype)
	return next((f for f in COMPANY_FIELDS if meta.has_field(f)), None)


class RowChecks:
	"""Per-row checks of one sheet. A row of another company gets only the other_company issue: nothing about
	that company's records is looked up or reported."""

	def __init__(self, template, company, records, columns):
		self.spec = template_spec(template)
		self.company = company
		self.columns = {c["fieldname"]: c for c in columns}
		self.records = records
		self.other_rows = set()
		self.register_rows = {}
		self.stored_registers = self.stored_register_numbers(records)

	def stored_register_numbers(self, records):
		"""{register no: company of the Student holding it}."""
		numbers = {v for r in records for v in r["values"].get(REGISTER_FIELD, [])}
		if not numbers or self.spec["doctype"] != "Student":
			return {}
		rows = frappe.get_all(
			"Student", {REGISTER_FIELD: ["in", list(numbers)]}, [REGISTER_FIELD, "oc_company"]
		)
		return {r[REGISTER_FIELD]: r.oc_company for r in rows}

	def run(self, record):
		other = self.other_company(record)
		if other:
			self.other_rows.add(record["row"])
			return other
		return self.required(record) + self.aadhaar(record) + self.register(record)

	def label(self, fieldname):
		return self.columns[fieldname]["label"]

	def required(self, record):
		"""Parent cells once per record; child cells on every child line (a record without lines misses them on its row)."""
		lines = record["lines"] or [{"row": record["row"], "values": {}}]
		found = []
		for fieldname in self.spec["required"]:
			column = self.columns.get(fieldname)
			if not column:
				continue
			rows = (
				[line["row"] for line in lines if fieldname not in line["values"]]
				if column["child"]
				else ([record["row"]] if fieldname not in record["values"] else [])
			)
			found += [
				issue(row, column["label"], "missing_value", _("{0} is required.").format(column["label"]))
				for row in rows
			]
		return found

	def aadhaar(self, record):
		return [
			issue(record["row"], self.label(AADHAAR_FIELD), "bad_aadhaar",
				_("Aadhaar {0} fails the checksum or format.").format(mask(number)))
			for number in record["values"].get(AADHAAR_FIELD, [])
			if not is_valid_aadhaar(number)
		]  # fmt: skip

	def register(self, record):
		found = []
		for number in record["values"].get(REGISTER_FIELD, []):
			label = self.label(REGISTER_FIELD)
			if number in self.register_rows:
				found.append(issue(record["row"], label, "duplicate_in_file",
					_("Register No {0} is also on row {1}.").format(number, self.register_rows[number])))  # fmt: skip
			else:
				self.register_rows[number] = record["row"]
			if number not in self.stored_registers:
				continue
			if self.stored_registers[number] == self.company:
				found.append(issue(record["row"], label, "already_imported",
					_("Register No {0} already belongs to a Student; this row would fail and create no duplicate.").format(number)))  # fmt: skip
			else:
				found.append(issue(record["row"], label, "register_in_use",
					_("Register No {0} is already in use; this row would fail.").format(number)))  # fmt: skip
		return found

	def other_company(self, record):
		found = []
		for fieldname in COMPANY_FIELDS:
			for value in record["values"].get(fieldname, []):
				if value != self.company and fieldname in self.columns:
					found.append(issue(record["row"], self.label(fieldname), "other_company",
						_("Row belongs to company {0}, not {1}.").format(value, self.company)))  # fmt: skip
		return found

	def masters(self):
		"""One lookup per linked doctype (limited to this company's and shared records), then an issue per cell whose
		value is missing. Names compare case-insensitively, as the database and Data Import's link check do."""
		cells = [
			cell
			for record in self.records
			if record["row"] not in self.other_rows
			for cell in record["link_cells"]
		]
		values = {}
		for _row, col, text in cells:
			values.setdefault(col["link"], set()).add(text)
		known = {
			doctype: {name.casefold() for name in self.company_names(doctype, names)}
			for doctype, names in values.items()
		}
		return [
			self.missing_master(row, col, text)
			for row, col, text in cells
			if text.casefold() not in known[col["link"]]
		]

	def missing_master(self, row, col, text):
		if company_field(col["link"]):
			message = _("{0} {1} does not exist in {2}.").format(_(col["link"]), text, self.company)
		else:
			message = _("{0} {1} does not exist.").format(_(col["link"]), text)
		return issue(row, col["label"], "missing_master", message)

	def company_names(self, doctype, names):
		"""Those of `names` that exist as `doctype` records of this company or of no company."""
		fieldname = company_field(doctype)
		or_filters = [[fieldname, "=", self.company], [fieldname, "is", "not set"]] if fieldname else None
		return frappe.get_all(doctype, {"name": ["in", list(names)]}, pluck="name", or_filters=or_filters)


OPENING_TEMPLATE = "Opening Fee Balances"


class OpeningBalanceChecks(RowChecks):
	"""Opening Fee Balances: one opening invoice per student, booked to the company's Temporary and receivable accounts."""

	def __init__(self, template, company, records, columns):
		super().__init__(template, company, records, columns)
		students = {v for r in records for v in r["values"].get("student", [])}
		self.customers = dict(
			frappe.get_all(
				"Student",
				{"name": ["in", list(students) or [""]], "oc_company": company},
				["name", "customer"],
				as_list=True,
			)
		)
		self.opened = set(frappe.get_all("Sales Invoice", {"student": ["in", list(students) or [""]], "company": company,
			"is_opening": "Yes", "docstatus": ["<", 2]}, pluck="student"))  # fmt: skip
		accounts = {
			v for r in records for f in ("income_account", "debit_to") for v in r["values"].get(f, [])
		}
		self.account_types = dict(frappe.get_all("Account", {"name": ["in", list(accounts) or [""]], "company": company},
			["name", "account_type"], as_list=True))  # fmt: skip
		self.student_rows = {}
		self.head_types = finance.head_types()

	def run(self, record):
		found = super().run(record)
		if record["row"] in self.other_rows:
			return found
		return found + self.opening(record) + self.student(record) + self.accounts(record) + self.deposits(record)

	def opening(self, record):
		return [
			issue(record["row"], self.label("is_opening"), "not_opening", _("Is Opening Entry must be Yes, not {0}.").format(value))
			for value in record["values"].get("is_opening", [])
			if value != "Yes"
		]  # fmt: skip

	def student(self, record):
		found = []
		label = self.label("student")
		for student in record["values"].get("student", []):
			if student in self.student_rows:
				found.append(issue(record["row"], label, "duplicate_in_file",
					_("Student {0} is also on row {1}.").format(student, self.student_rows[student])))  # fmt: skip
			else:
				self.student_rows[student] = record["row"]
			if student in self.opened:
				found.append(issue(record["row"], label, "already_imported",
					_("Student {0} already has an opening invoice in {1}.").format(student, self.company)))  # fmt: skip
			customer, given = self.customers.get(student), record["values"].get("customer")
			if student in self.customers and given and customer not in given:
				found.append(issue(record["row"], self.label("customer"), "customer_mismatch",
					_("The customer of Student {0} is {1}.").format(student, customer or _("not set"))))  # fmt: skip
		return found

	def deposits(self, record):
		"""A caution deposit held is a liability, not fee dues: an opening invoice line would book it to income and
		make it refundable as a deposit the invoice never received."""
		return [
			issue(line["row"], self.label("item_code"), "caution_deposit",
				_("{0} is a caution deposit; bring deposits held forward by Journal Entry to the deposit account, not as opening dues.").format(
					line["values"]["item_code"]))
			for line in record["lines"]
			if self.head_types.get(line["values"].get("item_code")) == finance.CAUTION_DEPOSIT
		]  # fmt: skip

	def accounts(self, record):
		"""Each line's income account is a Temporary account and the debit-to a Receivable, both of this company
		(an account of another company is reported as a missing master)."""
		cells = [
			(line["row"], "income_account", line["values"].get("income_account")) for line in record["lines"]
		]
		cells += [(record["row"], "debit_to", value) for value in record["values"].get("debit_to", [])]
		wanted = {"income_account": "Temporary", "debit_to": "Receivable"}
		return [
			issue(row, self.label(fieldname), f"bad_{fieldname}",
				_("{0} {1} is not a {2} account.").format(self.label(fieldname), account, wanted[fieldname]))
			for row, fieldname, account in cells
			if account in self.account_types and self.account_types[account] != wanted[fieldname]
		]  # fmt: skip
