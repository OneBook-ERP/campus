"""M12-01 / M12-05 labs and equipment: lab masters, equipment by Asset Location, calibration due and notice, inspection, incident."""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase
from frappe.utils import add_days, add_to_date, now_datetime, today

from campus.labs.calibration import due_tasks, send_calibration_notices
from campus.labs.equipment import lab_equipment
from campus.labs.report.calibration_and_amc_due.calibration_and_amc_due import execute as due_report
from campus.labs.report.lab_equipment_register.lab_equipment_register import execute as register_report
from campus.labs.report.lab_incident_register.lab_incident_register import execute as incident_report
from campus.labs.rules import days_left, inspection_outcome, notice_due
from campus.labs.setup import setup_labs
from campus.tests.factories import make_company, make_user
from campus.tests.utils import CampusTestCase

COLLEGE, OTHER = "Lab Test College", "Lab Test Other College"
READER = "lab-reader@example.com"
OTHER_READER = "lab-other-reader@example.com"


class TestLabRules(UnitTestCase):
	def test_days_left_counts_to_the_due_date(self):
		self.assertEqual(days_left("2026-10-30", "2026-10-22"), 8)
		self.assertEqual(days_left("2026-10-20", "2026-10-22"), -2)

	def test_notice_starts_exactly_notice_days_ahead(self):
		self.assertFalse(notice_due("2026-11-07", "2026-10-22", 15))  # 16 days
		self.assertTrue(notice_due("2026-11-06", "2026-10-22", 15))  # 15 days
		self.assertTrue(notice_due("2026-10-22", "2026-10-22", 15))  # today

	def test_no_notice_once_overdue(self):
		self.assertFalse(notice_due("2026-10-21", "2026-10-22", 15))

	def test_a_critical_point_not_met_fails_the_inspection(self):
		self.assertEqual(inspection_outcome([{"compliant": 0, "critical": 1}, {"compliant": 1, "critical": 0}]), "Fail")

	def test_only_ordinary_points_not_met_needs_action(self):
		self.assertEqual(inspection_outcome([{"compliant": 1, "critical": 1}, {"compliant": 0, "critical": 0}]), "Needs Action")

	def test_everything_met_passes(self):
		self.assertEqual(inspection_outcome([{"compliant": 1, "critical": 1}]), "Pass")
		self.assertEqual(inspection_outcome([]), "Pass")


class LabCase(CampusTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_company(COLLEGE, "LTC")
		make_company(OTHER, "LTO")
		make_user(READER, [COLLEGE])
		make_user(OTHER_READER, [OTHER])
		frappe.get_doc("User", READER).add_roles("Lab In-charge")
		if not frappe.db.exists("Gender", "Male"):
			frappe.get_doc({"doctype": "Gender", "gender": "Male"}).insert()

	def setUp(self):
		frappe.db.savepoint("labs_test")
		self.addCleanup(frappe.db.rollback, save_point="labs_test")
		self.addCleanup(frappe.set_user, "Administrator")
		sendmail = patch("frappe.sendmail", return_value=frappe._dict(name="test-mail"))
		sendmail.start()
		self.addCleanup(sendmail.stop)
		setup_labs()
		self.in_charge = self.make_employee("InCharge", COLLEGE)
		self.lab = self.make_lab("Physics Lab")

	def make_employee(self, name, company):
		return frappe.get_doc(
			{
				"doctype": "Employee", "first_name": name, "last_name": frappe.generate_hash(length=6), "gender": "Male",
				"date_of_birth": "1980-01-01", "date_of_joining": "2010-01-01", "company": company,
				"personal_email": f"{frappe.generate_hash(length=8)}@example.com",
			}
		).insert().name  # fmt: skip

	def make_lab(self, name, company=COLLEGE, in_charge=None, **values):
		return frappe.get_doc(
			{
				"doctype": "Laboratory", "lab_name": f"{name} {frappe.generate_hash(length=5)}", "company": company,
				"in_charge": in_charge or self.in_charge, **values,
			}
		).insert()  # fmt: skip

	def make_category(self):
		if not frappe.db.exists("Asset Category", "Lab Equipment"):
			frappe.get_doc({"doctype": "Asset Category", "asset_category_name": "Lab Equipment"}).insert(ignore_mandatory=True)
		return "Lab Equipment"

	def make_asset(self, lab, company=COLLEGE, status="Draft"):
		"""A saved Asset (kept as Draft: submitting needs depreciation accounts) located in the lab."""
		category = self.make_category()
		item = frappe.generate_hash(length=8)
		if not frappe.db.exists("Item Group", "Lab Items"):
			frappe.get_doc({"doctype": "Item Group", "item_group_name": "Lab Items"}).insert(ignore_mandatory=True)
		frappe.get_doc(
			{
				"doctype": "Item", "item_code": f"LAB-{item}", "item_name": f"Oscilloscope {item}", "item_group": "Lab Items",
				"is_stock_item": 0, "is_fixed_asset": 1, "is_sales_item": 0, "asset_category": category, "stock_uom": "Nos",
			}
		).insert(ignore_mandatory=True)  # india_compliance asks for an HSN code the test has no use for
		asset = frappe.get_doc(
			{
				"doctype": "Asset", "item_code": f"LAB-{item}", "asset_name": f"Oscilloscope {item}", "company": company,
				"asset_category": category, "location": lab.asset_location, "purchase_date": add_days(today(), -400),
				"available_for_use_date": add_days(today(), -400), "net_purchase_amount": 50000, "is_existing_asset": 1,
			}
		)
		asset.insert(ignore_permissions=True, ignore_mandatory=True)
		if status != "Draft":
			frappe.db.set_value("Asset", asset.name, "status", status)
		return asset

	def make_maintenance(self, asset, tasks, assign_to=READER):
		"""Asset Maintenance with `tasks` = [(name, type, next_due_in_days)], each assigned to `assign_to` (an
		assignment shares the record with that user)."""
		team = self.make_team()
		doc = frappe.get_doc(
			{
				"doctype": "Asset Maintenance", "asset_name": asset.name, "company": asset.company, "maintenance_team": team,
				"asset_maintenance_tasks": [
					{
						"maintenance_task": name, "maintenance_type": kind, "maintenance_status": "Planned",
						"start_date": add_days(today(), -300), "periodicity": "Yearly", "assign_to": assign_to,
						"next_due_date": add_days(today(), due_in),
					}
					for name, kind, due_in in tasks
				],
			}
		)
		doc.flags.ignore_permissions = True
		return doc.insert()

	def make_team(self):
		if not frappe.db.exists("Asset Maintenance Team", "Lab Team"):
			frappe.get_doc(
				{
					"doctype": "Asset Maintenance Team", "maintenance_team_name": "Lab Team", "company": COLLEGE, "maintenance_manager": READER,
					"maintenance_team_members": [{"team_member": READER, "maintenance_role": "Maintenance User"}],
				}
			).insert()
		return "Lab Team"

	def notice_logs(self, maintenance):
		return frappe.get_all(
			"Message Log", {"event_key": "lab.calibration_due", "reference_name": maintenance.name}, ["recipient", "status"]
		)


class TestLaboratory(LabCase):
	def test_a_laboratory_gets_an_asset_location(self):
		self.assertTrue(frappe.db.exists("Location", self.lab.asset_location))

	def test_people_and_courses_must_belong_to_the_college(self):
		stranger = self.make_employee("Stranger", OTHER)
		with self.assertRaises(frappe.ValidationError):
			self.make_lab("Chem Lab", in_charge=stranger)
		with self.assertRaises(frappe.ValidationError):
			self.make_lab("Chem Lab", technician=stranger)

	def test_an_unknown_company_department_is_refused(self):
		department = frappe.get_doc({"doctype": "Department", "department_name": "Lab Dept", "company": OTHER}).insert()
		with self.assertRaises(frappe.ValidationError):
			self.make_lab("Bio Lab", department=department.name)


class TestEquipmentAndCalibration(LabCase):
	def test_equipment_is_the_assets_located_in_the_lab(self):
		asset = self.make_asset(self.lab)
		other_lab = self.make_lab("Chem Lab")
		self.make_asset(other_lab)
		rows = lab_equipment([COLLEGE], self.lab.name)
		self.assertEqual([row.asset for row in rows], [asset.name])

	def test_an_asset_of_another_company_is_not_the_labs_equipment(self):
		self.make_asset(self.lab, company=OTHER)
		self.assertEqual(lab_equipment([COLLEGE], self.lab.name), [])

	def test_due_tasks_include_the_window_and_overdue_but_not_later(self):
		asset = self.make_asset(self.lab)
		self.make_maintenance(asset, [("In window", "Calibration", 15), ("Later", "Calibration", 16), ("Overdue", "Calibration", -3)])
		rows = due_tasks(15, laboratory=self.lab.name)
		self.assertEqual([(row.task, row.days_left) for row in rows], [("Overdue", -3), ("In window", 15)])

	def test_type_filter_separates_amc_visits_from_calibrations(self):
		asset = self.make_asset(self.lab)
		self.make_maintenance(asset, [("Cal", "Calibration", 5), ("AMC visit", "Preventive Maintenance", 5)])
		self.assertEqual([r.task for r in due_tasks(15, maintenance_type="Calibration", laboratory=self.lab.name)], ["Cal"])
		self.assertEqual(
			[r.task for r in due_tasks(15, maintenance_type="Preventive Maintenance", laboratory=self.lab.name)], ["AMC visit"]
		)

	def test_retired_equipment_is_not_due(self):
		asset = self.make_asset(self.lab, status="Scrapped")
		self.make_maintenance(asset, [("Cal", "Calibration", 5)])
		self.assertEqual(due_tasks(15, laboratory=self.lab.name), [])

	def test_calibration_fifteen_days_ahead_notifies_the_in_charge(self):
		maintenance = self.make_maintenance(self.make_asset(self.lab), [("Cal", "Calibration", 15)])
		self.assertEqual(send_calibration_notices(), 1)
		logs = self.notice_logs(maintenance)
		self.assertTrue(logs)
		self.assertEqual({log.recipient.split(":")[-1] for log in logs}, {self.in_charge})

	def test_sixteen_days_ahead_is_not_yet_notified(self):
		maintenance = self.make_maintenance(self.make_asset(self.lab), [("Cal", "Calibration", 16)])
		self.assertEqual(send_calibration_notices(), 0)
		self.assertFalse(self.notice_logs(maintenance))

	def test_the_next_day_it_becomes_due(self):
		maintenance = self.make_maintenance(self.make_asset(self.lab), [("Cal", "Calibration", 16)])
		self.assertEqual(send_calibration_notices(), 0)
		self.assertEqual(send_calibration_notices(add_days(today(), 1)), 1)
		self.assertTrue(self.notice_logs(maintenance))

	def test_the_daily_run_does_not_repeat_a_notice(self):
		maintenance = self.make_maintenance(self.make_asset(self.lab), [("Cal", "Calibration", 15)])
		send_calibration_notices()
		count = len(self.notice_logs(maintenance))
		send_calibration_notices(add_days(today(), 1))
		self.assertEqual(len(self.notice_logs(maintenance)), count)

	def test_a_new_due_date_after_the_calibration_is_notified_afresh(self):
		maintenance = self.make_maintenance(self.make_asset(self.lab), [("Cal", "Calibration", 15)])
		send_calibration_notices()
		count = len(self.notice_logs(maintenance))
		frappe.db.set_value("Asset Maintenance Task", maintenance.asset_maintenance_tasks[0].name, "next_due_date", add_days(today(), 10))
		self.assertEqual(send_calibration_notices(), 1)
		self.assertGreater(len(self.notice_logs(maintenance)), count)

	def test_amc_visits_and_overdue_calibrations_are_not_notified(self):
		maintenance = self.make_maintenance(self.make_asset(self.lab), [("AMC", "Preventive Maintenance", 5), ("Late", "Calibration", -2)])
		self.assertEqual(send_calibration_notices(), 0)
		self.assertFalse(self.notice_logs(maintenance))

	def test_the_notice_days_are_a_setting(self):
		frappe.db.set_single_value("Laboratory Settings", "calibration_notice_days", 30)
		maintenance = self.make_maintenance(self.make_asset(self.lab), [("Cal", "Calibration", 30)])
		self.assertEqual(send_calibration_notices(), 1)
		self.assertTrue(self.notice_logs(maintenance))

	def test_a_disabled_lab_is_not_notified(self):
		maintenance = self.make_maintenance(self.make_asset(self.lab), [("Cal", "Calibration", 5)])
		frappe.db.set_value("Laboratory", self.lab.name, "disabled", 1)
		self.assertEqual(send_calibration_notices(), 0)
		self.assertFalse(self.notice_logs(maintenance))


class TestReportsAreCompanyScoped(LabCase):
	def test_a_user_reads_only_the_labs_of_their_college(self):
		other_in_charge = self.make_employee("OtherInCharge", OTHER)
		other_lab = self.make_lab("Other Lab", company=OTHER, in_charge=other_in_charge)
		mine = self.make_asset(self.lab)
		theirs = self.make_asset(other_lab, company=OTHER)
		self.make_maintenance(mine, [("Cal", "Calibration", 5)])
		self.make_maintenance(theirs, [("Cal", "Calibration", 5)])
		frappe.set_user(READER)
		self.assertEqual({row["asset"] for row in register_report({})[1]}, {mine.name})
		self.assertEqual({row["asset"] for row in due_report({})[1]}, {mine.name})
		self.assertEqual(register_report({"company": OTHER})[1], [])
		self.assertEqual(due_report({"company": OTHER})[1], [])

	def test_the_in_charge_reads_the_assets_of_their_college_only(self):
		"""Labs > Equipment: a read-and-report rule for Lab In-charge, scoped by the company User Permission."""
		other_lab = self.make_lab("Other Lab", company=OTHER, in_charge=self.make_employee("OtherInCharge", OTHER))
		mine, theirs = self.make_asset(self.lab), self.make_asset(other_lab, company=OTHER)
		ours = self.make_maintenance(mine, [("Cal", "Calibration", 5)])
		self.make_maintenance(theirs, [("Cal", "Calibration", 5)], assign_to=OTHER_READER)  # not shared with READER
		frappe.set_user(READER)
		assets, maintenance = frappe.get_list("Asset", pluck="name"), frappe.get_list("Asset Maintenance", pluck="name")
		self.assertIn(mine.name, assets)
		self.assertNotIn(theirs.name, assets)
		self.assertIn(ours.name, maintenance)
		self.assertEqual(set(frappe.get_all("Asset Maintenance", {"name": ["in", maintenance]}, pluck="company")), {COLLEGE})
		self.assertFalse(frappe.has_permission("Asset", "write", mine.name))

	def test_the_in_charge_reads_lab_equipment_not_the_colleges_other_assets(self):
		"""I4: an asset outside every laboratory (furniture, a vehicle) and its maintenance stay shut to the Lab In-charge."""
		location = frappe.get_doc({"doctype": "Location", "location_name": f"Admin Block {frappe.generate_hash(length=5)}"}).insert()
		elsewhere = self.make_asset(SimpleNamespace(asset_location=location.name))
		equipment = self.make_asset(self.lab)
		elsewhere_plan = self.make_maintenance(elsewhere, [("Cal", "Calibration", 5)], assign_to=OTHER_READER)
		equipment_plan = self.make_maintenance(equipment, [("Cal", "Calibration", 5)])
		frappe.set_user(READER)
		self.assertEqual({a for a in frappe.get_list("Asset", pluck="name")} & {elsewhere.name, equipment.name}, {equipment.name})
		counted = frappe.get_list("Asset", filters={"name": ["in", [elsewhere.name, equipment.name]]}, pluck="name")
		self.assertEqual(counted, [equipment.name])
		self.assertTrue(frappe.has_permission("Asset", "read", equipment.name))
		self.assertFalse(frappe.has_permission("Asset", "read", elsewhere.name))
		plans = frappe.get_list("Asset Maintenance", pluck="name")
		self.assertIn(equipment_plan.name, plans)
		self.assertNotIn(elsewhere_plan.name, plans)
		self.assertFalse(frappe.has_permission("Asset Maintenance", "read", elsewhere_plan.name))

	def test_a_user_with_another_asset_role_is_not_limited_to_the_labs(self):
		location = frappe.get_doc({"doctype": "Location", "location_name": f"Store {frappe.generate_hash(length=5)}"}).insert()
		elsewhere = self.make_asset(SimpleNamespace(asset_location=location.name))
		frappe.get_doc("User", READER).add_roles("Accounts User")
		frappe.set_user(READER)
		self.assertIn(elsewhere.name, frappe.get_list("Asset", pluck="name"))

	def test_the_principal_reads_the_labs_of_their_own_college_only(self):
		"""M7: Laboratory, Lab Incident and Lab Safety Inspection are scoped by the Principal's company User Permission."""
		principal, other_principal = "lab-principal@example.com", "lab-other-principal@example.com"
		for email, company in ((principal, COLLEGE), (other_principal, OTHER)):
			make_user(email, [company])
			frappe.get_doc("User", email).add_roles("Principal")
		frappe.set_user(principal)
		self.assertIn(self.lab.name, frappe.get_list("Laboratory", pluck="name"))
		self.assertFalse(frappe.has_permission("Laboratory", "write", self.lab.name))
		frappe.set_user(other_principal)
		self.assertEqual(frappe.get_list("Laboratory", pluck="name"), [])
		self.assertFalse(frappe.has_permission("Laboratory", "read", self.lab.name))

	def test_the_due_report_defaults_to_the_notice_window(self):
		self.make_maintenance(self.make_asset(self.lab), [("Soon", "Calibration", 15), ("Far", "Calibration", 40)])
		self.assertEqual([r["task"] for r in due_report({"laboratory": self.lab.name})[1]], ["Soon"])
		self.assertEqual(len(due_report({"laboratory": self.lab.name, "within_days": 45})[1]), 2)


class TestSafetyInspection(LabCase):
	def make_template(self, company=COLLEGE):
		return frappe.get_doc(
			{
				"doctype": "Safety Checklist Template", "template_name": f"Std {frappe.generate_hash(length=5)}", "company": company,
				"items": [{"check_point": "Fire extinguisher valid", "critical": 1}, {"check_point": "Eye wash clean", "critical": 0}],
			}
		).insert()  # fmt: skip

	def make_inspection(self, template, met=(1, 1)):
		inspection = frappe.get_doc(
			{"doctype": "Lab Safety Inspection", "laboratory": self.lab.name, "inspection_date": today(), "inspector": self.in_charge, "template": template.name}
		)
		inspection.insert()
		for row, ok in zip(inspection.results, met, strict=True):
			row.compliant = ok
		return inspection.save()

	def test_the_results_are_the_templates_points(self):
		inspection = self.make_inspection(self.make_template())
		self.assertEqual([r.check_point for r in inspection.results], ["Fire extinguisher valid", "Eye wash clean"])
		self.assertEqual(inspection.company, COLLEGE)

	def test_outcome_follows_the_points_met(self):
		template = self.make_template()
		self.assertEqual(self.make_inspection(template, (1, 1)).outcome, "Pass")
		self.assertEqual(self.make_inspection(template, (1, 0)).outcome, "Needs Action")
		self.assertEqual(self.make_inspection(template, (0, 1)).outcome, "Fail")

	def test_a_template_of_another_company_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.make_inspection(self.make_template(OTHER))


class TestLabIncident(LabCase):
	def make_incident(self, **values):
		return frappe.get_doc(
			{
				"doctype": "Lab Incident", "laboratory": self.lab.name, "incident_time": add_to_date(now_datetime(), hours=-2),
				"incident_type": "Fire", "severity": "Minor", "reported_by": self.in_charge, "description": "Small flare", **values,
			}
		).insert()  # fmt: skip

	def test_an_incident_takes_the_labs_company(self):
		self.assertEqual(self.make_incident().company, COLLEGE)

	def test_a_future_incident_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.make_incident(incident_time=add_to_date(now_datetime(), days=1))

	def test_it_closes_only_with_the_action_taken(self):
		incident = self.make_incident()
		incident.status = "Closed"
		with self.assertRaises(frappe.ValidationError):
			incident.save()
		incident = frappe.get_doc("Lab Incident", incident.name)
		incident.status = "Closed"
		incident.action_taken = "Extinguisher refilled"
		incident.save()
		self.assertEqual(str(incident.closed_on), today())

	def test_reopening_clears_the_closing_date(self):
		incident = self.make_incident(status="Closed", action_taken="Done")
		incident.status = "Open"
		incident.save()
		self.assertFalse(incident.closed_on)

	def test_the_register_lists_only_the_users_colleges(self):
		other_in_charge = self.make_employee("OtherIC", OTHER)
		other_lab = self.make_lab("Other Lab", company=OTHER, in_charge=other_in_charge)
		mine = self.make_incident()
		frappe.get_doc(
			{
				"doctype": "Lab Incident", "laboratory": other_lab.name, "incident_time": add_to_date(now_datetime(), hours=-1),
				"incident_type": "Injury", "severity": "Major", "reported_by": other_in_charge, "description": "Cut",
			}
		).insert()  # fmt: skip
		frappe.set_user(READER)
		self.assertEqual([row["name"] for row in incident_report({})[1]], [mine.name])
		self.assertEqual(incident_report({"severity": "Major"})[1], [])
