// M17-01 Desk routing for DocTypes whose slug matches a Workspace slug (e.g. Hostel, Communication).
frappe.provide("campus.navigation");

campus.navigation.collides_with_workspace = function (slug) {
	return Boolean(slug && frappe.workspaces?.[slug] && frappe.router?.routes?.[slug]);
};

if (frappe.router) {
	const _begins_with_shell = frappe.router.begins_with_shell;
	frappe.router.begins_with_shell = function (route) {
		if (route?.length > 1 && campus.navigation.collides_with_workspace(route[0])) {
			return false;
		}
		return _begins_with_shell.call(this, route);
	};

	const _convert_to_standard_route = frappe.router.convert_to_standard_route;
	frappe.router.convert_to_standard_route = async function (route) {
		if (route?.length > 1 && campus.navigation.collides_with_workspace(route[0])) {
			return await this.set_doctype_route(route);
		}
		return await _convert_to_standard_route.call(this, route);
	};

	const _convert_from_standard_route = frappe.router.convert_from_standard_route;
	frappe.router.convert_from_standard_route = function (route) {
		const new_route = _convert_from_standard_route.call(this, route);
		if (
			route?.[0]?.toLowerCase() === "list" &&
			new_route?.length === 1 &&
			campus.navigation.collides_with_workspace(new_route[0])
		) {
			return [new_route[0], "view", "list"];
		}
		return new_route;
	};
}

if (frappe.utils?.generate_route) {
	const _generate_route = frappe.utils.generate_route;
	frappe.utils.generate_route = function (item) {
		if (
			item?.type?.toLowerCase() === "doctype" &&
			item.name &&
			!item.route &&
			!item.link &&
			!item.doc_view &&
			!frappe.model?.is_single(item.name) &&
			campus.navigation.collides_with_workspace(frappe.router?.slug(item.name))
		) {
			return _generate_route.call(this, { ...item, doc_view: "List" });
		}
		return _generate_route.call(this, item);
	};
}

if (frappe.ui?.form?.get_breadcrumbs) {
	const _get_breadcrumbs = frappe.ui.form.get_breadcrumbs;
	frappe.ui.form.get_breadcrumbs = function (frm) {
		const crumbs = _get_breadcrumbs.call(this, frm);
		const slug = frm?.doctype && frappe.router?.slug(frm.doctype);
		if (
			slug &&
			!frm.meta?.issingle &&
			!frm.meta?.is_tree &&
			campus.navigation.collides_with_workspace(slug)
		) {
			const base = `/desk/${slug}`;
			for (const crumb of crumbs || []) {
				if (crumb.href === base) {
					crumb.href = `${base}/view/list`;
				}
			}
		}
		return crumbs;
	};
}
