"""M11-01 Transport Route master: trust-owned bus route with stops and seat capacity."""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt


class TransportRoute(Document):
	"""Bus route owned by a transport provider company with stops and seat capacity."""

	def validate(self):
		"""Verify provider agreement, positive seat capacity, unique stop names, and active occupancy bounds."""
		if self.is_new() and not frappe.db.exists(
			"Shared Service Agreement", {"provider_company": self.company, "service": "Transport"}
		):
			frappe.throw(
				_("{0} is not the provider on any Transport Shared Service Agreement").format(self.company)
			)
		if cint(self.seat_capacity) <= 0:
			frappe.throw(_("Seat Capacity must be greater than zero"))

		seen_stops = set()
		for row in self.stops or []:
			stop_key = (row.stop_name or "").strip().lower()
			if not stop_key:
				frappe.throw(_("Stop Name is required"))
			if stop_key in seen_stops:
				frappe.throw(_("Duplicate stop {0} on route {1}").format(row.stop_name, self.route_name))
			seen_stops.add(stop_key)
			if flt(row.fee_amount) < 0:
				frappe.throw(_("Fee Amount for stop {0} cannot be negative").format(row.stop_name))

		if not self.is_new() and frappe.db.table_exists("Transport Allocation"):
			active_count = frappe.db.count(
				"Transport Allocation", {"route": self.name, "status": "Active"}
			)
			if cint(self.seat_capacity) < active_count:
				frappe.throw(
					_("Seat Capacity ({0}) cannot be less than current occupied seats ({1})").format(
						self.seat_capacity, active_count
					)
				)
			self.occupied_seats = active_count
		else:
			self.occupied_seats = 0
