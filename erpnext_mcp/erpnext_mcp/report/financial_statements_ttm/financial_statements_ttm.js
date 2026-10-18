// SPDX-License-Identifier: MIT
// Financial Statements TTM (erpnext_mcp v0.333.0): ERPNext's Profit and Loss, Balance Sheet or
// Cash Flow over the trailing twelve months. The look (bold totals, account names, click through
// to the General Ledger, the tree) is ERPNext's own `erpnext.financial_statements`, borrowed rather
// than copied; nothing on ERPNext's screens is changed.

(function () {
	const base = (window.erpnext && erpnext.financial_statements) || {};
	const CASH_FLOW = "Cash Flow";

	const statement = () =>
		(frappe.query_report && frappe.query_report.get_filter_value("statement")) || "Profit and Loss Statement";

	// The last month-end before today: the default end of the window.
	const last_month_end = () =>
		frappe.datetime.month_end(frappe.datetime.add_months(frappe.datetime.get_today(), -1));

	frappe.query_reports["Financial Statements TTM"] = {
		filters: [
			{
				fieldname: "company",
				label: __("Company"),
				fieldtype: "Link",
				options: "Company",
				default: frappe.defaults.get_user_default("Company"),
				reqd: 1,
			},
			{
				fieldname: "statement",
				label: __("Statement"),
				fieldtype: "Select",
				options: [
					{ value: "Profit and Loss Statement", label: __("Profit and Loss") },
					{ value: "Balance Sheet", label: __("Balance Sheet") },
					{ value: "Cash Flow", label: __("Cash Flow") },
				],
				default: "Profit and Loss Statement",
				reqd: 1,
			},
			{
				fieldname: "as_of",
				label: __("As of (month-end)"),
				fieldtype: "Date",
				default: last_month_end(),
				description: __("The twelve full months ending at the last month-end on or before this date."),
			},
			{
				fieldname: "monthly_columns",
				label: __("Monthly Columns"),
				fieldtype: "Check",
				default: 0,
			},
			{
				fieldname: "compare_prior",
				label: __("Compare Prior TTM"),
				fieldtype: "Check",
				default: 1,
			},
			{
				fieldname: "finance_book",
				label: __("Finance Book"),
				fieldtype: "Link",
				options: "Finance Book",
			},
			{
				fieldname: "cost_center",
				label: __("Cost Center"),
				fieldtype: "Link",
				options: "Cost Center",
				get_query: () => ({ filters: { company: frappe.query_report.get_filter_value("company") } }),
			},
			{
				fieldname: "project",
				label: __("Project"),
				fieldtype: "Link",
				options: "Project",
			},
			{
				fieldname: "presentation_currency",
				label: __("Currency"),
				fieldtype: "Link",
				options: "Currency",
			},
			{
				fieldname: "include_default_book_entries",
				label: __("Include Default FB Entries"),
				fieldtype: "Check",
				default: 1,
			},
		],
		formatter: base.formatter,
		open_general_ledger: base.open_general_ledger,
		tree: true,
		initial_depth: 3,
		// Cash Flow's rows are sections, not accounts — the same switch ERPNext's own Cash Flow makes.
		get name_field() {
			return statement() === CASH_FLOW ? "section" : "account";
		},
		get parent_field() {
			return statement() === CASH_FLOW ? "parent_section" : "parent_account";
		},
		onload: function (report) {
			if (!report.page) return;
			const menu = report.page.add_custom_button_group(__("Financial Statements"));
			[
				["Profit and Loss", "Profit and Loss Statement"],
				["Balance Sheet", "Balance Sheet"],
				["Cash Flow Statement", "Cash Flow"],
			].forEach(([label, name]) => {
				report.page.add_custom_menu_item(menu, __(label), () =>
					frappe.set_route("query-report", name, { company: report.get_values().company })
				);
			});
		},
	};
})();
