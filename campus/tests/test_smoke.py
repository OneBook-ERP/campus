"""P1-T01 smoke test: every module package imports and app hooks load."""

import importlib

import frappe

from campus.tests.utils import CampusTestCase

REQUIRED_APPS = ["erpnext", "hrms", "education", "india_compliance", "frappe_whatsapp"]


def module_packages():
	with open(frappe.get_app_path("campus", "modules.txt")) as f:
		names = [line.strip() for line in f if line.strip()]
	return {name: "campus." + frappe.scrub(name) for name in names}


class TestSmoke(CampusTestCase):
	def test_modules_txt_has_eighteen_modules(self):
		"""The 17 areas plus Campus Approvals (approvals moved out of Campus Platform for the 16.53 Dock/Sidebar)."""
		self.assertEqual(len(module_packages()), 18)

	def test_every_module_package_imports(self):
		for name, dotted in module_packages().items():
			with self.subTest(module=name):
				self.assertIsNotNone(importlib.import_module(dotted))

	def test_integrations_package_imports(self):
		self.assertIsNotNone(importlib.import_module("campus.integrations"))

	def test_modules_registered_in_site(self):
		for name in module_packages():
			with self.subTest(module=name):
				self.assertTrue(frappe.db.exists("Module Def", name))

	def test_hooks_load(self):
		self.assertEqual(frappe.get_hooks("app_title", app_name="campus"), ["OneBook Campus"])
		self.assertEqual(frappe.get_hooks("required_apps", app_name="campus"), REQUIRED_APPS)
