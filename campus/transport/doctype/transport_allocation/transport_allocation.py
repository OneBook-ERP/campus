"""M11-02 Transport Allocation: row-locked seat allocation, trust fee invoice, and side-exit vacating."""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, getdate, today

from campus.campus_finance.fee_heads import TRANSPORT
from campus.campus_finance.invoices import check_fee_invoice, raise_fee_invoice
from campus.campus_platform.api import served_companies
from campus.student_services import lifecycle_rules as rules
from campus.student_services.onboarding import mark_item_done

SERVICE = "Transport"
ACTIVE, VACATED = "Active", "Vacated"
SIDE_EXIT_STATUSES = (rules.ON_BREAK, rules.WITHDRAWN, rules.DISCONTINUED, rules.CANCELLED)


class TransportAllocation(Document):
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

		route_row = lock_route(self.route)
		self.company = route_row.company
		self.fee_amount = resolve_stop_fee(
			self.route, self.stop, getattr(self, "_fee_amount", None) or self.fee_amount
		)

		if self.status == ACTIVE:
			self._validate_active_allocation(route_row)
		elif self.status == VACATED and not self.to_date:
			self.to_date = today()

	def _validate_active_allocation(self, route_row):
		if route_row.disabled:
			frappe.throw(_("Transport Route {0} is disabled").format(self.route))

		validate_allotment(self.route, self.student, route_company=route_row.company)
		student_status = frappe.db.get_value("Student", self.student, "oc_academic_status")
		if student_status in SIDE_EXIT_STATUSES:
			frappe.throw(
				_("Student {0} is {1} and cannot be allocated a transport seat").format(
					self.student, student_status
				)
			)

		check_seat_capacity(route_row, exclude_name=self.name)
		check_student_unallocated(self.student, exclude_name=self.name)
		if self.is_new() and not getattr(self.flags, "skip_fee_invoice", False):
			if flt(self.fee_amount) <= 0:
				frappe.throw(
					_("No positive Transport Fee found for stop {0} on route {1}").format(
						self.stop, self.route
					)
				)
			check_fee_invoice(self.student, TRANSPORT, self.company)

	def after_insert(self):
		if self.status != ACTIVE:
			return
		refresh_route_occupancy(self.route)
		frappe.db.set_value("Student", self.student, "oc_transport_user", 1, update_modified=False)
		mark_item_done(self.student, "BUS-PASS")
		mark_item_done(self.student, "TRANSPORT")
		if not self.sales_invoice and not getattr(self.flags, "skip_fee_invoice", False):
			invoice = raise_transport_invoice(self)
			self.db_set("sales_invoice", invoice.name, update_modified=False)

	def on_update(self):
		refresh_route_occupancy(self.route)
		refresh_student_transport_flag(self.student)

	def on_trash(self):
		refresh_route_occupancy(self.route, exclude_name=self.name)
		refresh_student_transport_flag(self.student)


@frappe.whitelist(methods=["POST"])
def allocate_seat(student, route, stop, academic_year=None, from_date=None, fee_amount=None):
	"""Allocate a seat on `route` at `stop` to `student` under a row lock and raise the Transport Fee invoice."""
	doc = frappe.get_doc(
		{
			"doctype": "Transport Allocation",
			"student": student,
			"route": route,
			"stop": stop,
			"academic_year": academic_year,
			"from_date": from_date or today(),
			"status": ACTIVE,
		}
	)
	if fee_amount is not None:
		doc._fee_amount = fee_amount
	doc.insert()
	return doc


def vacate_seat_for_student(student, end_date=None):
	"""End all active Transport Allocations for `student` on `end_date` and free their bus seats."""
	if not frappe.db.table_exists("Transport Allocation"):
		return []
	effective = getdate(end_date or today())
	alloc = frappe.qb.DocType("Transport Allocation")
	rows = (
		frappe.qb.from_(alloc)
		.select(alloc.name, alloc.route, alloc.from_date)
		.where((alloc.student == student) & (alloc.status == ACTIVE))
		.for_update()
		.run(as_dict=True)
	)
	for row in rows:
		lock_route(row.route)
		vacated_on = max(effective, getdate(row.from_date)) if row.from_date else effective
		frappe.db.set_value(
			"Transport Allocation",
			row.name,
			{"status": VACATED, "to_date": vacated_on},
			update_modified=False,
		)
		refresh_route_occupancy(row.route)
	refresh_student_transport_flag(student)
	return [row.name for row in rows]


vacate_for_student = vacate_seat_for_student


def served_colleges(route, route_company=None):
	"""Colleges the route's owner serves under its Transport Shared Service Agreements in force today."""
	company = route_company or frappe.db.get_value("Transport Route", route, "company")
	return served_companies(SERVICE, company)


def validate_allotment(route, student, route_company=None):
	"""Throw unless `student` may be allotted on `route`: route exists, is enabled, and student's college is served."""
	if not route_company:
		row = frappe.db.get_value("Transport Route", route, ["company", "disabled"], as_dict=True)
		if not row:
			frappe.throw(_("Transport Route {0} does not exist").format(route))
		if row.disabled:
			frappe.throw(_("Transport Route {0} is disabled").format(route))
		route_company = row.company

	details = frappe.db.get_value("Student", student, ["oc_company"], as_dict=True)
	if not details:
		frappe.throw(_("Student {0} does not exist").format(student))
	if not details.oc_company:
		frappe.throw(_("Student {0} has no college set").format(student))
	if details.oc_company not in served_colleges(route, route_company=route_company):
		frappe.throw(_("{0} does not serve {1}").format(route, details.oc_company))


def lock_route(route):
	"""Lock the Transport Route row for the current transaction and return its fields."""
	if not route:
		frappe.throw(_("Transport Route is required"))
	row = frappe.db.get_value(
		"Transport Route",
		route,
		["name", "company", "seat_capacity", "occupied_seats", "disabled"],
		as_dict=True,
		for_update=True,
	)
	if not row:
		frappe.throw(_("Transport Route {0} does not exist").format(route))
	return row


def resolve_stop_fee(route, stop, override_amount=None):
	"""Verify `stop` on `route` (when stops are configured) and return the applicable fee amount."""
	if not stop or not str(stop).strip():
		frappe.throw(_("Pickup / Drop Stop is required"))
	stops = frappe.get_all(
		"Transport Route Stop",
		filters={"parent": route, "parenttype": "Transport Route"},
		fields=["stop_name", "fee_amount"],
	)
	if stops:
		matched = next(
			(s for s in stops if (s.stop_name or "").strip().casefold() == str(stop).strip().casefold()),
			None,
		)
		if not matched:
			frappe.throw(_("Stop {0} is not on route {1}").format(stop, route))
		return flt(override_amount) if flt(override_amount) > 0 else flt(matched.fee_amount)
	return flt(override_amount)


def check_seat_capacity(route_row, exclude_name=None):
	"""Verify under row lock that `route_row` has at least one free seat."""
	alloc = frappe.qb.DocType("Transport Allocation")
	active = (
		frappe.qb.from_(alloc)
		.select(alloc.name)
		.where(
			(alloc.route == route_row.name)
			& (alloc.status == ACTIVE)
			& (alloc.name != (exclude_name or ""))
		)
		.for_update()
		.run(as_dict=True)
	)
	if len(active) >= cint(route_row.seat_capacity):
		frappe.throw(
			_("Transport Route {0} is full ({1}/{2} seats occupied)").format(
				route_row.name, len(active), route_row.seat_capacity
			)
		)


def check_student_unallocated(student, exclude_name=None):
	"""Verify under row lock that `student` does not already hold an active transport or hostel bed allocation."""
	alloc = frappe.qb.DocType("Transport Allocation")
	existing = (
		frappe.qb.from_(alloc)
		.select(alloc.name, alloc.route)
		.where((alloc.student == student) & (alloc.status == ACTIVE) & (alloc.name != (exclude_name or "")))
		.for_update()
		.run(as_dict=True)
	)
	if existing:
		frappe.throw(
			_("Student {0} already has an active transport allocation on route {1}").format(
				student, existing[0].route
			)
		)

	if frappe.db.table_exists("Hostel Bed Allocation"):
		hostel = frappe.qb.DocType("Hostel Bed Allocation")
		active_hostel = (
			frappe.qb.from_(hostel)
			.select(hostel.name, hostel.bed)
			.where((hostel.student == student) & (hostel.status == ACTIVE))
			.for_update()
			.run(as_dict=True)
		)
		if active_hostel:
			frappe.throw(
				_(
					"Student {0} already has an active hostel bed allocation ({1}); vacate hostel before allocating transport"
				).format(student, active_hostel[0].bed)
			)


def refresh_route_occupancy(route, exclude_name=None):
	if not route or not frappe.db.exists("Transport Route", route):
		return
	alloc = frappe.qb.DocType("Transport Allocation")
	query = (
		frappe.qb.from_(alloc)
		.select(alloc.name)
		.where((alloc.route == route) & (alloc.status == ACTIVE))
	)
	if exclude_name:
		query = query.where(alloc.name != exclude_name)
	active_count = len(query.run(as_dict=True))
	frappe.db.set_value("Transport Route", route, "occupied_seats", active_count, update_modified=False)


def refresh_student_transport_flag(student):
	if not student or not frappe.db.exists("Student", student):
		return
	has_active = bool(
		frappe.db.exists("Transport Allocation", {"student": student, "status": ACTIVE})
	)
	frappe.db.set_value("Student", student, "oc_transport_user", int(has_active), update_modified=False)


def raise_transport_invoice(allocation):
	"""Raise and submit the Transport Fee Sales Invoice under the route's owning company."""
	remarks = _("Transport seat allocation on {0} ({1})").format(allocation.route, allocation.stop)
	return raise_fee_invoice(
		allocation.student,
		TRANSPORT,
		flt(allocation.fee_amount),
		allocation.company,
		remarks,
	)
