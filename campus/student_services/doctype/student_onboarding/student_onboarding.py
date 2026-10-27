"""M05-01 Student Onboarding: one per student; completion % is mirrored on the Student form."""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime


class StudentOnboarding(Document):
	def before_insert(self):
		if not self.items and (self.student or self.template):
			details = get_onboarding_template_details(self.student, self.template)
			if details.get("template") and not self.template:
				self.template = details["template"]
			if details.get("items") and not self.items:
				for item in details["items"]:
					self.append("items", {**item, "status": "Pending"})

	def validate(self):
		if self.student and self.template:
			student_company = frappe.db.get_value("Student", self.student, "oc_company")
			template_company = frappe.db.get_value("Student Onboarding Template", self.template, "company")
			if student_company and template_company and student_company != template_company:
				frappe.throw(
					_(
						"Student Onboarding Template {0} belongs to {1}, but Student {2} belongs to {3}."
					).format(
						frappe.bold(self.template),
						frappe.bold(template_company),
						frappe.bold(self.student),
						frappe.bold(student_company),
					),
					title=_("Company Mismatch"),
				)
		for row in self.items:
			self.check_item(row)
		self.completion = completion_percent(self.items)

	def check_item(self, row):
		if row.status == "Waived" and row.mandatory:
			frappe.throw(_("Row {0}: {1} is mandatory and cannot be waived.").format(row.idx, row.title))
		if row.status == "Done":
			if row.requires_attachment and not row.attachment:
				frappe.throw(
					_("Row {0}: {1} needs an attachment before it is marked Done.").format(row.idx, row.title)
				)
			row.done_by = row.done_by or frappe.session.user
			row.done_on = row.done_on or now_datetime()
		else:
			row.done_by = row.done_on = None

	def on_update(self):
		frappe.db.set_value(
			"Student", self.student, "oc_onboarding_completion", self.completion, update_modified=False
		)

	def on_trash(self):
		frappe.db.set_value("Student", self.student, "oc_onboarding_completion", 0, update_modified=False)


def completion_percent(items):
	"""Done / (all - Waived) as a percentage; a checklist with nothing left to do is 100."""
	counted = [row for row in items if row.status != "Waived"]
	if not counted:
		return 100.0
	return flt(100 * sum(row.status == "Done" for row in counted) / len(counted), 2)


@frappe.whitelist()
def get_onboarding_template_details(student: str | None = None, template: str | None = None) -> dict:
	"""Resolves the default template and checklist items for a student or given template."""
	student_company = None
	if student:
		frappe.has_permission("Student", "read", student, throw=True)
		student_company = frappe.db.get_value("Student", student, "oc_company")

	if not template and student:
		if student_company:
			template = frappe.db.get_value(
				"Student Onboarding Template", {"company": student_company, "is_default": 1}
			)

	if not template:
		return {"template": None, "items": []}

	frappe.has_permission("Student Onboarding Template", "read", template, throw=True)

	if student and student_company:
		template_company = frappe.db.get_value("Student Onboarding Template", template, "company")
		if template_company and template_company != student_company:
			frappe.throw(
				_("Student Onboarding Template {0} belongs to {1}, but Student {2} belongs to {3}.").format(
					frappe.bold(template),
					frappe.bold(template_company),
					frappe.bold(student),
					frappe.bold(student_company),
				),
				title=_("Company Mismatch"),
			)

	items = frappe.get_all(
		"Student Onboarding Template Item",
		filters={"parent": template},
		fields=["item_code", "title", "mandatory", "requires_attachment"],
		order_by="idx",
	)
	return {"template": template, "items": items}
