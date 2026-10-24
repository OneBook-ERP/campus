"""M09-04 Student Fee Collection (Education's report) is not company-scoped, so it stays with Academics User: the Custom
Role that opened it to Accounts User and Manager is removed when it holds exactly those roles on top of the report's
own. Accounts staff use the Student Fee Ledger. Runs once."""

import frappe

REPORT = "Student Fee Collection"
ACCOUNTS_ROLES = {"Accounts User", "Accounts Manager"}


def execute():
	own = set(frappe.get_all("Has Role", {"parent": REPORT, "parenttype": "Report"}, pluck="role"))
	for name in frappe.get_all("Custom Role", {"report": REPORT}, pluck="name"):
		roles = set(frappe.get_all("Has Role", {"parent": name, "parenttype": "Custom Role"}, pluck="role"))
		if roles == own | ACCOUNTS_ROLES:
			frappe.delete_doc("Custom Role", name, ignore_permissions=True, force=True)
