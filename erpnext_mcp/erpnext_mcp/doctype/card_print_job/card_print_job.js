// SPDX-License-Identifier: MIT
// Card Print Job — printed by hand (v0.210.0): open the card PDF, print it, then
// Mark printed or Mark failed. Retry and Cancel as before. The server checks the
// status, the role and the company again; hiding the buttons is courtesy, not the control.
frappe.ui.form.on("Card Print Job", {
	refresh(frm) {
		const status = frm.doc.status;
		if (frm.doc.artwork) {
			frm.add_custom_button(__("Open card PDF"), () => window.open(frm.doc.artwork, "_blank"));
		}
		if (["Queued", "Downloaded", "Failed"].includes(status)) {
			frm.add_custom_button(__("Mark printed"), () => {
				frm.call("mark_printed").then(() => frm.reload_doc());
			}).addClass("btn-primary");
		}
		if (["Queued", "Downloaded"].includes(status)) {
			frm.add_custom_button(__("Mark failed"), () => {
				frappe.prompt(
					[{ fieldname: "error", fieldtype: "Small Text", label: __("What went wrong?"), reqd: 1 }],
					(values) => frm.call("mark_failed", { error: values.error }).then(() => frm.reload_doc()),
					__("Mark {0} failed", [frm.doc.name]),
					__("Mark failed")
				);
			});
		}
		if (status === "Failed") {
			frm.add_custom_button(__("Retry"), () => {
				frm.call("retry").then(() => frm.reload_doc());
			});
		}
		if (status === "Printed" && frm.doc.back_pending) {
			frm.add_custom_button(__("Print back"), () => {
				frappe.confirm(__("Flip the card and put it back in the feeder, then continue."), () => {
					frm.call("print_back").then(() => frm.reload_doc());
				});
			});
		}
		if (status === "Queued") {
			frm.add_custom_button(__("Cancel print"), () => {
				frm.call("cancel_job").then(() => frm.reload_doc());
			});
		}
		if (["Queued", "Downloaded"].includes(status)) {
			frm.set_intro(
				__(
					"Open the card PDF and print it from Preview — Paper Size CR80 / ISO 7810, Scale 100%, no Scale to Fit, Auto Rotate off. Page 1 is the front; flip the card for page 2. Then Mark printed."
				),
				"blue"
			);
		}
	},
});
