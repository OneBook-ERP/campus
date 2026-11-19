"""M17-01 Hide the workspaces of disabled module packs from desk navigation.

Mechanism: the `boot_session` hook removes a pack's workspaces (pages, the area's sidebar, dock entries and
grid icons) from a user's boot data when the pack is disabled for ALL companies the user can access, or when the
pack lists roles and the user has none of them. System Manager and Administrator always see everything.
This hides navigation only; data access stays governed by DocType permissions.
"""

import frappe
from frappe.core.doctype.user_permission.user_permission import get_user_permissions
from frappe.utils.caching import request_cache

from campus.campus_platform.api import pack_enabled


def accessible_companies(user):
	restricted = [p["doc"] for p in get_user_permissions(user).get("Company", [])]
	return restricted or frappe.get_all("Institution Profile", pluck="name")


@request_cache
def hidden_workspaces(user):
	"""Names of workspaces that belong to a pack the user may not see. Asked up to three times per boot (Apps tile,
	grid App icon, boot_session), so it is computed once per request; `clear_pack_cache` forgets it."""
	if user == "Administrator" or "System Manager" in frappe.get_roles(user):
		return set()
	user_roles = set(frappe.get_roles(user))
	companies = accessible_companies(user)
	hidden = set()
	for pack in frappe.get_all("Module Pack", pluck="name"):
		workspaces = frappe.get_all("Module Pack Workspace", filters={"parent": pack}, pluck="workspace")
		if not workspaces:
			continue
		roles = set(frappe.get_all("Module Pack Role", filters={"parent": pack}, pluck="role"))
		if (roles and not roles & user_roles) or not any(pack_enabled(c, pack) for c in companies):
			hidden.update(workspaces)
	# Education workspace is replaced by Campus workspaces for pilot roles
	hidden.add("Education")
	return hidden


DISALLOWED_PILOT_NAV_DOCTYPES = {
	"Assessment Plan",
	"Assessment Result",
	"Assessment Result Tool",
}


def boot_session(bootinfo):
	hidden = hidden_workspaces(frappe.session.user)
	if hidden:
		filter_bootinfo(bootinfo, hidden)
	filter_pilot_links(bootinfo)


def filter_pilot_links(bootinfo):
	"""Pilot roles must not reach Assessment Plan / Assessment Result from workspace navigation."""
	user = frappe.session.user
	if user == "Administrator" or "System Manager" in frappe.get_roles(user):
		return

	# Strip from module sidebars
	for sidebar in (bootinfo.get("module_sidebars") or {}).values():
		if "items" in sidebar:
			sidebar["items"] = [
				i
				for i in sidebar["items"]
				if i.get("link_to") not in DISALLOWED_PILOT_NAV_DOCTYPES
				and i.get("label") not in DISALLOWED_PILOT_NAV_DOCTYPES
			]

	# Strip from desktop icons
	if "desktop_icons" in bootinfo:
		bootinfo["desktop_icons"] = [
			i for i in bootinfo["desktop_icons"] if i.get("link_to") not in DISALLOWED_PILOT_NAV_DOCTYPES
		]

	# Strip from workspace pages
	workspaces = bootinfo.get("workspaces") or {}
	for page in workspaces.get("pages") or []:
		if "links" in page and isinstance(page["links"], list):
			page["links"] = [
				lnk
				for lnk in page["links"]
				if lnk.get("link_to") not in DISALLOWED_PILOT_NAV_DOCTYPES
				and lnk.get("label") not in DISALLOWED_PILOT_NAV_DOCTYPES
			]
		if "shortcuts" in page and isinstance(page["shortcuts"], list):
			page["shortcuts"] = [
				sc
				for sc in page["shortcuts"]
				if sc.get("link_to") not in DISALLOWED_PILOT_NAV_DOCTYPES
				and sc.get("label") not in DISALLOWED_PILOT_NAV_DOCTYPES
			]


def filter_bootinfo(bootinfo, hidden):
	workspaces = bootinfo.get("workspaces") or {}
	for key in ("pages", "private_pages"):
		if key in workspaces:
			workspaces[key] = [p for p in workspaces[key] if p.get("name") not in hidden]

	dropped = filter_sidebars(bootinfo.get("module_sidebars") or {}, hidden)
	if dropped:
		forget_shells(bootinfo, dropped)

	def reachable(row):
		if row.get("link_type") == "Workspace":
			return row.get("link_to") not in hidden
		return not (row.get("link_type") == "Sidebar" and row.get("link_to") in dropped)

	for app, rows in (bootinfo.get("dock") or {}).items():
		bootinfo["dock"][app] = [r for r in rows if reachable(r)]
	for app in bootinfo.get("app_data") or []:
		app["dock"] = [r for r in app.get("dock") or [] if reachable(r)]

	if "desktop_icons" in bootinfo:  # only the retiring icon grid carries these
		bootinfo["desktop_icons"] = [i for i in bootinfo["desktop_icons"] if i.get("link_to") not in hidden]


def filter_sidebars(sidebars, hidden):
	"""Drop the sidebars of a hidden workspace's module and strip hidden workspace links from the others. Matched by
	module, not by Home: Frappe already removed Home for a user without the workspace role, but keeps the shell when
	the user can read any DocType in it. Returns the dropped shells."""
	hidden_modules = set(frappe.get_all("Workspace", {"name": ["in", list(hidden)]}, pluck="module"))
	dropped = set()
	for shell, sidebar in list(sidebars.items()):
		if sidebar.get("module") in hidden_modules or set(sidebar.get("workspaces") or ()) & hidden:
			dropped.add(shell)
			del sidebars[shell]
			continue
		sidebar["items"] = [
			i
			for i in sidebar["items"]
			if not (i.get("link_type") == "Workspace" and i.get("link_to") in hidden)
		]
	return dropped


def forget_shells(bootinfo, dropped):
	"""Routing maps must not send a route to a shell the user no longer has."""
	entity_module = bootinfo.get("entity_module") or {}
	for entity, shell in list(entity_module.items()):
		if shell in dropped:
			del entity_module[entity]
	for by_name in (bootinfo.get("canonical_shell") or {}).values():
		for name, shell in list(by_name.items()):
			if shell in dropped:
				del by_name[name]
	if bootinfo.get("home_shell") in dropped:
		bootinfo["home_shell"] = None
