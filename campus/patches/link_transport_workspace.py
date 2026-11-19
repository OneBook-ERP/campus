"""M11-01 Link the Transport Module Pack to the Transport workspace (hidden with the pack). Re-runnable."""

from campus.campus_platform.module_packs import link_pack_workspaces


def execute():
	"""Backfill the Transport workspace link onto the TRANSPORT Module Pack when empty."""
	link_pack_workspaces()
