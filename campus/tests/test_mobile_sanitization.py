from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase

from campus.admissions.allotment_import import coerce
from campus.campus_platform.sanitize import clean_mobile_number, validate_mobile


class TestMobileSanitization(FrappeTestCase):
	def test_clean_mobile_number_variations(self):
		"""clean_mobile_number drops trailing .0 or .00 and int-casts whole floats, leaving other formats untouched."""
		cases = [
			(6380168782.0, "6380168782"),
			("6380168782.0", "6380168782"),
			("6380168780.0", "6380168780"),
			("6380168782.00", "6380168782"),
			("6380168782", "6380168782"),
			("+91 6380168782", "+91 6380168782"),
			("+916380168782.0", "+916380168782"),
			("9876543210/9123456780", "9876543210/9123456780"),
			("(044) 2345-6789 ext 12", "(044) 2345-6789 ext 12"),
			("(044) 2345-6789 ext 12.0", "(044) 2345-6789 ext 12.0"),
			("01.23.45.67.00", "01.23.45.67.00"),
			("01.23.45.67.0", "01.23.45.67.0"),
			(6380168782.5, "6380168782.5"),
			("6.380168782e+09", "6.380168782e+09"),
			(None, ""),
			("", ""),
		]
		for inp, expected in cases:
			self.assertEqual(clean_mobile_number(inp), expected, f"Failed on input {inp}")

	def test_validate_mobile_doc_event_hook(self):
		"""validate_mobile hook sanitizes mobile fields in memory on doc validation."""
		doc = MagicMock()
		doc.doctype = "Student"
		doc.meta.has_field.return_value = True
		doc.get.return_value = "9876543210.0"
		validate_mobile(doc)
		doc.set.assert_called_once_with("student_mobile_number", "9876543210")

	def test_coerce_cleans_mobile_number(self):
		"""allotment_import coerce cleans float mobile numbers from file imports."""
		with patch("frappe.get_meta") as mock_meta:
			mock_meta.return_value.get_field.return_value.fieldtype = "Data"
			self.assertEqual(coerce("student_mobile_number", 6380168782.0), "6380168782")
			self.assertEqual(coerce("student_mobile_number", "6380168782.0"), "6380168782")
