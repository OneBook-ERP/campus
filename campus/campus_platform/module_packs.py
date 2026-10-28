"""M17-01 Seed Module Pack records (idempotent: existing packs are never overwritten)."""

import frappe

ALL_STATUSES = ["Affiliated", "Autonomous", "Deemed"]
AUTONOMOUS_ONLY = ["Autonomous", "Deemed"]

SEED_PACKS = {
	"COE": (
		"Controller of Examinations",
		ALL_STATUSES,
	),  # M06-10: CIA and the university exchange in Affiliated mode
	"GOVERNANCE": ("Academic Governance", AUTONOMOUS_ONLY),
	"HOSTEL": ("Hostel", ALL_STATUSES),
	"TRANSPORT": ("Transport", ALL_STATUSES),
	"LABS": ("Labs", ALL_STATUSES),
	"CLINICAL": ("Clinical", ALL_STATUSES),
	"PLACEMENT": ("Placement", ALL_STATUSES),
	"ALUMNI": ("Alumni", ALL_STATUSES),
	"LIBRARY": ("Library", ALL_STATUSES),
	"RESEARCH": ("Research", ALL_STATUSES),
	"IQAC": ("IQAC", ALL_STATUSES),
}

# Packs whose module has a desk area: the area's workspace goes with the pack (hidden when the pack is off).
PACK_WORKSPACES = {
	"COE": ["Examinations"],
	"GOVERNANCE": ["Academic Governance"],
	"HOSTEL": ["Hostel"],
	"TRANSPORT": ["Transport"],
	"LABS": ["Labs"],
	"PLACEMENT": ["Placement"],
	"IQAC": ["IQAC"],
	"RESEARCH": ["Research"],
}


def seed_module_packs():
	for code, (title, statuses) in SEED_PACKS.items():
		if frappe.db.exists("Module Pack", code):
			continue
		frappe.get_doc(
			{
				"doctype": "Module Pack",
				"code": code,
				"title": title,
				"allowed_statuses": [{"status": s} for s in statuses],
				"workspaces": [{"workspace": w} for w in PACK_WORKSPACES.get(code, [])],
			}
		).insert(ignore_permissions=True)
	link_pack_workspaces()


def link_pack_workspaces():
	"""One-off for packs seeded before their workspaces existed: fill an empty workspace list."""
	for code, workspaces in PACK_WORKSPACES.items():
		if not frappe.db.exists("Module Pack", code):
			continue
		pack = frappe.get_doc("Module Pack", code)
		if pack.workspaces:
			continue
		for workspace in workspaces:
			pack.append("workspaces", {"workspace": workspace})
		pack.save(ignore_permissions=True)
