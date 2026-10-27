frappe.listview_settings["Hall Ticket"] = {
	get_empty_state() {
		return {
			title: __("No Hall Tickets found"),
			message: __("Hall tickets are generated in batches from an Exam Session."),
			indicator: "blue",
		};
	},
	onload(listview) {
		if (
			!frappe.user.has_role("COE") &&
			!frappe.user.has_role("System Manager") &&
			frappe.session.user !== "Administrator"
		) {
			return;
		}
		listview.page.add_inner_button(__("Generate Hall Tickets"), () => {
			const dialog = new frappe.ui.Dialog({
				title: __("Generate Hall Tickets"),
				fields: [
					{
						fieldname: "exam_session",
						fieldtype: "Link",
						options: "Exam Session",
						label: __("Exam Session"),
						reqd: 1,
						get_query: () => ({
							filters: {
								status: ["!=", "Published"],
							},
						}),
					},
				],
				primary_action_label: __("Generate"),
				primary_action({ exam_session }) {
					frappe
						.xcall("campus.examinations.api.generate_hall_tickets", {
							exam_session,
						})
						.then(() => {
							dialog.hide();
							frappe.show_alert({
								message: __("Hall ticket generation queued"),
								indicator: "green",
							});
						});
				},
			});
			dialog.show();
		});
	},
};
