# SPDX-License-Identifier: MIT
"""What kinds of thing this farm keeps in its asset register. v0.162.0.

WHY THIS IS A REGISTER AND NOT A SELECT. Until this release the answer to "what
asset types are there" was given in FOUR places, and three of them disagreed:

  * `Asset Register.asset_type`'s Select options — thirteen
  * `tools/asset_tags.ASSET_TYPES` — twelve, missing Wind Machine
  * `asset_register.AssetRegister.ASSET_TYPES` — ten, missing Wind Machine,
    Implement and Vehicle
  * `farm_overview.ASSET_ICONS` — four, plus a fallback

So `register_asset` accepted a Wind Machine and the controller's own tuple did
not name one; a farm that wanted a Fuel Tank waited for a release. This doctype
is the single answer, and the other four now read it.

THE DOCNAME IS THE TYPE NAME — `field:type_name` — and that is the load-bearing
decision, exactly as it is for `Training Type`. A `FAT-00007` autoname would have
meant rewriting `asset_type` on every asset on the site inside a migration; with
the name as the value, a Select holding 'Irrigation Valve' becomes a Link holding
'Irrigation Valve' and not one asset row is touched. See
`patches/migrate_asset_types.py`, which creates the masters those values were
already pointing at in spirit.

RETIREMENT IS A FLAG AND DELETION IS NOT THE WAY. `enabled` takes a type off
every picker and out of `list_asset_types` while leaving the assets that carry it
exactly as they are. Deleting the record instead would leave those assets
pointing at nothing — the same argument `asset_register.py` makes about retiring
an asset rather than removing it.

A NEW TYPE IS NEVER CREATED BY A TOOL, and that is the difference between this
register and `Training Type`. `training.ensure_type` creates a curriculum from
free text on purpose, because curricula are an open vocabulary — a regulator
renames one every few years and an operation invents its own. Asset types are
CLOSED here, because `_STATE_DEFINITIONS`, `ASSET_TYPE_SKILL_MAP` and
`asset_actions` are all keyed on them: a typo that silently created 'Tracter'
would produce an asset with no state machine, no skill and no action menu, and
nothing would say so. `register_asset` refuses an unknown type and lists what
this site has, which is the same call `training.py` makes for a compliance
regime.
"""

import frappe
from frappe import _
from frappe.model.document import Document


class FarmAssetType(Document):
	def validate(self):
		self.type_name = " ".join(str(self.type_name or "").split()).strip()
		if not self.type_name:
			frappe.throw(_("Type Name is required — it is what a picker shows and what every asset stores."))

		# v0.163.1. EDITING `type_name` ON AN EXISTING RECORD IS REFUSED, because
		# `field:` autoname only names a document at INSERT. A later edit moves
		# the column and leaves the docname alone — so the record reads
		# 'Fuel Depot' while its docname, and `asset_type` on every asset
		# carrying it, is still 'Storage'. A picker built from `type_name` then
		# offers a value the server will not accept, and the worker who picks it
		# gets a link error naming a type they can see on screen.
		#
		# PROVEN, NOT ASSUMED: `doc.type_name = "Fuel Depot"; doc.save()` on this
		# doctype leaves `name` as 'Storage'. Frappe does not put `set_only_once`
		# on an autoname field for you.
		#
		# REFUSED RATHER THAN AUTO-RENAMED. A rename inside `validate` is a save
		# inside a save, and `update_asset_type` already does it properly —
		# through `frappe.rename_doc`, which repoints every asset.
		if not self.is_new() and self.type_name != self.name:
			frappe.throw(
				_(
					"Changing the name of an asset type is a RENAME, not a field edit: the docname "
					"is what every asset stores, and editing this column alone would leave {0} "
					"assets pointing at {1!r} while this record calls itself {2!r}. Use "
					"update_asset_type(name={1!r}, type_name={2!r}), or the Desk's own Rename — "
					"both move the docname and repoint every asset."
				).format(
					frappe.db.count("Asset Register", {"asset_type": self.name}),
					self.name,
					self.type_name,
				)
			)

		# THE ICON IS ONE CHARACTER ON THE MAP AND MAY BE MORE ON A HANDSET.
		# `farm_overview` draws a lettered badge and takes the first character;
		# iOS may read the whole string as a symbol name. Nothing is truncated
		# here — a client that wants one character takes one.
		self.icon = str(self.icon or "").strip()

		# NOT `int(self.display_order or 0)`. A Frappe Int column is
		# NOT NULL DEFAULT 0, so an order nobody set reads back 0 and that is
		# the same value as one somebody deliberately put first. Both sort
		# first, which is correct and is why the tie-break on `type_name`
		# matters: it keeps a register nobody has ordered in alphabetical
		# order rather than in creation order.
		if self.display_order and int(self.display_order) < 0:
			frappe.throw(_("Display Order cannot be negative."))

	def on_trash(self):
		"""Refuse to delete a type any asset still carries.

		THE LINK IS NOT `frappe.LinkExistsError`-PROTECTED THE WAY IT LOOKS.
		Frappe's own link check runs on submitted documents and on doctypes it
		knows link here; this is the explicit version, and it names the count
		rather than the first row — "17 assets" is the sentence somebody needs
		to decide whether to retire the type instead.
		"""
		count = frappe.db.count("Asset Register", {"asset_type": self.name})
		if count:
			frappe.throw(
				_(
					"{0} asset(s) are registered as {1}. Deleting the type would leave every one "
					"of them pointing at nothing. Untick Enabled instead — that takes it off every "
					"picker and leaves those assets exactly as they are."
				).format(count, self.name)
			)
