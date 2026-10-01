// SPDX-License-Identifier: MIT
// Card Print Job — Retry and Cancel. v0.208.0. The server checks the status and
// the role again; hiding the buttons is courtesy, not the control.
frappe.ui.form.on("Card Print Job", {
	refresh(frm) {
		if (frm.doc.status === "Failed") {
			frm.add_custom_button(__("Retry"), () => {
				frm.call("retry").then(() => frm.reload_doc());
			});
		}
		if (frm.doc.status === "Printed" && frm.doc.back_pending) {
			frm.add_custom_button(__("Print back"), () => {
				frappe.confirm(__("Flip the card and put it back in the feeder, then continue."), () => {
					frm.call("print_back").then(() => frm.reload_doc());
				});
			});
		}
		if (frm.doc.artwork) {
			frm.add_custom_button(__("Open card PDF"), () => window.open(frm.doc.artwork, "_blank"));
		}
		if (frm.doc.status === "Queued") {
			frm.add_custom_button(__("Cancel print"), () => {
				frm.call("cancel_job").then(() => frm.reload_doc());
			});
		}
	},
});
