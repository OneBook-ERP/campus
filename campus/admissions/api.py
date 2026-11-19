"""M04 Admissions service API. Other modules call these, never the controllers (D7)."""

import frappe
from frappe import _

from campus.admissions import admit as admission
from campus.admissions import documents, eligibility, enquiry, joining
from campus.admissions import seat_matrix as seats
from campus.admissions.aadhaar import (  # noqa: F401  (public API; copy_aadhaar is internal, not whitelisted)
	copy_aadhaar,
	reveal_aadhaar,
)
from campus.admissions.documents import grant_temporary_clearance as grant_clearance


@frappe.whitelist()
def evaluate_eligibility(applicant: str):
	"""Apply the Eligibility Rule of the applicant's program: {eligible: bool, reasons: [...]}."""
	frappe.has_permission("Student Applicant", "write", applicant, throw=True)
	return eligibility.evaluate(applicant)


def check_seat_available(program, academic_year, quota, category, applicant=None, justification=None):
	"""Block admission into a full quota/category row unless the user holds "Admission Override" (then log it).

	Call it in the same transaction that creates and submits the Program Enrollment: it locks the Seat Matrix
	row until commit, so two admissions into the last seat cannot both pass. Supernumerary / not-counted quotas
	are allowed without a check. When the program and year have no Seat Matrix, or the matrix has no row for the
	quota and category: in strict mode (M04-08, Admission Settings `strict_seat_matrix`) the same override rule
	applies; otherwise it is allowed without a check. Returns True when a seat is free (or no check applies),
	"override" when an override holder was let through (logged with its reason and the `justification` they typed,
	which an override cannot do without; the matrix is never changed).
	"""
	return seats.check_seat_available(program, academic_year, quota, category, applicant, justification)


def documents_complete(applicant):
	"""True when every mandatory document of the applicant (name) is complete (Verified, or Submitted when no verification is needed)."""
	return documents.documents_complete(applicant)


def pending_documents(applicant):
	"""Document types of the applicant's incomplete mandatory documents (empty when the applicant may be admitted)."""
	return documents.incomplete_mandatory(frappe.get_doc("Student Applicant", applicant))


@frappe.whitelist()
def set_document_status(
	applicant: str,
	document_type: str,
	status: str,
	remarks: str | None = None,
	original_retained: int | None = None,
):
	"""Verifier action on one checklist item; Verified/Rejected need the Admission Verifier role."""
	return documents.set_document_status(applicant, document_type, status, remarks, original_retained)


@frappe.whitelist(methods=["POST"])
def grant_temporary_clearance(applicant: str, documents: list | str, due_date: str, reason: str):
	"""Temporary clearance (role Admission Officer, M04-10): the applicant may be admitted and join while the listed
	pending mandatory documents are outstanding, until `due_date`; they are reminded daily. Returns {cleared_until,
	documents}."""
	return grant_clearance(applicant, documents, due_date, reason)


@frappe.whitelist(methods=["POST"])
def admit(applicant: str):
	"""Admission approval of an Approved, Eligible applicant with complete documents (role Admission Officer).

	One transaction: Student (+ Customer) in lifecycle status Admitted, applicant status Admitted. Nothing billable
	and no ID: those follow `confirm_joining` (M04-09). Returns {student}.
	"""
	return admission.admit(applicant)


@frappe.whitelist(methods=["POST"])
def confirm_joining(
	applicant: str, joining_date: str | None = None, override_justification: str | None = None
):
	"""Joining confirmation of an admitted applicant (role Admission Officer, M04-09).

	One transaction: seat check (strict mode; an override needs `override_justification`), submitted Program
	Enrollment from `joining_date` (default today, never in the future; also its attendance start), register/roll
	number, lifecycle Joined then Active, fee Sales Invoice with the student's pre-joining advances allocated,
	onboarding checklist. Idempotent per applicant: a retry returns the earlier result. Returns {student,
	program_enrollment, invoice, register_no, roll_no}.
	"""
	return joining.confirm_joining(applicant, joining_date, override_justification)


@frappe.whitelist(methods=["POST"])
def convert_lead_to_applicant(lead: str, academic_year: str | None = None):
	"""Convert an enquiry (Lead) into a Student Applicant (name, mobile, email, program, company) and link both ways.

	Idempotent: a second call returns the existing applicant's name. The academic year defaults to the current one.
	"""
	return enquiry.convert_lead_to_applicant(lead, academic_year)


@frappe.whitelist()
def enroll_student(source_name: str):
	"""Refuse Education's one-step Enroll (M04-01 / M04-09): campus admissions go through Admit, then Confirm Joining."""
	frappe.throw(_("Use Admit, then Confirm Joining."))
