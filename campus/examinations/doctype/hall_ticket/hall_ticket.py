"""M06-02 Hall Ticket: one student's admission to the exams of one Exam Session, or the record that it is withheld.

Made and refreshed only by the bulk generation (campus.examinations.hall_tickets). An Issued ticket lists the
eligible courses with their dates and carries a verifiable QR; a Withheld one lists why and has no valid QR.
"""

import frappe
from frappe import _
from frappe.model.document import Document


def ticket_name(exam_session, student):
	return f"HT-{exam_session}-{student}"


class HallTicket(Document):
	def autoname(self):
		self.name = ticket_name(self.exam_session, self.student)

	def before_validate(self):
		if self.exam_registration and (not self.exam_session or not self.student or not self.status):
			from campus.examinations.hall_tickets import ticket_values

			reg = frappe.get_doc("Exam Registration", self.exam_registration)
			values = ticket_values(reg)
			for field, val in values.items():
				if field == "courses":
					if not self.courses:
						for row in val:
							self.append("courses", row)
				elif not self.get(field):
					self.set(field, val)

	def after_insert(self):
		if not getattr(frappe.flags, "in_hall_ticket_generation", False):
			from campus.examinations.hall_tickets import refresh_qr

			refresh_qr(self, changed=True)

	def verification_details(self):
		"""[(label, value)] for the public verification page: never the student id."""
		confirmed = frappe.db.get_value("Exam Registration", self.exam_registration, "confirmed")
		return [
			(_("Exam session"), self.exam_session),
			(_("Academic term"), self.academic_term),
			(_("Registration"), _("Confirmed") if confirmed else _("Not confirmed")),
			(_("Hall ticket"), _(self.status)),
		]
