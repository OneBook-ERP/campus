"""Re-export Hostel Bed Allocation service functions for direct module imports."""

from campus.hostel.doctype.hostel_bed_allocation.hostel_bed_allocation import (
	HostelBedAllocation,
	allocate_bed,
	vacate_for_student,
)

__all__ = [
	"HostelBedAllocation",
	"allocate_bed",
	"vacate_for_student",
]
