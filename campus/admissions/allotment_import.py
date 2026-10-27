"""M04-02 Allotment Import: create or update Student Applicants from a counselling authority's allotment file.

Flow: an Allotment Import (company, counselling authority, academic year, CSV/XLSX file, branch-code to Program
mapping) is started with `start_import`; the file is parsed with the authority's Column Mapping and one bulk
run item per file row is queued (chunk=1: every row is its own transaction).

Identity: an applicant is keyed by (authority, allotment no): `oc_allotment_key` = "<authority>:<allotment no>",
a unique column, so a re-import or a concurrent import can never create a second applicant. A re-import
updates the mapped fields that changed (rank, cutoff...), reports an identical row as Unchanged and never
touches an applicant that is already Admitted. Blank cells never overwrite stored values.

Outcome of every file row is kept as an Allotment Import Row (Created / Updated / Unchanged / Error with the
message); the Allotment Import carries the counts.

Aadhaar: columns mapped to the Aadhaar fields are never imported (Counselling Authority rejects such a
mapping and the parser drops them regardless), so no Aadhaar number can reach an item, a log or an error.
Mapped targets that are system-managed (company, key, status, tables) are likewise ignored.

Concurrency (MariaDB REPEATABLE READ): the handler's first statement is a locking read of the applicant by
key, and every fact it decides on (the stored values it compares, the status) comes from that read. Two
imports of the same key serialise there; the unique key backs it. Rows are processed in key order so two
imports lock in the same order; a lock wait or deadlock loses only that row, recorded as an Error row.
"""

import csv
import hashlib
import io
import json
import os
import re

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, get_datetime, getdate, strip_html_tags
from frappe.utils.file_manager import get_file

from campus.admissions import seat_matrix
from campus.admissions.aadhaar import AADHAAR_FIELD, MASKED_FIELD
from campus.campus_platform.bulk import run_bulk

HANDLER = "campus.admissions.allotment_import.process_row"
KEY = "campus.admissions.allotment_import.row_key"
RUN_TYPE = "Allotment Import"
SAVEPOINT = "allotment_row"
KEY_LENGTH = 140
ALLOTMENT_FIELD = "oc_allotment_no"
PROGRAM_FIELD = "program"
AADHAAR_FIELDS = (AADHAAR_FIELD, MASKED_FIELD)
SYSTEM_FIELDS = (
	"name", "docstatus", "application_status", "oc_company", "oc_counselling_authority", "oc_allotment_key",
	"oc_documents", "oc_qualifying_marks", "academic_year", "oc_eligibility_status", "oc_eligibility_reasons",
)  # fmt: skip
COUNTER = {
	"Created": "created_rows",
	"Updated": "updated_rows",
	"Unchanged": "unchanged_rows",
	"Error": "error_rows",
}


@frappe.whitelist()
def start_import(name):
	"""Parse the attached file and queue the bulk run that applies its rows; returns the Bulk Run name."""
	frappe.has_permission("Allotment Import", "write", name, throw=True)
	status = frappe.db.get_value(
		"Allotment Import", name, "status", for_update=True
	)  # a double click waits here
	if status != "Draft":
		frappe.throw(_("This import has already been started."))
	doc = frappe.get_doc("Allotment Import", name)
	frappe.has_permission("Student Applicant", "create", throw=True)
	mapping = column_mapping(doc.counselling_authority)
	content = read_file(doc.import_file)
	content = content.encode() if isinstance(content, str) else content
	make_private(doc.import_file)
	rows = parse_file(doc.import_file, content)
	items = build_items(doc, mapping, rows)
	doc.db_set(
		{"total_rows": len(items), "status": "Processing", "file_sha256": hashlib.sha256(content).hexdigest()}
	)
	run = run_bulk(RUN_TYPE, items, HANDLER, chunk=1, key=KEY, parameters={"import": name})
	doc.db_set("bulk_run", run)
	return run


def column_mapping(authority):
	"""{source column (lower case): target field} of the authority; both identity targets must be mapped."""
	rows = frappe.get_all(
		"Import Column Mapping", {"parent": authority}, ["source_column", "target_field"], limit=0
	)
	mapping = {
		cstr(r.source_column).strip().lower(): r.target_field
		for r in rows
		if r.target_field not in AADHAAR_FIELDS
	}
	for target in (ALLOTMENT_FIELD, PROGRAM_FIELD):
		if target not in mapping.values():
			frappe.throw(_("{0} has no column mapped to {1}.").format(authority, target))
	return mapping


def read_file(file_url):
	"""The attached file's content, after checking the user may read that File."""
	file_name = frappe.db.get_value("File", {"file_url": file_url}, "name")
	if not file_name:
		frappe.throw(_("The allotment file {0} was not found.").format(file_url))
	frappe.get_doc("File", file_name).check_permission("read")
	return get_file(file_url)[1]


def make_private(file_url, doctype="Allotment Import", fieldname="import_file"):
	"""Allotment (and applicant upload) files carry personal data: the attached File is made private; returns its (new) url.

	When a private file of the same name already exists, the content is stored as a new, uniquely named
	private File and the public one is deleted, so a name clash never blocks the import.
	"""
	name = frappe.db.get_value("File", {"file_url": file_url}, "name")
	file = frappe.get_doc("File", name)
	if file.is_private:
		return file.file_url
	if os.path.exists(frappe.get_site_path("private", "files", file.file_url.split("/")[-1])):
		stem, ext = os.path.splitext(file.file_name)
		private = frappe.get_doc(
			{
				"doctype": "File", "file_name": f"{stem}-{frappe.generate_hash(length=8)}{ext}", "content": file.get_content(),
				"is_private": 1, "attached_to_doctype": file.attached_to_doctype, "attached_to_name": file.attached_to_name,
			}
		).insert(ignore_permissions=True)  # fmt: skip
		public_path = frappe.get_site_path("public", "files", file.file_url.split("/")[-1])
		frappe.delete_doc("File", file.name, ignore_permissions=True, force=True)
		if os.path.exists(public_path) and not frappe.db.exists("File", {"file_url": file.file_url}):
			os.remove(
				public_path
			)  # content-hash dedupe can skip the disk deletion: never leave the data readable
		new_url = private.file_url
	else:
		file.is_private = 1
		file.save(ignore_permissions=True)
		new_url = file.file_url
	frappe.db.set_value(doctype, {fieldname: file_url}, fieldname, new_url, update_modified=False)
	return new_url


def discard_file(import_name, doctype="Allotment Import", fieldname="import_file"):
	"""Delete the uploaded file once the import is complete; its sha256 stays on the import as the audit trail."""
	file_url = frappe.db.get_value(doctype, import_name, fieldname)
	if not file_url:
		return
	for name in frappe.get_all(
		"File",
		{"file_url": file_url, "attached_to_doctype": doctype, "attached_to_name": import_name},
		pluck="name",
	):
		frappe.delete_doc("File", name, ignore_permissions=True, force=True)
	frappe.db.set_value(doctype, import_name, fieldname, None, update_modified=False)


def parse_file(file_url, content=None):
	"""Rows of the CSV or XLSX attachment as lists of text, header first."""
	content = content if content is not None else read_file(file_url)
	if cstr(file_url).lower().endswith(".xlsx"):
		from frappe.utils.xlsxutils import read_xlsx_file_from_attached_file

		return [[cstr(c).strip() for c in row] for row in read_xlsx_file_from_attached_file(fcontent=content)]
	text = content.decode("utf-8-sig") if isinstance(content, bytes) else content
	return [[cstr(c).strip() for c in row] for row in csv.reader(io.StringIO(text))]


def build_items(doc, mapping, rows):
	"""One bulk item per data row: {import, row_no, company, authority, academic_year, values{target: text}}."""
	if not rows:
		frappe.throw(_("The allotment file is empty."))
	header = [h.lower() for h in rows[0]]
	missing = [c for c in mapping if c not in header]
	if missing:
		frappe.throw(_("The allotment file is missing the columns: {0}").format(", ".join(missing)))
	meta = frappe.get_meta("Student Applicant")
	for target in set(mapping.values()) - {PROGRAM_FIELD}:
		if not meta.has_field(target):
			frappe.throw(_("Student Applicant has no field {0}.").format(target))
	programs = {m.source_code.strip().lower(): m.program for m in doc.program_mapping}
	items = []
	for row_no, cells in enumerate(rows[1:], start=2):
		if not any(cells):
			continue
		values = {}
		for column, target in mapping.items():
			cell = cells[header.index(column)] if header.index(column) < len(cells) else ""
			if cell and target not in SYSTEM_FIELDS + AADHAAR_FIELDS:
				values[target] = cell
		code = values.pop(PROGRAM_FIELD, "")
		items.append(
			{
				"import": doc.name, "row_no": row_no, "company": doc.company, "authority": doc.counselling_authority,
				"academic_year": doc.academic_year, "allotment_no": values.pop(ALLOTMENT_FIELD, ""),
				"program": programs.get(code.lower()) if code else None, "program_code": code, "values": values,
			}
		)  # fmt: skip
	items.sort(key=lambda i: (applicant_key(i["authority"], i["allotment_no"]), i["row_no"]))
	return items


def applicant_key(authority, allotment_no):
	return f"{authority}:{allotment_no}"


def row_key(item):
	return f"{item['import']}::{item['row_no']}"


def set_allotment_key(doc, method=None):
	"""Student Applicant validate hook: keep `oc_allotment_key` in step with authority and allotment no."""
	if doc.oc_counselling_authority and doc.oc_allotment_no:
		doc.oc_allotment_key = applicant_key(doc.oc_counselling_authority, doc.oc_allotment_no)
	else:
		doc.oc_allotment_key = None


# Row handler
def process_row(item, run):
	"""Bulk handler: create, update or leave the applicant of one file row and record the outcome. DB-only, no commit.

	A failure of the row is rolled back to the row savepoint and recorded as an Error row. When the savepoint
	itself is gone (a deadlock rolls the whole transaction back) the error propagates: the bulk runner records
	the item as failed, and `reconcile` later turns the runner's failed items into Error rows and counts, so the
	import still completes. The handler never rolls back or commits the whole transaction itself.
	"""
	frappe.db.savepoint(SAVEPOINT)
	try:
		status, applicant, message = apply_row(item)
	except frappe.QueryDeadlockError:
		raise  # every savepoint is gone: the runner rolls back fully and records the item as failed
	except Exception as e:  # noqa: BLE001 - every failure of a row is recorded, never stalls the import
		frappe.db.rollback(save_point=SAVEPOINT)  # raises when the savepoint is dead: propagate to the runner
		status, applicant, message = "Error", None, cstr(e)
	try:
		record(item, status, applicant, message)
	except Exception:  # noqa: BLE001
		frappe.db.rollback(save_point=SAVEPOINT)
		record(item, "Error", None, _("The row could not be processed."))


def mask_numbers(message):
	"""Long digit runs (ids, phone and Aadhaar-like numbers) never reach a stored message."""
	return re.sub(r"\d{10,}", lambda m: "X" * len(m.group()), strip_html_tags(cstr(message)))


def apply_row(item):
	"""(status, applicant, message) for a row; raises on a row that cannot be applied."""
	problem = invalid_reason(item)
	if problem:
		return "Error", None, problem
	key = applicant_key(item["authority"], item["allotment_no"])
	values = coerce_values(item["values"])
	wanted = {**values, "program": item["program"], "academic_year": item["academic_year"]}
	existing = frappe.db.get_value(
		"Student Applicant",
		{"oc_allotment_key": key},
		["name", "oc_company", "application_status", *wanted],
		as_dict=True,
		for_update=True,
	)
	if not existing:
		return create_applicant(item, key, wanted)
	if existing.oc_company != item["company"]:
		return "Error", None, _("This allotment number is already used by another college.")
	changed = {f: v for f, v in wanted.items() if coerce(f, existing[f]) != v}
	if not changed:
		return "Unchanged", existing.name, ""
	if existing.application_status == "Admitted":
		return "Error", existing.name, _("The applicant is already admitted and was not changed.")
	doc = frappe.get_doc("Student Applicant", existing.name)
	doc.update(changed)
	doc.save(ignore_permissions=True)
	return "Updated", doc.name, _("Changed: {0}").format(", ".join(sorted(changed)))


def create_applicant(item, key, wanted):
	doc = frappe.get_doc(
		{
			"doctype": "Student Applicant", "application_status": "Applied", "oc_company": item["company"],
			"oc_counselling_authority": item["authority"], "oc_allotment_no": item["allotment_no"], "oc_allotment_key": key, **wanted,
		}
	)  # fmt: skip
	doc.insert(ignore_permissions=True)
	return "Created", doc.name, ""


def invalid_reason(item):
	if not item["allotment_no"]:
		return _("The allotment number is blank.")
	if len(applicant_key(item["authority"], item["allotment_no"])) > KEY_LENGTH:
		return _("The allotment number is too long.")
	if not item["program"]:
		return _("Branch code {0} is not in the program mapping.").format(
			item["program_code"] or _("(blank)")
		)


def coerce_values(values):
	return {field: coerce(field, value) for field, value in values.items()}


def coerce(fieldname, value):
	"""The value as the Student Applicant field stores it, so file text and stored values compare equal."""
	fieldtype = frappe.get_meta("Student Applicant").get_field(fieldname).fieldtype
	if value in (None, ""):
		return None
	if fieldtype in ("Int", "Check"):
		return cint(value)
	if fieldtype in ("Float", "Currency", "Percent"):
		return flt(value)
	if fieldtype == "Date":
		return getdate(value)
	if fieldtype == "Datetime":
		return get_datetime(value)
	if fieldname in ("student_mobile_number", "mobile_number", "mobile_no") or fieldtype == "Phone":
		from campus.campus_platform.sanitize import clean_mobile_number

		return clean_mobile_number(value)
	return cstr(value).strip()


def record(item, status, applicant, message):
	frappe.get_doc(
		{
			"doctype": "Allotment Import Row", "allotment_import": item["import"], "row_no": item["row_no"],
			"allotment_no": item["allotment_no"][:140], "status": status, "applicant": applicant, "message": mask_numbers(message)[:300],
		}
	).insert(ignore_permissions=True)  # fmt: skip
	bump(item["import"], status)


def bump(import_name, status):
	"""Count one more row; once every file row has an outcome the import is complete and its matrices refresh."""
	column = COUNTER[status]
	frappe.db.sql(
		f"update `tabAllotment Import` set `{column}` = `{column}` + 1 where name = %s", import_name
	)
	counts = frappe.db.get_value(
		"Allotment Import", import_name, ["total_rows", *COUNTER.values()], as_dict=True
	)
	if sum(counts[c] or 0 for c in COUNTER.values()) < counts.total_rows:
		return
	frappe.db.set_value(
		"Allotment Import",
		import_name,
		"status",
		"Completed with errors" if counts.error_rows else "Completed",
	)
	refresh_matrices(import_name)
	discard_file(import_name)


def refresh_matrices(import_name):
	company, year = frappe.db.get_value("Allotment Import", import_name, ["company", "academic_year"])
	for program in frappe.get_all(
		"Allotment Program Mapping", {"parent": import_name}, pluck="program", distinct=True
	):
		seat_matrix.refresh_allotted(company, program, year)


def reconcile(import_name):
	"""Make a finished import complete when the runner failed or never reached some items (they have no row).

	Each failed Bulk Run item becomes an Error row; for a run that ended Failed, the items it never processed
	become "not processed" Error rows. Every row is counted, so the import completes like a normal finish.
	A no-op unless the import is Processing and its run has finished (Completed, with errors, or Failed).
	"""
	doc = frappe.db.get_value("Allotment Import", import_name, ["status", "bulk_run"], as_dict=True)
	if not doc or doc.status != "Processing" or not doc.bulk_run:
		return
	status = frappe.db.get_value("Bulk Run", doc.bulk_run, "status")
	if status not in ("Completed", "Completed with errors", "Failed"):
		return
	recorded = set(
		frappe.get_all("Allotment Import Row", {"allotment_import": import_name}, pluck="row_no", limit=0)
	)
	for failed in frappe.get_all(
		"Bulk Run Row Error", {"parent": doc.bulk_run}, ["item_key", "error"], limit=0
	):
		record_missing(import_name, failed.item_key, failed.error, recorded)
	if status == "Failed":
		keys = frappe.get_all("Bulk Run Item Key", {"parent": doc.bulk_run}, pluck="item_key", limit=0)
		for item_key in unprocessed_keys(doc.bulk_run, set(keys)):
			record_missing(import_name, item_key, _("Not processed: the import run failed."), recorded)


def record_missing(import_name, item_key, message, recorded):
	row_no = cint(item_key.rpartition("::")[2])
	if row_no not in recorded:
		recorded.add(row_no)
		record({"import": import_name, "row_no": row_no, "allotment_no": ""}, "Error", None, message)


def unprocessed_keys(bulk_run, processed):
	items = json.loads(frappe.db.get_value("Bulk Run", bulk_run, "items") or "[]")
	return [k for k in (row_key(item) for item in items) if k not in processed]


def reconcile_finished_imports():
	"""Scheduler (hourly): complete imports whose bulk run has finished; one bad import never stops the others."""
	for name in frappe.get_all("Allotment Import", {"status": "Processing"}, pluck="name"):
		try:
			reconcile(name)
			frappe.db.commit()
		except Exception:  # noqa: BLE001
			frappe.db.rollback()
			frappe.log_error(
				title=f"Allotment import reconcile failed: {name}",
				message=frappe.get_traceback(with_context=False),
			)
