"""M10-02 tests: Hostel Bed Allocation (`allocate_bed`) and lifecycle side-exit teardown hook."""

from unittest.mock import patch

import frappe
from frappe.utils import add_days, flt, getdate, today

from campus.hostel.api import add_beds
from campus.hostel.doctype.hostel_bed_allocation.hostel_bed_allocation import (
	ACTIVE,
	BED_AVAILABLE,
	BED_OCCUPIED,
	VACATED,
	allocate_bed,
)
from campus.student_services import lifecycle
from campus.student_services import lifecycle_rules as rules
from campus.tests.finance_factories import make_finance_company
from campus.tests.utils import CampusTestCase

TRUST = "Hostel Alloc Test Trust"
COLLEGE_A = "Hostel Alloc College A"
OUTSIDER = "Hostel Alloc Outsider"
YEAR = "Hostel Alloc 2026-27"
FIXTURE_STUDENT = "EDU-STU-00001"


def ensure_gender(name):
	"""Ensure the `Gender` record `name` exists for test fixtures."""
	if not frappe.db.exists("Gender", name):
		frappe.get_doc({"doctype": "Gender", "gender": name}).insert()


class TestHostelAllocation(CampusTestCase):
	"""Integration tests for M10-02 Hostel Bed Allocation and side-exit lifecycle cleanup."""

	@classmethod
	def setUpClass(cls):
		"""Seed finance companies, genders, and academic year once for the suite."""
		super().setUpClass()
		make_finance_company(TRUST, "HATT")
		make_finance_company(COLLEGE_A, "HACA")
		make_finance_company(OUTSIDER, "HAOC")
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
		"""Create a savepoint and seed a Hostel Shared Service Agreement, hostel, room, beds, and fee rate."""
		frappe.db.savepoint("hostel_allocation_test")
		self.addCleanup(frappe.db.rollback, save_point="hostel_allocation_test")
		self.agreement = frappe.get_doc(
			{
				"doctype": "Shared Service Agreement",
				"provider_company": TRUST,
				"service": "Hostel",
				"from_date": add_days(today(), -30),
				"members": [{"company": COLLEGE_A}],
			}
		).insert()
		self.boys = frappe.get_doc(
			{
				"doctype": "Hostel",
				"hostel_name": "Alloc Boys Hostel",
				"company": TRUST,
				"gender": "Male",
			}
		).insert()
		self.room = self.make_room(self.boys, "G", "101", room_type="Double")
		self.beds = add_beds(self.room.name, 2)
		self.rate = frappe.get_doc(
			{
				"doctype": "Hostel Fee Rate",
				"company": TRUST,
				"academic_year": YEAR,
				"room_type": "Double",
				"items": [{"fee_category": "Hostel", "amount": 45000}],
			}
		).insert()

	def make_room(self, hostel, floor_name, room_no, room_type="Double"):
		"""Create or reuse a block, floor, room type, and hostel room for `hostel`."""
		block_name = f"B-{floor_name}"
		block_id = f"{hostel.name}-{block_name}"
		if not frappe.db.exists("Hostel Block", block_id):
			frappe.get_doc(
				{"doctype": "Hostel Block", "hostel": hostel.name, "block_name": block_name}
			).insert()
		floor_id = f"{block_id}-{floor_name}"
		if not frappe.db.exists("Hostel Floor", floor_id):
			frappe.get_doc(
				{"doctype": "Hostel Floor", "block": block_id, "floor_name": floor_name}
			).insert()
		if not frappe.db.exists("Hostel Room Type", room_type):
			frappe.get_doc({"doctype": "Hostel Room Type", "room_type_name": room_type}).insert()
		return frappe.get_doc(
			{"doctype": "Hostel Room", "floor": floor_id, "room_no": room_no, "room_type": room_type}
		).insert()

	def make_student(self, first_name, company=COLLEGE_A, gender="Male", name=None):
		"""Create or reset a Student fixture in `company` with `gender`."""
		if name and frappe.db.exists("Student", name):
			frappe.db.set_value(
				"Student",
				name,
				{
					"oc_company": company,
					"gender": gender,
					"oc_academic_status": rules.ACTIVE,
					"oc_hostel_resident": 0,
				},
			)
			return frappe.get_doc("Student", name)
		doc = frappe.get_doc(
			{
				"doctype": "Student",
				"first_name": first_name,
				"student_email_id": f"{first_name.lower()}@hostel-alloc.example.com",
				"oc_company": company,
				"gender": gender,
			}
		).insert()
		if name and doc.name != name:
			frappe.rename_doc("Student", doc.name, name, force=True)
			return frappe.get_doc("Student", name)
		return doc

	def test_allocate_bed_creates_allocation_occupies_bed_and_raises_trust_invoice(self):
		"""Verify `allocate_bed` occupies the bed, sets resident flag, and submits a Trust Sales Invoice."""
		student = self.make_student("FixtureStu", name=FIXTURE_STUDENT)
		bed = self.beds[0]

		allocation = allocate_bed(student.name, bed, YEAR)

		self.assertEqual(allocation.status, ACTIVE)
		self.assertEqual(allocation.student, student.name)
		self.assertEqual(allocation.bed, bed)
		self.assertEqual(allocation.room, self.room.name)
		self.assertEqual(allocation.hostel, self.boys.name)
		self.assertEqual(allocation.company, TRUST)
		self.assertTrue(allocation.sales_invoice)

		bed_state = frappe.db.get_value("Hostel Bed", bed, ["status", "occupied_by"], as_dict=True)
		self.assertEqual((bed_state.status, bed_state.occupied_by), (BED_OCCUPIED, student.name))
		self.assertEqual(frappe.db.get_value("Student", student.name, "oc_hostel_resident"), 1)

		invoice = frappe.get_doc("Sales Invoice", allocation.sales_invoice)
		self.assertEqual(invoice.docstatus, 1)
		self.assertEqual(invoice.company, TRUST)
		self.assertEqual(invoice.student, student.name)
		self.assertEqual(flt(invoice.grand_total), 45000.0)

		allocation.delete()
		self.assertEqual(frappe.db.get_value("Student", student.name, "oc_hostel_resident"), 0)

	def test_allocate_bed_acquires_mariadb_row_lock(self):
		"""Verify `allocate_bed` locks `Student`, `Hostel Bed`, and `Hostel Bed Allocation` rows with `FOR UPDATE`."""
		student = self.make_student("RowLockStu")
		bed = self.beds[0]
		locked_calls = []
		real_get_value = frappe.db.get_value

		def spy_get_value(*args, **kwargs):
			"""Record `frappe.db.get_value` calls made with `for_update=True`."""
			if kwargs.get("for_update"):
				locked_calls.append(args[:2])
			return real_get_value(*args, **kwargs)

		with (
			patch.object(frappe.db, "get_value", side_effect=spy_get_value),
			patch.object(frappe.db, "sql", wraps=frappe.db.sql) as sql_spy,
		):
			allocate_bed(student.name, bed, YEAR)

		self.assertIn(("Student", student.name), locked_calls)
		self.assertIn(("Hostel Bed", bed), locked_calls)
		self.assertTrue(
			any(
				"tabHostel Bed Allocation" in str(c.args[0]) and "FOR UPDATE" in str(c.args[0])
				for c in sql_spy.call_args_list
			)
		)

	def test_allocate_bed_checks_gender_and_shared_service_agreement(self):
		"""Verify `allocate_bed` enforces gender matching and active `Shared Service Agreement` coverage."""
		girl = self.make_student("GirlStu", company=COLLEGE_A, gender="Female")
		outsider = self.make_student("OutsiderStu", company=OUTSIDER, gender="Male")

		with self.assertRaisesRegex(frappe.ValidationError, "cannot take a student of gender"):
			allocate_bed(girl.name, self.beds[0], YEAR)

		with self.assertRaisesRegex(frappe.ValidationError, "does not serve"):
			allocate_bed(outsider.name, self.beds[0], YEAR)

		self.agreement.db_set("to_date", add_days(today(), -1))
		boy = self.make_student("ExpiredAgreementStu", company=COLLEGE_A, gender="Male")
		with self.assertRaisesRegex(frappe.ValidationError, "does not serve"):
			allocate_bed(boy.name, self.beds[0], YEAR)

	def test_allocate_bed_refuses_occupied_bed_duplicate_student_and_deactivating_occupied_bed(self):
		"""Verify occupied beds, duplicate active student allocations, and bed deactivation are rejected."""
		first = self.make_student("FirstBoy")
		second = self.make_student("SecondBoy")
		allocate_bed(first.name, self.beds[0], YEAR)

		with self.assertRaisesRegex(frappe.ValidationError, "already occupied"):
			allocate_bed(second.name, self.beds[0], YEAR)

		with self.assertRaisesRegex(frappe.ValidationError, "already has an active hostel bed allocation"):
			allocate_bed(first.name, self.beds[1], YEAR)

		bed_doc = frappe.get_doc("Hostel Bed", self.beds[0])
		bed_doc.is_active = 0
		with self.assertRaisesRegex(frappe.ValidationError, "Cannot deactivate an occupied bed"):
			bed_doc.save()

	def test_side_exit_transitions_set_end_date_and_free_bed(self):
		"""Verify side-exit lifecycle transitions vacate the allocation, set `to_date`, and free the bed."""
		exit_date = add_days(today(), 5)
		for idx, exit_status in enumerate(
			(rules.ON_BREAK, rules.WITHDRAWN, rules.DISCONTINUED, rules.CANCELLED)
		):
			with self.subTest(exit_status=exit_status):
				student = self.make_student(f"ExitStu{idx}")
				bed = self.beds[0]
				allocation = allocate_bed(student.name, bed, YEAR)

				lifecycle.transition(student.name, exit_status, exit_date, f"Moved to {exit_status}")

				allocation.reload()
				self.assertEqual(allocation.status, VACATED)
				self.assertEqual(getdate(allocation.to_date), getdate(exit_date))
				self.assertEqual(getdate(allocation.end_date), getdate(exit_date))

				bed_state = frappe.db.get_value(
					"Hostel Bed", bed, ["status", "occupied_by"], as_dict=True
				)
				self.assertEqual((bed_state.status, bed_state.occupied_by), (BED_AVAILABLE, None))
				self.assertEqual(frappe.db.get_value("Student", student.name, "oc_hostel_resident"), 0)

		next_student = self.make_student("NextInBed")
		reallocated = allocate_bed(next_student.name, self.beds[0], YEAR)
		self.assertEqual(reallocated.status, ACTIVE)
		self.assertEqual(
			frappe.db.get_value("Hostel Bed", self.beds[0], "occupied_by"), next_student.name
		)
