"""M17-01 The OneBook Campus tile on the Apps screen."""

import frappe

from campus.campus_platform.pack_visibility import hidden_workspaces


def has_app_permission():
	"""Show the Campus tile when at least one visible workspace is available."""
	hidden = hidden_workspaces(frappe.session.user)

	workspaces = frappe.get_all(
		"Workspace",
		filters={"app": "campus", "is_hidden": 0},
		pluck="name",
	)

	return any(workspace not in hidden for workspace in workspaces)
