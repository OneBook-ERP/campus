"""M04-09 The Admission Officer's Custom DocPerm rows were created with Custom DocPerm's default export 1, and the Data
Import rule carried write and create. Clear them on rows still holding exactly the rights campus granted; a row the
college edited stays as it is. Runs once."""

import frappe

from campus.admissions.legacy_import import FIRST_CUT_RIGHTS, IMPORT_CHECK_RIGHTS, IMPORT_CHECK_ROLE
from campus.admissions.setup import OFFICER_READ, OFFICER_RIGHTS, OFFICER_ROLE

OTHER_RIGHTS = ("submit", "cancel", "amend", "delete", "import", "share", "print", "email")
LEAD_RIGHTS = ("write", "create", "report")


def execute():
	# Customer: the rule that was read-only gets the write and create Education's Student hook needs (see OFFICER_RIGHTS)
	narrow("Customer", OFFICER_ROLE, ("read",), {"write": 1, "create": 1, "export": 0})
	granted = {doctype: ("read",) for doctype in OFFICER_READ}
	granted |= {doctype: ("read", *rights) for doctype, rights in OFFICER_RIGHTS.items()}
	granted["Lead"] = ("read", *LEAD_RIGHTS)
	for doctype, rights in granted.items():
		narrow(doctype, OFFICER_ROLE, rights, {"export": 0})
	narrow("Data Import", IMPORT_CHECK_ROLE, tuple(FIRST_CUT_RIGHTS), {"write": 0, "create": 0, "export": 0})


def narrow(doctype, role, rights, change):
	"""Apply change to the role's plain row when its rights are exactly `rights` (export aside)."""
	rows = frappe.get_all(
		"Custom DocPerm", {"parent": doctype, "role": role, "permlevel": 0, "if_owner": 0}, ["name", "read", "write", "create", "report", *OTHER_RIGHTS]
	)
	for row in rows:
		held = {right for right in ("read", "write", "create", "report", *OTHER_RIGHTS) if row[right]}
		if held == set(rights) or (doctype == "Data Import" and held == set(IMPORT_CHECK_RIGHTS)):
			frappe.db.set_value("Custom DocPerm", row.name, change)
			frappe.clear_cache(doctype=doctype)
