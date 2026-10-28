"""M11-02 tests: Transport Allocation (`allocate_seat`) and lifecycle side-exit teardown hook."""

from unittest.mock import patch

import frappe
from frappe.utils import add_days, cint, flt, getdate, today

from campus.hostel.api import add_beds
from campus.hostel.doctype.hostel_bed_allocation.hostel_bed_allocation import (
	BED_AVAILABLE,
	allocate_bed,
)
from campus.student_services import lifecycle
from campus.student_services import lifecycle_rules as rules
from campus.tests.finance_factories import make_finance_company
from campus.tests.utils import CampusTestCase
from campus.transport.doctype.transport_allocation.transport_allocation import (
	ACTIVE,
	VACATED,
	allocate_seat,
)

TRUST = "Transport Alloc Test Trust"
COLLEGE_A = "Transport Alloc College A"
OUTSIDER = "Transport Alloc Outsider"
YEAR = "Transport Alloc 2026-27"
FIXTURE_STUDENT = "EDU-STU-00001"


def ensure_gender(name):
	if not frappe.db.exists("Gender", name):
		frappe.get_doc({"doctype": "Gender", "gender": name}).insert()


class TestTransportAllocation(CampusTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_finance_company(TRUST, "TATT")
		make_finance_company(COLLEGE_A, "TACA")
		make_finance_company(OUTSIDER, "TAOC")
		for gender in ("Male", "Female"):
			ensure_gender(gender)
		if not frappe.db.exists("Academic Year", YEAR):
			frappe.get_doc(
				{
					"doctype": "Academic Year",
					"academic_year_name": YEAR,
					"year_start_date": f"{today()[:4]}-01-01",
					"year_end_date": f"{today()[:4]}-12-31",
				}
			).insert()

	def setUp(self):
		frappe.db.savepoint("transport_allocation_test")
		self.addCleanup(frappe.db.rollback, save_point="transport_allocation_test")
		self.transport_agreement = frappe.get_doc(
			{
				"doctype": "Shared Service Agreement",
				"provider_company": TRUST,
				"service": "Transport",
				"from_date": add_days(today(), -30),
				"members": [{"company": COLLEGE_A}],
			}
		).insert()
		self.route = frappe.get_doc(
			{
				"doctype": "Transport Route",
				"route_name": "Route-01 Tambaram",
				"company": TRUST,
				"vehicle_no": "TN-01-AB-1234",
				"seat_capacity": 2,
				"stops": [
					{"stop_name": "Tambaram", "distance_km": 18, "fee_amount": 22000},
					{"stop_name": "Chromepet", "distance_km": 14, "fee_amount": 18000},
				],
			}
		).insert()

	def make_student(self, first_name, company=COLLEGE_A, gender="Male", name=None):
		if name and frappe.db.exists("Student", name):
			frappe.db.set_value(
				"Student",
				name,
				{
					"oc_company": company,
					"gender": gender,
					"oc_academic_status": rules.ACTIVE,
					"oc_transport_user": 0,
					"oc_hostel_resident": 0,
				},
			)
			return frappe.get_doc("Student", name)
		doc = frappe.get_doc(
			{
				"doctype": "Student",
				"first_name": first_name,
				"student_email_id": f"{first_name.lower()}@transport-alloc.example.com",
				"oc_company": company,
				"gender": gender,
			}
		).insert()
		if name and doc.name != name:
			frappe.rename_doc("Student", doc.name, name, force=True)
			return frappe.get_doc("Student", name)
		return doc

	def test_allocate_seat_creates_allocation_increments_occupancy_and_raises_trust_invoice(self):
		student = self.make_student("FixtureBusStu", name=FIXTURE_STUDENT)

		allocation = allocate_seat(student.name, self.route.name, "Tambaram", academic_year=YEAR)

		self.assertEqual(allocation.status, ACTIVE)
		self.assertEqual(allocation.student, student.name)
		self.assertEqual(allocation.route, self.route.name)
		self.assertEqual(allocation.stop, "Tambaram")
		self.assertEqual(allocation.company, TRUST)
		self.assertEqual(flt(allocation.fee_amount), 22000.0)
		self.assertTrue(allocation.sales_invoice)

		self.assertEqual(
			cint(frappe.db.get_value("Transport Route", self.route.name, "occupied_seats")), 1
		)
		self.assertEqual(frappe.db.get_value("Student", student.name, "oc_transport_user"), 1)

		invoice = frappe.get_doc("Sales Invoice", allocation.sales_invoice)
		self.assertEqual(invoice.docstatus, 1)
		self.assertEqual(invoice.company, TRUST)
		self.assertEqual(invoice.student, student.name)
		self.assertEqual(flt(invoice.grand_total), 22000.0)

	def test_allocate_seat_acquires_mariadb_row_lock(self):
		student = self.make_student("BusLockStu")
		locked_calls = []
		real_get_value = frappe.db.get_value

		def spy_get_value(*args, **kwargs):
			if kwargs.get("for_update"):
				locked_calls.append(args[:2])
			return real_get_value(*args, **kwargs)

		with (
			patch.object(frappe.db, "get_value", side_effect=spy_get_value),
			patch.object(frappe.db, "sql", wraps=frappe.db.sql) as sql_spy,
		):
			allocate_seat(student.name, self.route.name, "Chromepet")

		self.assertIn(("Transport Route", self.route.name), locked_calls)
		self.assertTrue(
			any(
				"tabTransport Allocation" in str(c.args[0]) and "FOR UPDATE" in str(c.args[0])
				for c in sql_spy.call_args_list
			)
		)

	def test_allocate_seat_enforces_capacity_stop_validity_duplicate_student_and_shared_service(self):
		s1 = self.make_student("RiderOne")
		s2 = self.make_student("RiderTwo")
		s3 = self.make_student("RiderThree")
		outsider = self.make_student("RiderOutsider", company=OUTSIDER)

		with self.assertRaisesRegex(frappe.ValidationError, "does not serve"):
			allocate_seat(outsider.name, self.route.name, "Tambaram")

		with self.assertRaisesRegex(frappe.ValidationError, "is not on route"):
			allocate_seat(s1.name, self.route.name, "Guindy")

		allocate_seat(s1.name, self.route.name, "Tambaram")

		with self.assertRaisesRegex(
			frappe.ValidationError, "already has an active transport allocation"
		):
			allocate_seat(s1.name, self.route.name, "Chromepet")

		allocate_seat(s2.name, self.route.name, "Chromepet")
		self.assertEqual(
			cint(frappe.db.get_value("Transport Route", self.route.name, "occupied_seats")), 2
		)

		with self.assertRaisesRegex(frappe.ValidationError, "is full"):
			allocate_seat(s3.name, self.route.name, "Tambaram")

	def test_student_cannot_hold_hostel_and_transport_simultaneously(self):
		frappe.get_doc(
			{
				"doctype": "Shared Service Agreement",
				"provider_company": TRUST,
				"service": "Hostel",
				"from_date": add_days(today(), -30),
				"members": [{"company": COLLEGE_A}],
			}
		).insert()
		hostel = frappe.get_doc(
			{
				"doctype": "Hostel",
				"hostel_name": "Branching Test Hostel",
				"company": TRUST,
				"gender": "Male",
			}
		).insert()
		block = frappe.get_doc(
			{"doctype": "Hostel Block", "hostel": hostel.name, "block_name": "A"}
		).insert()
		floor = frappe.get_doc(
			{"doctype": "Hostel Floor", "block": block.name, "floor_name": "1"}
		).insert()
		if not frappe.db.exists("Hostel Room Type", "Double"):
			frappe.get_doc({"doctype": "Hostel Room Type", "room_type_name": "Double"}).insert()
		room = frappe.get_doc(
			{"doctype": "Hostel Room", "floor": floor.name, "room_no": "101", "room_type": "Double"}
		).insert()
		beds = add_beds(room.name, 2)
		frappe.get_doc(
			{
				"doctype": "Hostel Fee Rate",
				"company": TRUST,
				"academic_year": YEAR,
				"room_type": "Double",
				"items": [{"fee_category": "Hostel", "amount": 40000}],
			}
		).insert()

		hosteller = self.make_student("HostelBranchStu")
		bed_alloc = allocate_bed(hosteller.name, beds[0], YEAR)

		with self.assertRaisesRegex(
			frappe.ValidationError, "already has an active hostel bed allocation"
		):
			allocate_seat(hosteller.name, self.route.name, "Tambaram", academic_year=YEAR)

		day_scholar = self.make_student("BusBranchStu")
		seat_alloc = allocate_seat(day_scholar.name, self.route.name, "Tambaram", academic_year=YEAR)

		with self.assertRaisesRegex(
			frappe.ValidationError, "already has an active transport allocation"
		):
			allocate_bed(day_scholar.name, beds[1], YEAR)

		# Once the student vacates hostel, they can switch to transport (and vice versa)
		bed_alloc.status = VACATED
		bed_alloc.save()
		switched_to_bus = allocate_seat(hosteller.name, self.route.name, "Chromepet", academic_year=YEAR)
		self.assertEqual(switched_to_bus.status, ACTIVE)

		seat_alloc.status = VACATED
		seat_alloc.save()
		switched_to_hostel = allocate_bed(day_scholar.name, beds[1], YEAR)
		self.assertEqual(switched_to_hostel.status, ACTIVE)

	def test_side_exit_transitions_set_end_date_and_free_transport_seat(self):
		exit_date = add_days(today(), 7)
		for idx, exit_status in enumerate(
			(rules.ON_BREAK, rules.WITHDRAWN, rules.DISCONTINUED, rules.CANCELLED)
		):
			with self.subTest(exit_status=exit_status):
				student = self.make_student(f"BusExitStu{idx}")
				seat_alloc = allocate_seat(student.name, self.route.name, "Tambaram", academic_year=YEAR)
				self.assertEqual(
					cint(frappe.db.get_value("Transport Route", self.route.name, "occupied_seats")), 1
				)

				lifecycle.transition(student.name, exit_status, exit_date, f"Moved to {exit_status}")

				seat_alloc.reload()
				self.assertEqual(seat_alloc.status, VACATED)
				self.assertEqual(getdate(seat_alloc.to_date), getdate(exit_date))
				self.assertEqual(getdate(seat_alloc.end_date), getdate(exit_date))

				self.assertEqual(
					cint(frappe.db.get_value("Transport Route", self.route.name, "occupied_seats")), 0
				)
				self.assertEqual(frappe.db.get_value("Student", student.name, "oc_transport_user"), 0)
