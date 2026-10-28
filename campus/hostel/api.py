"""M10-01 Hostel service API. Other modules call these, never the controllers (D7)."""

import frappe
from frappe import _

from campus.campus_platform.api import served_companies
from campus.hostel.rules import gender_allowed

SERVICE = "Hostel"


def served_colleges(hostel):
	"""Colleges the hostel's owner serves under its Hostel Shared Service Agreements in force today."""
	return served_companies(SERVICE, frappe.db.get_value("Hostel", hostel, "company"))


def validate_allotment(hostel, student):
	"""Throw unless `student` (Student name) may be allotted in `hostel`: it is enabled, same gender, college served."""
	row = frappe.db.get_value("Hostel", hostel, ["gender", "disabled"], as_dict=True)
	if not row:
		frappe.throw(_("Hostel {0} does not exist").format(hostel))
	if row.disabled:
		frappe.throw(_("Hostel {0} is disabled").format(hostel))
	details = frappe.db.get_value("Student", student, ["gender", "oc_company"], as_dict=True)
	if not details:
		frappe.throw(_("Student {0} does not exist").format(student))
	if not gender_allowed(row.gender, details.gender):
		frappe.throw(
			_("{0} cannot take a student of gender {1}").format(hostel, details.gender or _("not set"))
		)
	if not details.oc_company:
		frappe.throw(_("Student {0} has no college set").format(student))
	if details.oc_company not in served_colleges(hostel):
		frappe.throw(_("{0} does not serve {1}").format(hostel, details.oc_company))


def fee_rate(company, academic_year, room_type):
	"""{fee category: amount} for the room type in the academic year, or {} when no rate is set."""
	name = frappe.db.exists(
		"Hostel Fee Rate", {"company": company, "academic_year": academic_year, "room_type": room_type}
	)
	if not name:
		return {}
	rows = frappe.get_all("Hostel Fee Rate Item", filters={"parent": name}, fields=["fee_category", "amount"])
	return {row.fee_category: row.amount for row in rows}


def add_beds(room, count):
	"""Add `count` active beds to `room`, numbered after the highest existing number; returns their names."""
	frappe.db.get_value("Hostel Room", room, "name", for_update=True)  # one numbering at a time
	existing = frappe.get_all("Hostel Bed", filters={"room": room}, pluck="bed_no")
	start = max((int(n) for n in existing if str(n).isdigit()), default=0)
	beds = []
	for number in range(start + 1, start + 1 + int(count)):
		beds.append(
			frappe.get_doc({"doctype": "Hostel Bed", "room": room, "bed_no": str(number)}).insert().name
		)
	return beds


def allocate_bed(student, bed, academic_year, from_date=None, fee_amount=None):
	"""Allocate `bed` to `student` for `academic_year` under a row lock and raise the Hostel Fee invoice."""
	from campus.hostel.doctype.hostel_bed_allocation.hostel_bed_allocation import (
		allocate_bed as _allocate_bed,
	)

	return _allocate_bed(
		student=student,
		bed=bed,
		academic_year=academic_year,
		from_date=from_date,
		fee_amount=fee_amount,
	)


def vacate_for_student(student, end_date=None):
	"""End all active Hostel Bed Allocations for `student` on `end_date` and free their beds."""
	from campus.hostel.doctype.hostel_bed_allocation.hostel_bed_allocation import (
		vacate_for_student as _vacate_for_student,
	)

	return _vacate_for_student(student=student, end_date=end_date)
