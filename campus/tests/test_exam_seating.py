"""M06-02 integration tests: Exam Hall layout on Room, Seating Plan generation from Issued Hall Tickets, exam
attendance (absent) flowing into the Result Run as AB, and the seating chart / hall attendance sheet prints.

Fixture (demo college of engineering): Exam Session over 20, 21 and 23 Nov 2026 with courses SEAT C1..C6, two
Exam Halls (A: 3 x 4 with seat R1C1 blocked = 11 seats; B: 2 x 3 = 6 seats) and 13 students with tickets. The 13th
student's ticket is Withheld and must never be seated.
"""

import frappe

from campus.examinations import api, seating
from campus.examinations.seating_rules import Seat, conflicts
from campus.tests import demo
from campus.tests.test_results import ExamFixture, make_login
from campus.tests.utils import CampusTestCase

COLLEGE = demo.ENGINEERING
COE_USER = "seat-coe@example.com"
DAY1, DAY2, DAY3 = "2026-11-20", "2026-11-21", "2026-11-23"
SCHEDULE = {
	"C1": (DAY1, "FN"),
	"C2": (DAY1, "FN"),
	"C3": (DAY1, "AN"),
	"C4": (DAY2, "FN"),
	"C5": (DAY2, "AN"),
	"C6": (DAY3, "AN"),
}
HALLS = {"Seat Hall A": (3, 4, [(1, 1)]), "Seat Hall B": (2, 3, [])}


def course(key):
	return f"SEAT {key}"


def ticket_courses(i):
	"""Student i's courses: C1 (7) or C2 (6) on day 1 FN, C3 for 5, C4 for all, C5 for even i, C6 for 4."""
	keys = ["C1" if i < 7 else "C2", "C4"]
	keys += ["C3"] * (i < 5) + ["C5"] * (i % 2 == 0) + ["C6"] * (i < 4)
	return keys


def make_room(name, rows, columns, blocked, company=COLLEGE, kind="Exam Hall"):
	return frappe.get_doc(
		{
			"doctype": "Room", "room_name": name, "room_number": name[-1], "seating_capacity": "99", "oc_room_type": kind,
			"oc_company": company, "oc_seat_rows": rows, "oc_seat_columns": columns,
			"oc_blocked_seats": [{"seat_row": r, "seat_column": c} for r, c in blocked],
		}
	).insert()  # fmt: skip


def make_plan(exam_session, exam_date, slot, halls):
	return frappe.get_doc(
		{
			"doctype": "Seating Plan",
			"exam_session": exam_session,
			"exam_date": exam_date,
			"slot": slot,
			"halls": [{"room": h} for h in halls],
		}
	).insert()


class SeatingFixture(CampusTestCase):
	demo_scale = "small"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_login(COE_USER, "COE")
		year = demo.year_name(2026)
		cls.term = frappe.get_doc(
			{"doctype": "Academic Term", "academic_year": year, "term_name": "Seat Test Odd", "term_start_date": "2026-06-01", "term_end_date": "2026-11-30"}
		).insert().name  # fmt: skip
		for key in SCHEDULE:
			demo.get_or_insert("Course", {"course_name": course(key)}, {"oc_company": COLLEGE})
		cls.session = frappe.get_doc(
			{
				"doctype": "Exam Session", "company": COLLEGE, "academic_term": cls.term, "status": "Draft",
				"exam_start": DAY1, "exam_end": DAY3,
				"schedule": [{"course": course(k), "exam_date": d, "slot": s} for k, (d, s) in SCHEDULE.items()],
			}
		).insert().name  # fmt: skip
		cls.rooms = {name: make_room(name, *layout).name for name, layout in HALLS.items()}
		cls.students = frappe.get_all(
			"Student", {"oc_company": COLLEGE}, pluck="name", order_by="name asc", limit=13
		)
		for i, student in enumerate(cls.students):
			cls.make_ticket(student, ticket_courses(i), "Issued" if i < 12 else "Withheld")

	@classmethod
	def make_ticket(cls, student, keys, status):
		registration = frappe.get_doc(
			{
				"doctype": "Exam Registration", "exam_session": cls.session, "student": student, "attendance_status": "Eligible",
				"courses": [{"course": course(k), "kind": "Current"} for k in keys],
			}
		).insert()  # fmt: skip
		frappe.get_doc(
			{
				"doctype": "Hall Ticket", "exam_registration": registration.name, "exam_session": cls.session, "student": student,
				"company": COLLEGE, "academic_term": cls.term, "status": status,
				"courses": [{"course": course(k), "kind": "Current", "exam_date": SCHEDULE[k][0], "slot": SCHEDULE[k][1]} for k in keys],
			}
		).insert()  # fmt: skip

	def setUp(self):
		frappe.db.savepoint("seat_test")
		self.addCleanup(frappe.db.rollback, save_point="seat_test")
		self.addCleanup(frappe.set_user, "Administrator")

	def generated(self, exam_date=DAY1, slot="FN", halls=None):
		plan = make_plan(self.session, exam_date, slot, halls or list(self.rooms.values()))
		seating.generate(plan.name)
		return plan.reload()


class TestExamHall(SeatingFixture):
	def test_capacity_is_the_grid_less_the_blocked_seats(self):
		self.assertEqual(frappe.db.get_value("Room", self.rooms["Seat Hall A"], "seating_capacity"), "11")

	def test_blocked_seats_outside_the_grid_or_twice_are_refused(self):
		with self.assertRaisesRegex(frappe.ValidationError, "outside"):
			make_room("Seat Hall X", 2, 2, [(3, 1)])
		with self.assertRaisesRegex(frappe.ValidationError, "twice"):
			make_room("Seat Hall Y", 2, 2, [(1, 1), (1, 1)])

	def test_seating_plan_hall_auto_fetches_layout_and_capacity(self):
		meta = frappe.get_meta("Seating Plan Hall")
		self.assertEqual(meta.get_field("seat_rows").fetch_from, "room.oc_seat_rows")
		self.assertEqual(meta.get_field("seat_columns").fetch_from, "room.oc_seat_columns")
		self.assertEqual(meta.get_field("capacity").fetch_from, "room.seating_capacity")
		plan = make_plan(self.session, DAY1, "FN", [self.rooms["Seat Hall A"]])
		hall = plan.halls[0]
		self.assertEqual((hall.seat_rows, hall.seat_columns, hall.capacity), (3, 4, 11))


class TestSeatingPlan(SeatingFixture):
	def test_issued_students_are_seated_interleaved_and_never_on_blocked_seats(self):  # AC1
		plan = self.generated()
		withheld = self.students[12]
		self.assertEqual(sorted(s.student for s in plan.seats), sorted(self.students[:12]))
		self.assertNotIn(withheld, [s.student for s in plan.seats])
		placed = {Seat(s.room, s.seat_row, s.seat_column): (s.student, s.course) for s in plan.seats}
		self.assertEqual((conflicts(placed), plan.adjacent_same_course, plan.seated), (0, 0, 12))
		self.assertNotIn(
			(self.rooms["Seat Hall A"], 1, 1), [(s.room, s.seat_row, s.seat_column) for s in plan.seats]
		)
		for hall in plan.halls:
			self.assertLessEqual(hall.seated, hall.capacity)
		self.assertEqual({s.course for s in plan.seats}, {course("C1"), course("C2")})

	def test_a_debarred_student_is_not_seated(self):
		frappe.db.set_value(
			"Exam Registration",
			f"EXR-{self.session}-{self.students[0]}",
			{"debarred": 1, "debarment_reason": "Malpractice"},
		)
		plan = self.generated()
		self.assertEqual(sorted(s.student for s in plan.seats), sorted(self.students[1:12]))

	def test_more_students_than_seats_is_refused_and_nothing_is_seated(self):  # AC1: capacity
		plan = make_plan(self.session, DAY1, "FN", [self.rooms["Seat Hall B"]])  # 6 seats for 12
		with self.assertRaisesRegex(frappe.ValidationError, "12 students"):
			seating.generate(plan.name)
		self.assertEqual(plan.reload().seats, [])

	def test_regenerating_gives_the_same_seats(self):
		plan = self.generated(DAY2)
		first = [(s.room, s.seat_label, s.student) for s in plan.seats]
		seating.generate(plan.name)
		self.assertEqual([(s.room, s.seat_label, s.student) for s in plan.reload().seats], first)

	def test_seats_are_server_owned_but_absent_may_be_marked(self):
		plan = self.generated()
		plan.seats[0].student = self.students[12]
		with self.assertRaisesRegex(frappe.ValidationError, "Generate Seating"):
			plan.save()
		plan.reload()
		plan.seats[0].absent = 1
		plan.save()
		with self.assertRaisesRegex(frappe.ValidationError, "already marked"):
			seating.generate(plan.name)

	def test_halls_must_be_exam_halls_of_the_company_with_a_layout(self):
		classroom = make_room("Seat Class Z", 2, 2, [], kind="Classroom").name
		with self.assertRaisesRegex(frappe.ValidationError, "not an Exam Hall"):
			make_plan(self.session, DAY1, "AN", [classroom])
		other = make_room("Seat Hall Arts", 2, 2, [], company=demo.ARTS).name
		with self.assertRaisesRegex(frappe.ValidationError, "does not belong"):
			make_plan(self.session, DAY1, "AN", [other])

	def test_only_the_coe_generates_and_marks_and_only_by_post(self):
		plan = make_plan(self.session, DAY1, "FN", list(self.rooms.values()))
		frappe.set_user(demo.email("faculty_1"))
		with self.assertRaises(frappe.PermissionError):
			api.generate_seating(plan.name)
		with self.assertRaises(frappe.PermissionError):
			api.mark_exam_absent(plan.name, [self.students[0]])
		frappe.set_user(COE_USER)
		self.assertEqual(api.generate_seating(plan.name), 12)
		self.assertEqual(api.mark_exam_absent(plan.name, frappe.as_json([self.students[0]])), 1)
		for method in (api.generate_seating, api.mark_exam_absent):
			self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[method], ["POST"])

	def test_prints_show_the_seats_and_escape_names(self):
		plan = self.generated()
		frappe.db.set_value(
			"Seating Plan Seat", plan.seats[0].name, "student_name", "<script>alert(1)</script>"
		)
		for print_format in ("Seating Chart", "Hall Attendance Sheet"):
			html = frappe.get_print("Seating Plan", plan.name, print_format)
			self.assertIn(plan.seats[1].student, html, print_format)
			self.assertIn("R1C2", html, print_format)
			self.assertNotIn("<script>alert(1)</script>", html, print_format)
			self.assertIn("&lt;script&gt;", html, print_format)


class TestAttendanceToResult(ExamFixture):
	"""AC3: the T12 result fixture (EXM courses, marks entered for everyone) plus an exam-attendance absent mark."""

	def plan_with(self, student, course_name, absent):
		room = make_room("Exm Hall 1", 2, 2, [], company=self.session.company).name
		plan = frappe.new_doc("Seating Plan")
		plan.update(
			{
				"exam_session": self.session.name,
				"exam_date": "2022-12-01",
				"slot": "FN",
				"halls": [{"room": room}],
			}
		)
		plan.append(
			"seats",
			{
				"room": room,
				"seat_row": 1,
				"seat_column": 1,
				"seat_label": "R1C1",
				"student": student,
				"course": course_name,
				"absent": absent,
			},
		)
		plan.flags.generating = True  # stands in for Generate Seating: the result side is under test here
		return plan.insert()

	def grades(self, student):
		name = frappe.get_value("Student Result", {"student": student, "exam_session": self.session.name})
		return {c.course: (c.grade, c.result) for c in frappe.get_doc("Student Result", name).courses}

	def test_an_absent_mark_makes_the_course_ab_although_marks_were_entered(self):
		plan = self.plan_with(self.s22.name, "EXM TH", 0)
		self.run_session()
		self.assertEqual(self.grades(self.s22.name)["EXM TH"][1], "Pass")
		seating.mark_absent(plan.name, [self.s22.name])
		self.run_session()
		grades = self.grades(self.s22.name)
		self.assertEqual(grades["EXM TH"], ("AB", "AB"))
		self.assertEqual(grades["EXM TL"][1], "Pass")  # other courses are graded from their marks

	def test_an_absent_student_without_a_mark_row_gets_ab_not_an_error(self):
		frappe.db.delete(
			"Mark Sheet Entry Row",
			{
				"student": self.s22.name,
				"parent": frappe.db.get_value(
					"Mark Sheet Entry",
					{"exam_session": self.session.name, "course_offering": self.offerings22["EXM FA"]},
				),
			},
		)
		self.plan_with(self.s22.name, "EXM FA", 1)
		self.run_session()
		self.assertEqual(self.grades(self.s22.name)["EXM FA"], ("AB", "AB"))
