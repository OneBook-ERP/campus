"""M12-01 Who reads which Asset: a Lab In-charge whose only asset-reading role is Lab In-charge reads the assets located in
a laboratory of the asset's own college (the lab equipment), not vehicles, furniture or IT assets. Any user holding
another role with read on Asset (Accounts User, Asset Manager and the like) is not limited here."""

import frappe

from campus.labs.setup import LAB_ROLE


def limited_to_lab_equipment(user):
	if user == "Administrator":
		return False
	roles = set(frappe.get_roles(user))
	if LAB_ROLE not in roles:
		return False
	asset_readers = {p.role for p in frappe.get_meta("Asset").permissions if p.read and not p.permlevel}
	return not (roles & asset_readers) - {LAB_ROLE}


def equipment_names_query():
	"""SQL selecting the names of the Assets located in a laboratory of their own company."""
	return (
		"select a.name from `tabAsset` a join `tabLaboratory` l "
		"on l.asset_location = a.location and l.company = a.company"
	)


def asset_query(user=None):
	"""permission_query_conditions for Asset."""
	user = user or frappe.session.user
	if limited_to_lab_equipment(user):
		return f"`tabAsset`.name in ({equipment_names_query()})"
	return ""


def asset_has_permission(doc, ptype=None, user=None):
	"""Deny-only: a limited Lab In-charge opens only lab equipment."""
	user = user or frappe.session.user
	if not limited_to_lab_equipment(user):
		return True
	return bool(
		frappe.db.sql(
			f"select 1 from ({equipment_names_query()}) e where e.name = %s", doc.name
		)
	)


def maintenance_query(user=None):
	"""permission_query_conditions for Asset Maintenance: the lab equipment's schedules."""
	user = user or frappe.session.user
	if limited_to_lab_equipment(user):
		return f"`tabAsset Maintenance`.asset_name in ({equipment_names_query()})"
	return ""


def maintenance_has_permission(doc, ptype=None, user=None):
	user = user or frappe.session.user
	if not limited_to_lab_equipment(user):
		return True
	return bool(
		frappe.db.sql(
			f"select 1 from ({equipment_names_query()}) e where e.name = %s", doc.asset_name
		)
	)
