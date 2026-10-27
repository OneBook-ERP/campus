// M05-07 Student list: the mentor allocation tool for HODs and deans.
frappe.listview_settings["Student"] = frappe.listview_settings["Student"] || {};

// M05-09 (BUG-10): show the lifecycle status instead of the generic Enabled/Disabled. frappe.get_indicator
// also uses this for the form's title badge. Display only: the status itself is set by the lifecycle service.
{
	const LIFECYCLE_COLOURS = {
		Admitted: "blue",
		Joined: "cyan",
		Active: "green",
		"On Break": "orange",
		Discontinued: "red",
		Withdrawn: "gray",
		Cancelled: "red",
		Readmitted: "purple",
		"Study Completed": "blue",
		"Award Eligible": "cyan",
		Graduated: "green",
		Alumni: "gray",
	};
	const student_settings = frappe.listview_settings["Student"];
	student_settings.add_fields = [
		...new Set([...(student_settings.add_fields || []), "oc_academic_status", "enabled"]),
	];
	const indicator_before = student_settings.get_indicator;
	student_settings.get_indicator = function (doc) {
		const status = doc.oc_academic_status;
		if (status) {
			return [
				__(status),
				LIFECYCLE_COLOURS[status] || "gray",
				`oc_academic_status,=,${status}`,
			];
		}
		return indicator_before ? indicator_before(doc) : undefined;
	};
}

const onload_before = frappe.listview_settings["Student"].onload;
frappe.listview_settings["Student"].onload = function (listview) {
	if (onload_before) onload_before(listview);
	listview.page.add_menu_item(__("Allocate Mentors"), () => {
		frappe.prompt(
			[
				{
					fieldname: "student_group",
					fieldtype: "Link",
					options: "Student Group",
					label: __("Student Group"),
				},
				{
					fieldname: "program",
					fieldtype: "Link",
					options: "Program",
					label: __("Program"),
					depends_on: "eval:!doc.student_group",
				},
				{
					fieldname: "batch",
					fieldtype: "Link",
					options: "Academic Year",
					label: __("Batch"),
					depends_on: "eval:!doc.student_group",
				},
				{
					fieldname: "per_mentor",
					fieldtype: "Int",
					label: __("Maximum students per mentor"),
				},
			],
			(values) => {
				frappe.call({
					method: "campus.student_services.api.allocate_mentors",
					args: values,
					freeze: true,
					callback: (r) => {
						const lines = Object.entries(r.message || {}).map(
							([m, s]) => `${m}: ${s.length}`
						);
						frappe.msgprint(
							__("Allocated to mentors (all active Mentor position holders)") +
								"<br>" +
								lines.join("<br>")
						);
						listview.refresh();
					},
				});
			},
			__("Allocate Mentors"),
			__("Allocate")
		);
	});
};
