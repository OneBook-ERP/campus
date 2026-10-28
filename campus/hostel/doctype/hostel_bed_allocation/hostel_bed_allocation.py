"""M10-02 Hostel Bed Allocation: row-locked bed allocation, trust fee invoice, and side-exit vacating."""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, flt, getdate, nowdate, today

from campus.campus_finance.fee_heads import HOSTEL
from campus.campus_finance.invoices import DUE_DAYS, check_fee_invoice, raise_fee_invoice
from campus.hostel.api import fee_rate, validate_allotment
from campus.student_services import lifecycle_rules as rules
from campus.student_services.onboarding import mark_item_done

ACTIVE, VACATED = "Active", "Vacated"
BED_AVAILABLE, BED_OCCUPIED = "Available", "Occupied"
SIDE_EXIT_STATUSES = (rules.ON_BREAK, rules.WITHDRAWN, rules.DISCONTINUED, rules.CANCELLED)


class HostelBedAllocation(Document):
	@property
	def end_date(self):
		return self.to_date

	@end_date.setter
	def end_date(self, value):
		self.to_date = value

	def validate(self):
		self.status = self.status or ACTIVE
		self.from_date = self.from_date or today()
		if self.to_date and getdate(self.to_date) < getdate(self.from_date):
			frappe.throw(_("To Date cannot be before From Date"))

		bed_row = lock_bed(self.bed)
		self.room = bed_row.room
		self.hostel = bed_row.hostel
		self.company = bed_row.company

		if self.status == ACTIVE:
			self._validate_active_allocation(bed_row)
		elif self.status == VACATED and not self.to_date:
			self.to_date = today()

	def _validate_active_allocation(self, bed_row):
		if not bed_row.is_active:
			frappe.throw(_("Hostel Bed {0} is not active").format(self.bed))
		room = frappe.db.get_value(
			"Hostel Room", bed_row.room, ["status", "room_type"], as_dict=True
		)
		if not room or room.status != "Available":
			frappe.throw(
				_("Hostel Room {0} is {1}").format(bed_row.room, room.status if room else _("missing"))
			)

		validate_allotment(bed_row.hostel, self.student)
		student_status = frappe.db.get_value("Student", self.student, "oc_academic_status")
		if student_status in SIDE_EXIT_STATUSES:
			frappe.throw(
				_("Student {0} is {1} and cannot be allocated a hostel bed").format(
					self.student, student_status
				)
			)

		check_bed_free(bed_row, self.student, exclude_name=self.name)
		check_student_unallocated(self.student, exclude_name=self.name)
		if self.is_new() and not getattr(self.flags, "skip_fee_invoice", False):
			positive_rates(self.company, self.academic_year, room.room_type, getattr(self, "_fee_amount", None))

	def after_insert(self):
		if self.status != ACTIVE:
			return
		occupy_bed(self.bed, self.student)
		frappe.db.set_value("Student", self.student, "oc_hostel_resident", 1, update_modified=False)
		mark_item_done(self.student, "HOSTEL")
		if not self.sales_invoice and not getattr(self.flags, "skip_fee_invoice", False):
			invoice = raise_hostel_invoice(self)
			self.db_set("sales_invoice", invoice.name, update_modified=False)

	def on_update(self):
		if self.status == ACTIVE:
			occupy_bed(self.bed, self.student)
			refresh_student_resident_flag(self.student)
		elif self.status == VACATED:
			release_bed_if_vacated(self.bed, self.student, exclude_name=self.name)
			refresh_student_resident_flag(self.student)

	def on_trash(self):
		release_bed_if_vacated(self.bed, self.student, exclude_name=self.name)
		refresh_student_resident_flag(self.student)


@frappe.whitelist(methods=["POST"])
def allocate_bed(student, bed, academic_year, from_date=None, fee_amount=None):
	"""Allocate `bed` to `student` for `academic_year` under a row lock and raise the Hostel Fee invoice."""
	doc = frappe.get_doc(
		{
			"doctype": "Hostel Bed Allocation",
			"student": student,
			"bed": bed,
			"academic_year": academic_year,
			"from_date": from_date or today(),
			"status": ACTIVE,
		}
	)
	if fee_amount is not None:
		doc._fee_amount = fee_amount
	doc.insert()
	return doc


def vacate_for_student(student, end_date=None):
	"""End all active Hostel Bed Allocations for `student` on `end_date` and free their beds."""
	if not frappe.db.table_exists("Hostel Bed Allocation"):
		return []
	effective = getdate(end_date or today())
	alloc = frappe.qb.DocType("Hostel Bed Allocation")
	rows = (
		frappe.qb.from_(alloc)
		.select(alloc.name, alloc.bed, alloc.from_date)
		.where((alloc.student == student) & (alloc.status == ACTIVE))
		.for_update()
		.run(as_dict=True)
	)
	for row in rows:
		lock_bed(row.bed)
		vacated_on = max(effective, getdate(row.from_date)) if row.from_date else effective
		frappe.db.set_value(
			"Hostel Bed Allocation",
			row.name,
			{"status": VACATED, "to_date": vacated_on},
			update_modified=False,
		)
		release_bed_if_vacated(row.bed, student, exclude_name=row.name)
	refresh_student_resident_flag(student)
	return [row.name for row in rows]


def lock_bed(bed):
	"""Lock the Hostel Bed row for the current transaction and return its fields."""
	if not bed:
		frappe.throw(_("Hostel Bed is required"))
	row = frappe.db.get_value(
		"Hostel Bed",
		bed,
		["name", "room", "bed_no", "is_active", "hostel", "company", "status", "occupied_by"],
		as_dict=True,
		for_update=True,
	)
	if not row:
		frappe.throw(_("Hostel Bed {0} does not exist").format(bed))
	return row


def check_bed_free(bed_row, student, exclude_name=None):
	"""Verify under row lock that `bed_row` has no other active allocation."""
	alloc = frappe.qb.DocType("Hostel Bed Allocation")
	active = (
		frappe.qb.from_(alloc)
		.select(alloc.name, alloc.student)
		.where((alloc.bed == bed_row.name) & (alloc.status == ACTIVE) & (alloc.name != (exclude_name or "")))
		.for_update()
		.run(as_dict=True)
	)
	if active or (
		bed_row.status == BED_OCCUPIED and bed_row.occupied_by and bed_row.occupied_by != student
	):
		frappe.throw(_("Hostel Bed {0} is already occupied").format(bed_row.name))


def check_student_unallocated(student, exclude_name=None):
	"""Verify under row lock that `student` does not already hold an active bed or transport allocation."""
	alloc = frappe.qb.DocType("Hostel Bed Allocation")
	existing = (
		frappe.qb.from_(alloc)
		.select(alloc.name, alloc.bed)
		.where((alloc.student == student) & (alloc.status == ACTIVE) & (alloc.name != (exclude_name or "")))
		.for_update()
		.run(as_dict=True)
	)
	if existing:
		frappe.throw(
			_("Student {0} already has an active hostel bed allocation ({1})").format(
				student, existing[0].bed
			)
		)

	if frappe.db.table_exists("Transport Allocation"):
		transport = frappe.qb.DocType("Transport Allocation")
		active_transport = (
			frappe.qb.from_(transport)
			.select(transport.name, transport.route)
			.where((transport.student == student) & (transport.status == ACTIVE))
			.for_update()
			.run(as_dict=True)
		)
		if active_transport:
			frappe.throw(
				_(
					"Student {0} already has an active transport allocation on route {1}; vacate transport before allocating a hostel bed"
				).format(student, active_transport[0].route)
			)


def occupy_bed(bed, student):
	frappe.db.set_value(
		"Hostel Bed",
		bed,
		{"status": BED_OCCUPIED, "occupied_by": student},
		update_modified=False,
	)


def release_bed_if_vacated(bed, student, exclude_name=None):
	if not bed or not frappe.db.exists("Hostel Bed", bed):
		return
	alloc = frappe.qb.DocType("Hostel Bed Allocation")
	still_active = (
		frappe.qb.from_(alloc)
		.select(alloc.student)
		.where((alloc.bed == bed) & (alloc.status == ACTIVE) & (alloc.name != (exclude_name or "")))
		.limit(1)
		.run(as_dict=True)
	)
	if still_active:
		occupy_bed(bed, still_active[0].student)
	else:
		frappe.db.set_value(
			"Hostel Bed",
			bed,
			{"status": BED_AVAILABLE, "occupied_by": None},
			update_modified=False,
		)


def refresh_student_resident_flag(student):
	if not student or not frappe.db.exists("Student", student):
		return
	has_active = bool(
		frappe.db.exists("Hostel Bed Allocation", {"student": student, "status": ACTIVE})
	)
	frappe.db.set_value("Student", student, "oc_hostel_resident", int(has_active), update_modified=False)


def hostel_fee_category(company):
	configured = frappe.db.get_value("Hostel Settings", {"company": company}, "hostel_fee_category")
	return configured or HOSTEL


def positive_rates(company, academic_year, room_type, override_amount=None):
	rates = {
		head: flt(amount)
		for head, amount in fee_rate(company, academic_year, room_type).items()
		if flt(amount) > 0
	}
	if not rates and flt(override_amount) > 0:
		rates = {hostel_fee_category(company): flt(override_amount)}
	if not rates:
		frappe.throw(
			_("No Hostel Fee Rate found for {0}, {1}, {2}").format(company, academic_year, room_type)
		)
	return rates


def raise_hostel_invoice(allocation):
	"""Raise and submit the Hostel Fee Sales Invoice under the hostel's owning company."""
	room_type = frappe.db.get_value("Hostel Room", allocation.room, "room_type")
	rates = positive_rates(
		allocation.company,
		allocation.academic_year,
		room_type,
		getattr(allocation, "_fee_amount", None),
	)
	remarks = _("Hostel bed allocation {0} ({1}, {2})").format(
		allocation.bed, allocation.hostel, allocation.academic_year
	)
	if len(rates) == 1:
		category, amount = next(iter(rates.items()))
		head_type = frappe.db.get_value("Fee Category", category, "oc_fee_head_type") or category
		if category == head_type:
			return raise_fee_invoice(allocation.student, head_type, amount, allocation.company, remarks)

	customer, receivable, _item = check_fee_invoice(allocation.student, HOSTEL, allocation.company)
	items = []
	for category, amount in rates.items():
		item_code = frappe.db.get_value("Fee Category", category, "item") or category
		items.append({"item_code": item_code, "qty": 1, "rate": amount})
	posting_date = nowdate()
	invoice = frappe.get_doc(
		{
			"doctype": "Sales Invoice",
			"company": allocation.company,
			"customer": customer,
			"student": allocation.student,
			"posting_date": posting_date,
			"set_posting_time": 1,
			"due_date": add_days(posting_date, DUE_DAYS),
			"remarks": remarks,
			"debit_to": receivable,
			"items": items,
		}
	)
	invoice.flags.ignore_permissions = True
	invoice.insert()
	return invoice.submit()
