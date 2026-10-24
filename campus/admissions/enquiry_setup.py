"""M04-05 Enquiry CRM setup: Lead and ToDo custom fields, the web form source."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from campus.admissions.setup import MODULE, OFFICER_ROLE, field


def setup_enquiry():
	lead = [
		field("oc_enquiry_section", "Section Break", "Admission Enquiry", "company_name"),
		field("oc_program_interest", "Link", "Program of Interest", "oc_enquiry_section", options="Program",
			in_standard_filter=1),
		field("oc_source_campaign", "Data", "Source Campaign", "oc_program_interest"),
		field("oc_applicant", "Link", "Applicant", "oc_source_campaign", options="Student Applicant",
			read_only=1, no_copy=1),
		field("oc_website_url", "Data", "Website (leave empty)", "oc_applicant", hidden=1, no_copy=1,
			description="Spam guard: bots fill it, people never see it."),
	]  # fmt: skip
	todo = [
		field("oc_followup_rule", "Link", "Enquiry Follow-up Rule", "reference_name",
			options="Enquiry Follow-up Rule", read_only=1, no_copy=1, search_index=1),
	]  # fmt: skip
	create_custom_fields({"Lead": lead, "ToDo": todo}, update=True)
	grant_officer_lead_access()
	ensure_web_form_source()


def grant_officer_lead_access():
	"""Officers work the enquiries assigned to them (company User Permissions still scope them)."""
	from frappe.permissions import add_permission, update_permission_property

	if not frappe.db.exists("Custom DocPerm", {"parent": "Lead", "role": OFFICER_ROLE, "permlevel": 0}):
		add_permission("Lead", OFFICER_ROLE, 0)
		update_permission_property("Lead", OFFICER_ROLE, 0, "export", 0)  # Custom DocPerm defaults to export 1
	for right in ("write", "create", "report"):
		update_permission_property("Lead", OFFICER_ROLE, 0, right, 1)


def ensure_web_form_source():
	from campus.admissions.enquiry import WEB_FORM_SOURCE

	if not frappe.db.exists("UTM Source", WEB_FORM_SOURCE):
		frappe.get_doc({"doctype": "UTM Source", "name": WEB_FORM_SOURCE}).insert(ignore_permissions=True)
