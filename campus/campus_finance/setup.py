"""M09 Campus Finance setup: custom fields on Fee Category, Fee Structure, Sales Invoice and Payment Entry, and the seed fee heads."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from campus.campus_finance.fee_heads import FEE_HEADS, seed_fee_categories
from campus.campus_finance.no_dues import seed_office_roles

MODULE = "Campus Finance"


def field(fieldname, fieldtype, label, insert_after, **kwargs):
	return {
		"fieldname": fieldname, "fieldtype": fieldtype, "label": label, "insert_after": insert_after,
		"module": MODULE, **kwargs,
	}  # fmt: skip


CONCESSION_POLICY = "Fee Concession Approval"
REFUND_POLICY = "Fee Refund Approval"
SCHOLARSHIP_POLICY = "Scholarship Sanction Approval"


def setup_finance():
	setup_finance_fields()
	seed_fee_categories()
	seed_office_roles()
	seed_concession_policy()
	seed_refund_policy()
	seed_scholarship_policy()
	grant_guardian_invoice_read()
	narrow_student_invoice_rights()


# ERPNext's Payment Request validation reads its Sales Invoice as the session user (ERPNext 58de4a5c36, 16.52), so a
# guardian paying a ward's fee through the portal needs read on Sales Invoice. Read alone (no report, export, print,
# email or share; the role has no desk access): the PORTAL_SCOPES hooks in campus.examinations.access narrow it to the
# invoices of the guardian's wards, in lists, counts and by name. Custom DocPerm: ERPNext's JSON is never edited.
GUARDIAN_ROLE = "Guardian"
GUARDIAN_INVOICE_FLAGS = ("read", "write", "create", "report", "export", "print", "email", "share", "select")


def grant_guardian_invoice_read():
	"""Idempotent: add the Guardian row once, with read only; a row the college already keeps is left as it is."""
	from frappe.core.doctype.custom_docperm.custom_docperm import update_custom_docperm
	from frappe.permissions import add_permission

	if not frappe.db.exists("Role", GUARDIAN_ROLE) or frappe.db.exists(
		"Custom DocPerm", {"parent": "Sales Invoice", "role": GUARDIAN_ROLE, "permlevel": 0}
	):
		return
	name = add_permission("Sales Invoice", GUARDIAN_ROLE, 0)  # copies the standard DocPerms first, so none is lost
	update_custom_docperm(name, {flag: int(flag == "read") for flag in GUARDIAN_INVOICE_FLAGS})


# Education gives its Student role read, write and print (and Custom DocPerm's export) on Sales Invoice. The student's
# fee views (/campus-portal/fees, the online payment, an invoice's print for the receipt) need read and print; writing or
# exporting an invoice is never needed. Narrowed once per site: a right the college changes afterwards stays as it is.
STUDENT_ROLE = "Student"
STUDENT_INVOICE_KEY = "campus_student_invoice_narrowed"


def narrow_student_invoice_rights():
	if frappe.db.get_global(STUDENT_INVOICE_KEY):
		return
	rule = frappe.db.get_value("Custom DocPerm", {"parent": "Sales Invoice", "role": STUDENT_ROLE, "permlevel": 0, "if_owner": 0}, "name")
	if not rule:  # Education not set up for students here: nothing to narrow, nothing recorded
		return
	frappe.db.set_value("Custom DocPerm", rule, {"write": 0, "export": 0, "email": 0, "share": 0})
	frappe.clear_cache(doctype="Sales Invoice")
	frappe.db.set_global(STUDENT_INVOICE_KEY, 1)


def seed_concession_policy():
	"""A single Principal level for Fee Concessions, unless the college has its own policy for them."""
	if frappe.db.exists("Approval Policy", {"document_type": "Fee Concession"}) or not frappe.db.exists("Position", "PRINCIPAL"):
		return
	frappe.get_doc(
		{
			"doctype": "Approval Policy", "policy_name": CONCESSION_POLICY, "document_type": "Fee Concession",
			"levels": [{"approver_type": "Position", "approver": "PRINCIPAL", "scope_field": "company", "title": "Principal", "sla_hours": 72}],
		}
	).insert(ignore_permissions=True)


def seed_scholarship_policy():
	"""A single Principal level for sanctioning Scholarship Applications (on the mandatory approval list), unless the
	college has its own policy for them; upgraded sites thus keep sanctioning with separation of duties."""
	if frappe.db.exists("Approval Policy", {"document_type": "Scholarship Application"}) or not frappe.db.exists("Position", "PRINCIPAL"):
		return
	frappe.get_doc(
		{
			"doctype": "Approval Policy", "policy_name": SCHOLARSHIP_POLICY, "document_type": "Scholarship Application",
			"levels": [{"approver_type": "Position", "approver": "PRINCIPAL", "scope_field": "company", "title": "Principal", "sla_hours": 72}],
		}
	).insert(ignore_permissions=True)


def seed_refund_policy():
	"""A single Accounts Manager level for Fee Refunds, unless the institution has its own policy for them."""
	if frappe.db.exists("Approval Policy", {"document_type": "Fee Refund"}):
		return
	frappe.get_doc(
		{
			"doctype": "Approval Policy", "policy_name": REFUND_POLICY, "document_type": "Fee Refund",
			"levels": [{"approver_type": "Role", "approver": "Accounts Manager", "limit_to_company": 1, "title": "Accounts", "sla_hours": 72}],
		}
	).insert(ignore_permissions=True)


def setup_finance_fields():
	"""Create/refresh the finance custom fields. Idempotent."""
	head_types = "\n".join(FEE_HEADS) + "\nOther"
	create_custom_fields(
		{
			"Fee Category": [
				field("oc_fee_head_type", "Select", "Fee Head Type", "category_name", options=head_types,
					in_list_view=1, in_standard_filter=1),
				field("oc_refundable", "Check", "Refundable", "oc_fee_head_type"),
				field("oc_gst_applicable", "Check", "GST Applicable", "oc_refundable"),
				field("oc_concession_allowed", "Check", "Concession Allowed", "oc_gst_applicable",
					description="Fee Concessions may discount this category."),
			],
			"Fee Structure": [
				field("oc_admission_quota", "Link", "Admission Quota", "student_category", options="Admission Quota",
					in_standard_filter=1),
			],
			"Sales Invoice": [
				field("oc_challan_no", "Data", "Challan No", "fee_schedule", unique=1, read_only=1, no_copy=1,
					search_index=1, description="Bank challan number; used to match bank collections."),
			],
			"Sales Invoice Item": [
				field("oc_fee_concession", "Link", "Fee Concession", "description", options="Fee Concession", read_only=1,
					no_copy=1, description="Set on the discount line a Fee Concession adds."),
			],
			"Company": [
				field("oc_damages_income_account", "Link", "Damages Income Account", "default_receivable_account", options="Account",
					description="Income account that damages kept from a caution deposit are moved to."),
			],
			"Payment Entry": [
				field("oc_student", "Link", "Student", "party_name", options="Student", read_only=1, no_copy=1,
					search_index=1),
				field("oc_receipt_no", "Data", "Fee Receipt No", "oc_student", unique=1, read_only=1, no_copy=1),
			],
		},
		update=True,
	)
