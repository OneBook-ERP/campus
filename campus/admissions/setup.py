"""M04-01 Admissions module setup: Student Applicant and Student custom fields, the Aadhaar Viewer role."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from campus.admissions.aadhaar import VIEWER_ROLE

MODULE = "Admissions"
ADMISSION_TYPES = ("Regular", "Lateral", "PG", "Transfer")
OVERRIDE_ROLE = "Admission Override"  # may admit into a full row, or (strict mode) a missing matrix/row; logged
SEAT_COUNT_INDEX = "oc_seat_count_index"
CANDIDATE_INDEX = "oc_candidate_index"
APPLICANT_INDEX = "oc_student_applicant_index"
VERIFIER_ROLE = "Admission Verifier"  # may mark applicant documents Verified or Rejected
OFFICER_ROLE = "Admission Officer"  # may admit approved applicants, confirm their joining and cancel admissions
LATE_REQUEST = "Late Joining Request"  # M04-10


def setup_admissions():
	ensure_aadhaar_role()
	ensure_admission_roles()
	setup_admission_fields()
	seed_strict_seat_matrix()
	seed_late_joining_policy()
	from campus.admissions.enquiry_setup import setup_enquiry
	from campus.admissions.legacy_import import grant_import_check_rights

	setup_enquiry()
	grant_import_check_rights()


def ensure_admission_roles():
	for role in (OVERRIDE_ROLE, VERIFIER_ROLE, OFFICER_ROLE):
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(ignore_permissions=True)


def seed_strict_seat_matrix():
	"""M04-08: strict seat matrix is on for a new install. A value already stored (on or off) is never changed."""
	stored = frappe.db.sql(
		"select 1 from `tabSingles` where doctype = 'Admission Settings' and field = 'strict_seat_matrix'"
	)
	if not stored:
		frappe.db.set_single_value("Admission Settings", "strict_seat_matrix", 1)


def seed_late_joining_policy():
	"""M04-10: a late joining is approved by the Principal, unless the college has its own policy."""
	if not frappe.db.exists("Position", "PRINCIPAL") or frappe.db.exists("Approval Policy", {"document_type": LATE_REQUEST}):
		return
	frappe.get_doc(
		{
			"doctype": "Approval Policy", "policy_name": LATE_REQUEST, "document_type": LATE_REQUEST,
			"levels": [
				{"title": "Principal approval", "approver_type": "Position", "approver": "PRINCIPAL", "scope_field": "company", "sla_hours": 48}
			],
		}
	).insert(ignore_permissions=True)  # fmt: skip


def ensure_aadhaar_role():
	if not frappe.db.exists("Role", VIEWER_ROLE):
		frappe.get_doc({"doctype": "Role", "role_name": VIEWER_ROLE, "desk_access": 1}).insert(
			ignore_permissions=True
		)


def field(fieldname, fieldtype, label, insert_after, **kwargs):
	return {
		"fieldname": fieldname, "fieldtype": fieldtype, "label": label,
		"insert_after": insert_after, "module": MODULE, **kwargs,
	}  # fmt: skip


def aadhaar_fields(insert_after):
	"""Entry field (encrypted, never printed or copied) plus the read-only masked display."""
	return [
		field("oc_aadhaar_section", "Section Break", "Aadhaar", insert_after),
		field(
			"oc_aadhaar", "Password", "Aadhaar Number", "oc_aadhaar_section",
			print_hide=1, report_hide=1, no_copy=1, permlevel=0,
			description="Stored encrypted. Only the masked number is shown.",
		),
		field(
			"oc_aadhaar_masked", "Data", "Aadhaar (Masked)", "oc_aadhaar",
			read_only=1, no_copy=1, in_list_view=0,
		),
	]  # fmt: skip


def oc_lead_can_be_unique():
	"""False when existing applicants already share an enquiry: the unique index would fail migrate.

	The duplicates are logged and the constraint skipped (conversion still guards by lookup under a row
	lock); once they are merged or cleared, the next migrate creates the index.
	"""
	if not frappe.db.has_column("Student Applicant", "oc_lead"):  # fresh install: the field is not created yet
		return True
	duplicates = frappe.db.sql(
		"select oc_lead from `tabStudent Applicant` where oc_lead is not null and oc_lead != '' "
		"group by oc_lead having count(*) > 1"
	)
	if not duplicates:
		return True
	frappe.log_error(
		title="Student Applicant.oc_lead unique index skipped",
		message="Enquiries with more than one applicant: " + ", ".join(row[0] for row in duplicates),
	)
	return False


def setup_admission_fields():
	"""Create/refresh the admission fields. Idempotent."""
	applicant = [
		field("oc_admission_section", "Section Break", "Admission", "student_category"),
		field("oc_admission_type", "Select", "Admission Type", "oc_admission_section",
			options="\n" + "\n".join(ADMISSION_TYPES), in_standard_filter=1),
		field("oc_admission_quota", "Link", "Admission Quota", "oc_admission_type",
			options="Admission Quota", in_standard_filter=1),
		field("oc_counselling_authority", "Link", "Counselling Authority", "oc_admission_quota",
			options="Counselling Authority"),
		field("oc_lead", "Link", "Enquiry (Lead)", "oc_counselling_authority", options="Lead", unique=int(oc_lead_can_be_unique()), no_copy=1,
			description="One applicant per enquiry."),
		field("oc_admission_column", "Column Break", None, "oc_lead"),
		field("oc_allotment_no", "Data", "Allotment No", "oc_admission_column", search_index=1),
		field("oc_allotment_key", "Data", "Allotment Key", "oc_allotment_no", unique=1, hidden=1, read_only=1,
			no_copy=1, description="Counselling authority and allotment number: one applicant per allotment."),
		field("oc_allotment_date", "Date", "Allotment Date", "oc_allotment_key"),
		field("oc_rank", "Int", "Rank", "oc_allotment_date"),
		field("oc_cutoff", "Float", "Cutoff", "oc_rank"),
		field("oc_first_graduate", "Check", "First Graduate", "oc_cutoff"),
		field("oc_differently_abled", "Check", "Differently Abled", "oc_first_graduate"),
		field("oc_marks_section", "Section Break", "Qualifying Marks", "oc_differently_abled"),
		field("oc_qualifying_marks", "Table", "Qualifying Marks", "oc_marks_section",
			options="Applicant Qualifying Mark"),
		field("oc_qualifying_exam", "Data", "Qualifying Exam / Board", "oc_qualifying_marks"),
		field("oc_qualifying_register_no", "Data", "Qualifying Exam Register No", "oc_qualifying_exam", search_index=1,
			description="Stored as letters and digits only; used to detect duplicate candidates."),
		field("oc_candidate_key", "Data", "Candidate Key", "oc_qualifying_register_no", unique=1, hidden=1, read_only=1,
			no_copy=1, description="Register no, date of birth, program and year: one applicant per upload row."),
		field("oc_duplicate_review", "Link", "Duplicate Review", "oc_candidate_key", options="Candidate Duplicate Review",
			read_only=1, no_copy=1, description="The review that cleared this applicant as not a duplicate."),
		field("oc_eligibility_status", "Select", "Eligibility", "oc_duplicate_review",
			options="Pending\nEligible\nIneligible", default="Pending", read_only=1, no_copy=1,
			in_standard_filter=1),
		field("oc_eligibility_reasons", "Small Text", "Eligibility Reasons", "oc_eligibility_status",
			read_only=1, no_copy=1),
		field("oc_documents_section", "Section Break", "Documents", "oc_eligibility_reasons"),
		field("oc_documents", "Table", "Documents", "oc_documents_section", options="Applicant Document"),
		*aadhaar_fields("oc_documents"),
	]  # fmt: skip
	student = aadhaar_fields("student_email_id")
	enrollment = [
		field("oc_admission_quota", "Link", "Admission Quota", "student_category", options="Admission Quota",
			in_standard_filter=1, description="Counted against the Seat Matrix when submitted."),
		field("oc_attendance_start_date", "Date", "Attendance Start Date", "enrollment_date",
			description="Set only by the joining confirmation (the joining date): attendance counts from this date. Blank (e.g. a legacy or promotion enrollment): the whole term counts."),
		field("oc_joining_key", "Data", "Joining Key", "oc_attendance_start_date", unique=1, hidden=1, read_only=1,
			no_copy=1, description="The applicant whose joining confirmation made this enrollment: one per applicant."),
	]  # fmt: skip
	student += [
		field("oc_register_no", "Data", "Register No", "student_applicant", unique=1, read_only=1, no_copy=1,
			in_standard_filter=1, description="Assigned at the joining confirmation from the Admission Settings pattern."),
		field("oc_roll_no", "Data", "Roll No", "oc_register_no", read_only=1, no_copy=1),
		field("oc_reporting_deadline", "Date", "Reporting Deadline", "oc_roll_no", read_only=1, no_copy=1,
			description="Last day to join, stamped at admission from Admission Settings (M04-10)."),
	]  # fmt: skip
	category = [
		field("oc_is_reserved", "Check", "Reserved Category", "category", in_list_view=1,
			description="Seats of this category count as reserved in the IQAC data points."),
	]  # fmt: skip
	invoice = [
		field("oc_program_enrollment", "Link", "Admission Enrollment", "student", options="Program Enrollment",
			read_only=1, no_copy=1, search_index=1, description="Set on the fee invoice raised at the joining confirmation."),
	]  # fmt: skip
	payment = [
		field("oc_admission_cancellation", "Link", "Admission Cancellation", "reference_no", options="Admission Cancellation",
			hidden=1, read_only=1, set_only_once=1, search_index=1,
			description="The cancellation before joining whose advance this payment refunds (M04-10): paid out once. Copied with the entry, so a copy is checked too."),
	]  # fmt: skip
	create_custom_fields(
		{
			"Student Applicant": applicant, "Student": student, "Program Enrollment": enrollment,
			"Student Category": category, "Sales Invoice": invoice, "Payment Entry": payment,
		},
		update=True,
	)
	grant_officer_read_access()
	# the seat count locks exactly this index range (submitted enrollments of one program and year)
	frappe.db.add_index("Program Enrollment", ["oc_company", "program", "academic_year", "docstatus"], SEAT_COUNT_INDEX)
	# the duplicate candidate check locks these ranges (M04-07)
	for doctype in ("Student Applicant", "Student"):
		frappe.db.add_index(doctype, ["oc_company", "date_of_birth"], CANDIDATE_INDEX)
	# the joining confirmation locks the applicant's Student by this column (M04-09): without it, every row
	frappe.db.add_index("Student", ["student_applicant"], APPLICANT_INDEX)


# Read rights ERPNext's validation checks as the session user even when ignore_permissions is set (item details, party
# account, the Payment Entry the cancellation refund drafts, the fee invoice Confirm Joining raises); company User
# Permissions still scope the company-linked ones. Sales Invoice and Payment Entry were taken away once and are back:
# test_admission_flow.TestOfficerWithoutInvoiceRights ran Confirm Joining and both cancellations without them and failed.
# Read only: no write, create, export or print.
OFFICER_READ = ("Account", "Item", "Cost Center", "Price List", "Sales Invoice", "Payment Entry", "Course Enrollment")
# The officer's own work on Education records without the office role (Academics User): the applicants they convert,
# check and admit, the Student whose form holds Confirm Joining, the enrollments the admission reports count, and the
# masters an applicant links. Custom DocPerm: Education's DocType JSON is never edited.
OFFICER_RIGHTS = {
	"Student Applicant": ("write", "create", "report"),
	"Customer": ("write", "create"),  # Education creates the Student's Customer with the session user's rights
	"Student": ("report",),
	"Program Enrollment": ("report",),
	"Program": (),
	"Academic Year": (),
	"Student Category": (),
	"Student Admission": (),
}


def grant_officer_read_access():
	"""Add the officer's read (and OFFICER_RIGHTS) rule on each doctype the role has no rule on; a rule the college
	edited stays as it is."""
	from frappe.permissions import add_permission, update_permission_property

	for doctype in (*OFFICER_READ, *OFFICER_RIGHTS):
		if frappe.db.exists("Custom DocPerm", {"parent": doctype, "role": OFFICER_ROLE, "permlevel": 0}):
			continue
		add_permission(doctype, OFFICER_ROLE, 0)
		for right in OFFICER_RIGHTS.get(doctype, ()):
			update_permission_property(doctype, OFFICER_ROLE, 0, right, 1)
		update_permission_property(doctype, OFFICER_ROLE, 0, "export", 0)  # Custom DocPerm defaults to export 1
