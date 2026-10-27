"""M17-01 Company scoping on academic records.

Every college is a Company (D2). Academic records carry `oc_company` so User Permissions on
Company scope lists, links and reports. This module installs the custom fields, derives the
company from parent links and rejects links across companies.
"""

import frappe
from frappe import _
from frappe.core.doctype.user_permission.user_permission import get_user_permissions
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

MODULE = "Campus Platform"

# Fee Schedule is not listed here: it already has a standard `company` field (pre-flight ruling).
COMPANY_SCOPED_DOCTYPES = [
	"Student",
	"Student Applicant",
	"Program",
	"Course",
	"Student Group",
	"Program Enrollment",
	"Course Schedule",
	"Instructor",
	"Room",
	"Student Admission",
	"Assessment Plan",
]

# Company fieldname per scoped doctype; Fee Schedule keeps its own standard field.
COMPANY_FIELD = {
	**dict.fromkeys(COMPANY_SCOPED_DOCTYPES, "oc_company"),
	"Fee Schedule": "company",
	"Regulation": "company",
	"Curriculum": "company",
}

# doctype -> (link field on the doc, doctype it points to) whose company is inherited.
PARENT_LINKS = {
	"Program Enrollment": ("program", "Program"),
	"Course Schedule": ("student_group", "Student Group"),
	"Student Group": ("program", "Program"),
}

# doctype -> [(table fieldname or None, link fieldname, linked doctype)] that must share the company.
LINKED_RECORDS = {
	"Curriculum": [(None, "program", "Program"), (None, "regulation", "Regulation")],
	"Student Applicant": [(None, "program", "Program"), (None, "student_admission", "Student Admission")],
	"Program": [("courses", "course", "Course")],
	"Student Group": [
		(None, "program", "Program"),
		(None, "course", "Course"),
		("students", "student", "Student"),
		("instructors", "instructor", "Instructor"),
	],
	"Program Enrollment": [(None, "student", "Student"), (None, "program", "Program")],
	"Course Schedule": [
		(None, "student_group", "Student Group"),
		(None, "instructor", "Instructor"),
		(None, "course", "Course"),
		(None, "room", "Room"),
		(None, "program", "Program"),
	],
	"Student Admission": [("program_details", "program", "Program")],
	"Assessment Plan": [
		(None, "student_group", "Student Group"),
		(None, "course", "Course"),
		(None, "program", "Program"),
		(None, "room", "Room"),
		(None, "examiner", "Instructor"),
		(None, "supervisor", "Instructor"),
	],
	"Regulation": [("programs", "program", "Program")],
	"Fee Schedule": [(None, "program", "Program"), ("student_groups", "student_group", "Student Group")],
}


def setup_company_fields():
	"""Create/refresh `oc_company` on all scoped doctypes. Idempotent; run on install and migrate."""
	field = {
		"fieldname": "oc_company",
		"label": "Company",
		"fieldtype": "Link",
		"options": "Company",
		"reqd": 1,
		"in_standard_filter": 1,
		"module": MODULE,
	}
	create_custom_fields({dt: [field] for dt in COMPANY_SCOPED_DOCTYPES}, update=True)


def permitted_companies(user):
	"""Companies the user is restricted to by User Permission, or None when unrestricted."""
	restricted = get_user_permissions(user).get("Company")
	return [p["doc"] for p in restricted] if restricted else None


def before_validate(doc, method=None):
	"""Set the company from the parent link, falling back to the user's default company."""
	company_field = COMPANY_FIELD.get(doc.doctype, "oc_company")
	parent_company = get_parent_company(doc)

	company = (
		parent_company
		or doc.get(company_field)
		or frappe.defaults.get_user_default("Company")
		or frappe.db.get_single_value("Global Defaults", "default_company")
	)
	if company:
		doc.set(company_field, company)


def get_parent_company(doc):
	parent = PARENT_LINKS.get(doc.doctype)
	if not parent:
		return None
	link_field, linked_doctype = parent
	name = doc.get(link_field)
	return name and frappe.db.get_value(linked_doctype, name, COMPANY_FIELD[linked_doctype])


def validate_linked_companies(doc, method=None):
	"""Reject links to records of another company."""
	company = doc.get(COMPANY_FIELD[doc.doctype])
	if not company:
		return
	for table, link_field, linked_doctype in LINKED_RECORDS.get(doc.doctype, []):
		rows = doc.get(table) if table else [doc]
		names = list({row.get(link_field) for row in rows if row.get(link_field)})
		if not names:
			continue
		company_field = COMPANY_FIELD[linked_doctype]
		linked = frappe.get_all(
			linked_doctype, filters={"name": ["in", names]}, fields=["name", company_field]
		)
		for record in linked:
			linked_company = record.get(company_field)
			if linked_company and linked_company != company:
				frappe.throw(
					_("{0} {1} belongs to {2}, but this {3} belongs to {4}.").format(
						_(linked_doctype),
						frappe.bold(record.name),
						frappe.bold(linked_company),
						_(doc.doctype),
						frappe.bold(company),
					),
					title=_("Company Mismatch"),
				)
