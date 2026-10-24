"""M04-04 Admission Cancellation: refund by Refund Rule slab, Credit Note, draft Payment Entry, seat freed.

On submit, in the submit's transaction: a Credit Note against the admission fee invoice for the refundable
portion (invoice total less the slab deduction), a draft Payment Entry (Pay) for the money the student paid
(Payment Entries received, not scholarships or credit notes) in excess of the deduction (accounts approves and
submits it), cancellation of the Program Enrollment (the Seat Matrix hook frees the seat), and the student's and applicant's statuses. The applicant becomes "Rejected":
Education's Student Applicant status has no cancelled value and this app does not change upstream options.
A cancellation is final: cancel the Credit Note and Payment Entry in Accounts if it was made in error.
While a scholarship disbursement settles the invoice the cancellation is refused: the credit note would turn the
scholarship into the student's credit, so Accounts cancels the disbursement voucher first. Submit takes the invoice's
row lock and reads what was paid with locking reads.

M04-10 Before joining: an Admitted student who never joined (a no-show) has no Program Enrollment, invoice or seat.
Their cancellation names no enrollment: on submit the advance they paid (unallocated Payment Entries against their
customer, less refunds already paid) gets a draft refund Payment Entry in full (no fee was charged, so no slab
deduction applies), and the student moves Admitted -> Cancelled. Nothing else is released: nothing was held. Submit
takes the applicant's and the Student's row locks (the joining's lock order), re-reads the status under them and
reads the advance with locking reads; one cancellation per student. The draft refund names its cancellation
(`oc_admission_cancellation`, copied with the entry), and its submit is refused when another submitted entry
already refunds that cancellation or the advance left (locking reads) is less than the refund: it is paid once.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, today

from campus.admissions import refund
from campus.admissions.admit import check_company_access
from campus.campus_finance import api as finance
from campus.student_services.api import ADMITTED, CANCELLED, last_change_date, transition


class AdmissionCancellation(Document):
	def validate(self):
		self.check_company()
		if self.program_enrollment:
			self.validate_enrollment()
		else:
			self.validate_not_joined()
		self.validate_date()
		if self.program_enrollment:
			self.compute_refund()
		else:
			self.compute_advance_refund()

	def check_company(self):
		"""Before anything is read back to the user: an officer limited to another company gets no figures."""
		if self.program_enrollment:
			company = frappe.db.get_value("Program Enrollment", self.program_enrollment, "oc_company")
		else:
			company = frappe.db.get_value("Student", self.student, "oc_company")
		check_company_access(company)
		self.company = company

	def validate_date(self):
		"""Cancelled on or after the admission, not in the future (the refund slab counts from this date)."""
		if self.program_enrollment:
			admitted = frappe.db.get_value("Program Enrollment", self.program_enrollment, "enrollment_date")
		else:
			admitted = last_change_date(self.student)
		if getdate(self.cancellation_date) > getdate(today()):
			frappe.throw(_("The cancellation date cannot be in the future."))
		if admitted and getdate(self.cancellation_date) < getdate(admitted):
			frappe.throw(_("The cancellation date cannot be before the admission date {0}.").format(admitted))

	def validate_not_joined(self):
		"""Without an enrollment only a student still Admitted (not joined) is cancelled; on submit the status is read
		under the applicant's and the Student's row locks, so a joining cannot pass in between."""
		locking = self.docstatus == 1
		applicant = frappe.db.get_value("Student", self.student, "student_applicant")
		if locking and applicant:
			frappe.db.get_value("Student Applicant", applicant, "name", for_update=True)
		status = frappe.db.get_value("Student", self.student, "oc_academic_status", for_update=locking)
		if status != ADMITTED:
			frappe.throw(
				_("{0} is {1}: name the Program Enrollment to cancel an admission after joining.").format(
					frappe.bold(self.student), _(status or "")
				)
			)
		clash = frappe.db.exists("Admission Cancellation", {"student": self.student, "docstatus": 1, "name": ("!=", self.name)})
		if clash:
			frappe.throw(_("{0} already cancels this admission.").format(clash))

	def compute_advance_refund(self):
		"""No invoice and no fee: the whole advance is refunded (read with locking reads on submit)."""
		customer = frappe.db.get_value("Student", self.student, "customer")
		advance = finance.unallocated_advance(customer, self.company, locking=self.docstatus == 1) if customer else 0
		self.update(
			{
				"fee_invoice": None, "refund_rule": None, "admission_start": None, "days_since_start": 0, "invoice_total": 0,
				"amount_paid": advance, "deduction": 0, "refundable_amount": 0, "cash_refund": advance,
			}
		)  # fmt: skip

	def validate_enrollment(self):
		enrollment = frappe.db.get_value(
			"Program Enrollment", self.program_enrollment, ["student", "docstatus"], as_dict=True
		)
		if not enrollment or enrollment.docstatus != 1:
			frappe.throw(_("Program Enrollment {0} is not submitted.").format(self.program_enrollment))
		if enrollment.student != self.student:
			frappe.throw(_("Program Enrollment {0} belongs to another student.").format(self.program_enrollment))
		clash = frappe.db.exists(
			"Admission Cancellation", {"program_enrollment": self.program_enrollment, "docstatus": 1, "name": ("!=", self.name)}
		)
		if clash:
			frappe.throw(_("{0} already cancels this admission.").format(clash))

	def compute_refund(self):
		enrollment = frappe.db.get_value(
			"Program Enrollment", self.program_enrollment, ["oc_company", "academic_year"], as_dict=True
		)
		invoice = admission_invoice(self.program_enrollment)
		locking = self.docstatus == 1
		if locking:  # submitting: lock the invoice, then read what was paid with locking reads
			frappe.db.get_value("Sales Invoice", invoice.name, "name", for_update=True)
		check_no_scholarship(invoice.name, locking)
		rule = refund.find_refund_rule(enrollment.oc_company, enrollment.academic_year)
		start = frappe.db.get_value("Academic Year", enrollment.academic_year, "year_start_date")
		days = refund.days_since_start(start, self.cancellation_date)
		result = refund.refund_for(rule.slabs, days, invoice.grand_total)
		paid = finance.paid_by_student(invoice.name, locking=locking)
		self.update(
			{
				"fee_invoice": invoice.name, "refund_rule": rule.name, "admission_start": start, "days_since_start": days,
				"invoice_total": invoice.grand_total, "amount_paid": paid, "deduction": result["deduction"],
				"refundable_amount": result["refundable"], "cash_refund": min(max(paid - result["deduction"], 0), result["refundable"]),
			}
		)  # fmt: skip

	def on_submit(self):
		if self.program_enrollment:
			credit_note = make_credit_note(self) if self.refundable_amount else None
			payment = make_refund_payment(self, credit_note) if credit_note and self.cash_refund else None
			cancel_enrollment(self.program_enrollment)
		else:  # not joined: no invoice, enrollment or seat to release
			credit_note, payment = None, make_advance_refund(self) if self.cash_refund else None
		close_records(self)
		self.db_set({"credit_note": credit_note and credit_note.name, "payment_entry": payment and payment.name})

	def before_cancel(self):
		frappe.throw(_("An Admission Cancellation cannot be cancelled; reverse its Credit Note and Payment Entry in Accounts."))


def admission_invoice(program_enrollment):
	name = frappe.db.get_value(
		"Sales Invoice", {"oc_program_enrollment": program_enrollment, "docstatus": 1, "is_return": 0}, "name", order_by="creation desc"
	)
	if not name:
		frappe.throw(_("No submitted fee invoice was raised for Program Enrollment {0}.").format(program_enrollment))
	return frappe.get_doc("Sales Invoice", name)


def check_no_scholarship(invoice, locking):
	vouchers = finance.scholarship_vouchers(invoice, locking=locking)
	if vouchers:
		frappe.throw(
			_("{0} is settled by scholarship disbursement {1}. Cancel it in Accounts first, so the scholarship is not refunded.").format(
				invoice, ", ".join(vouchers)
			)
		)


def make_credit_note(cancellation):
	"""Credit Note against the fee invoice for `refundable_amount` (shared builder: campus_finance.refunds)."""
	invoice = frappe.get_doc("Sales Invoice", cancellation.fee_invoice)
	return finance.make_credit_note(invoice, cancellation.refundable_amount)


def make_refund_payment(cancellation, credit_note):
	"""Draft Payment Entry (Pay) against the credit note, for accounts to review and submit; the cancellation date is in the remarks."""
	remarks = _("Refund on cancellation of admission {0} dated {1}: {2}").format(
		cancellation.name, cancellation.cancellation_date, cancellation.reason
	)
	return finance.make_refund_payment(credit_note, cancellation.cash_refund, cancellation.name, remarks)


def make_advance_refund(cancellation):
	"""Draft Payment Entry (Pay) refunding the advance of a student cancelled before joining, for accounts to submit."""
	remarks = _("Refund of the advance on cancellation of admission {0} dated {1}: {2}").format(
		cancellation.name, cancellation.cancellation_date, cancellation.reason
	)
	customer = frappe.db.get_value("Student", cancellation.student, "customer")
	return finance.make_advance_refund(
		customer, cancellation.company, cancellation.cash_refund, cancellation.name, remarks,
		oc_admission_cancellation=cancellation.name,
	)  # fmt: skip


def check_advance_refund(doc, method=None):
	"""Payment Entry before_submit: an advance refund of a cancellation before joining is paid once, and never more
	than the advance left. The cancellation's row lock serialises its refunds; the facts are re-read FOR UPDATE."""
	cancellation = doc.get("oc_admission_cancellation")
	if not cancellation:
		return
	student, company = frappe.db.get_value("Admission Cancellation", cancellation, ["student", "company"], for_update=True)
	customer = frappe.db.get_value("Student", student, "customer")
	if (doc.party_type, doc.party, doc.company) != ("Customer", customer, company):
		frappe.throw(_("{0} refunds the advance of {1}: it must be paid to their customer {2} of {3}.").format(
			doc.name, cancellation, customer, company
		))  # fmt: skip
	paid = frappe.db.sql(
		"select name from `tabPayment Entry` where oc_admission_cancellation = %s and docstatus = 1 and name != %s for update",
		(cancellation, doc.name),
	)
	if paid:
		frappe.throw(_("{0} already refunds the advance of {1}.").format(paid[0][0], cancellation), title=_("Already Refunded"))
	left = finance.unallocated_advance(customer, company, locking=True)
	if flt(doc.paid_amount) > left:
		frappe.throw(
			_("{0} refunds {1}, but only {2} of the advance is left unrefunded.").format(
				doc.name, frappe.format(doc.paid_amount, "Currency"), frappe.format(left, "Currency")
			),
			title=_("Already Refunded"),
		)


def cancel_enrollment(program_enrollment):
	# Education's on_cancel deletes the course enrollments as the session user (the officer has no delete right on them)
	for name in frappe.get_all("Course Enrollment", {"program_enrollment": program_enrollment}, pluck="name"):
		frappe.delete_doc("Course Enrollment", name, ignore_permissions=True)
	enrollment = frappe.get_doc("Program Enrollment", program_enrollment)
	enrollment.flags.ignore_permissions = True
	enrollment.flags.ignore_links = True  # the cancellation links to the enrollment it cancels
	enrollment.cancel()


def close_records(cancellation):
	"""Student leaves (lifecycle Cancelled from the cancellation date, M05-09); applicant is closed. Plain writes:
	Student.validate would put the applicant back to Admitted."""
	frappe.db.set_value(
		"Student", cancellation.student,
		{"enabled": 0, "date_of_leaving": cancellation.cancellation_date, "reason_for_leaving": cancellation.reason},
	)  # fmt: skip
	transition(
		cancellation.student, CANCELLED, cancellation.cancellation_date, cancellation.reason, cancellation,
		program_enrollment=cancellation.program_enrollment,
	)  # fmt: skip
	if cancellation.applicant:
		frappe.db.set_value("Student Applicant", cancellation.applicant, "application_status", "Rejected")
