**Deploy: catch-up v0.230.3 → v0.231.1 — one build, ONE migrate (app 0.38.2)**

For a box on v0.230.3. One image covers v0.230.4, v0.230.5, v0.230.6, v0.231.0 and v0.231.1, and a single
`bench migrate` runs everything those releases need, in order:

1. v0.230.4 — the after-migrate rewrite of every stored asset `qr_url` to `<farmops_public_url>/farmops/api/scan/<name>`.
2. v0.230.5 — four columns on Farm Asset Type, one fetched flag on Asset Register, and the patch
   `asset_type_slope_defaults` (Tractor 15°, Vehicle 20°, Sprayer 12°, Implement 15°).
3. v0.231.0 — six Contact columns (business cards) and two ERPNext MCP Settings switches, both off.

v0.230.6 and v0.231.1 are code only. Every step is idempotent: a second migrate changes nothing. **Test on
umbrel.local first; OML is Tim's call.** Details per release are in the files named below.

**1. Build (Tim)**

Push erpnext_mcp main (`__version__ = "0.231.1"`), build the fafo-erpnext image. Archive and ship FarmOps 0.38.2
(build 27) from fafo_ios main.

**2. Server, over SSH to the box**

```zsh
sudo docker exec -u frappe -w /home/frappe/frappe-bench fafo-erpnext_server_1 bench --site frontend migrate 2>&1 | grep -i "slope limits\|asset tag url\|business-card"
sudo docker restart fafo-erpnext_server_1
sudo docker exec fafo-erpnext_server_1 grep __version__ /home/frappe/frappe-bench/apps/erpnext_mcp/erpnext_mcp/__init__.py
```

Expect:

1. "slope limits are now data on Farm Asset Type — Tractor, Vehicle, Sprayer, Implement …" (first migrate only).
2. "N of M asset tag URL(s) now point at https://…/farmops/api/scan/…" on a box whose tags still pointed at
   `/erpnext` — on OML that includes TC-TRAKHOE-1 and about 42 others. Nothing on a box that was already right.
3. No "business-card fields were not added" and no "asset tag URLs were not refreshed" line.
4. `__version__ = "0.231.1"`.

**3. Checks after the migrate**

1. Tag URLs (`v0.230.4_tag_urls.md`): `get_asset_detail` TC-TRAKHOE-1 → `qr_url` is
   `<farmops_public_url>/farmops/api/scan/TC-TRAKHOE-1`; `generate_asset_qr` returns the same; with Tailscale off,
   that URL opens the plain scan page in Safari. A second migrate prints no tag URL line.
2. Asset types (`v0.230.5_asset_type_defaults.md`): `get_asset_type` Tractor → `has_slope_limit: true`, 15°.
3. Business cards (`v0.231.0_business_cards.md`): Desk → Contact shows Captured From, Met On, Met At, Website,
   Captured By; Settings has "Search Contacts" and "Save Contact", both unticked.
4. Phone 0.38.2: the checks in `v0.230.6_polish.md` §4, `v0.231.0_business_cards.md` §4 and
   `v0.231.1_offline.md` §3.

**4. Then, on OML when Tim chooses (data, no code)**

1. Card CPJ-2026-00004 was rendered with the old `/erpnext` address: download a fresh PDF with `download_card_pdf`
   (nothing has been printed, so no reprint reason is needed) and mark CPJ-2026-00004 failed ("rendered with the old
   /erpnext address"). Do not print the old one.
2. Mini Excavator and TC-TRAKHOE-1: `v0.230.5_asset_type_defaults.md` §4 (the track hoe stays typed Tractor at 10°
   until Tim says otherwise).

**5. Rollback**

All columns and switches are additive; rewritten tag URLs stay correct under an older image. See each release's
own file.
