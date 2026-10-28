"""Arrange Assessment Result fields so Row 1 is Assessment Plan & Program, Row 2 is Student Group & Student."""

from campus.examinations.setup import setup_assessment_result_layout


def execute():
	setup_assessment_result_layout()
