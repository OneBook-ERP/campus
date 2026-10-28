frappe.ui.form.on("Assessment Result", {
	setup(frm) {
		frm.set_query("student", () => {
			if (!frm.doc.student_group && !frm.doc.program) {
				return {
					filters: {
						name: ["in", [""]],
					},
				};
			}
			return {
				query: "campus.examinations.api.get_students_for_assessment_result",
				filters: {
					student_group: frm.doc.student_group,
					program: frm.doc.program,
				},
			};
		});
	},

	student_group(frm) {
		if (frm.doc.student && frm.doc.student_group) {
			frappe.db.get_value(
				"Student Group Student",
				{ parent: frm.doc.student_group, student: frm.doc.student, active: 1 },
				"name",
				(r) => {
					if (!r || !r.name) {
						frm.set_value("student", null);
					}
				}
			);
		} else if (!frm.doc.student_group && !frm.doc.program) {
			frm.set_value("student", null);
		}
	},

	assessment_plan(frm) {
		if (!frm.doc.assessment_plan) {
			frm.set_value("student", null);
		}
	},
});
