# MCP-native gap audit — October 2026

Tim, 2026-10-11: *"What other things do we have to develop to make this MCP native?"* This is a read-only audit of
erpnext_mcp, the FarmOps phone API, Bank Bridge, the backup kit and the deploy steps. "MCP native" means three
things:
- anything a person or the phone can do, an MCP client can do, or propose for a person to approve;
- an MCP client can verify the result without a shell;
- every boundary that remains is deliberate and written down.

Tim approved building items B1–B5 the same day. The release that closed each one is noted beside it.

## A. Human-only boundaries to keep (deliberate)

| Boundary | Why | MCP's part |
|---|---|---|
| Publishing payroll, garnishment, withholding and IPM config | A person answers for pay and pesticide decisions | `config` drafts and previews; publishing is done in the Desk |
| Approving an OAuth client | The approval is the security step | `list_access_inventory` shows pending clients |
| Ticking Require User Context and unticking Legacy Static MCP Token | It locks the door; Tim decides when | `static_token_callers` shows when it is safe (v0.317.0) |
| Signing forms (I-9, W-4, OR-W-4, sign-in sheets) | Signer authentication (26 CFR 31.3402(f)(5)-1(c)) | `collect_form_signature` evidence reads |
| Moving money: NACHA, payments, JE posting gate, Bank Bridge kill switches | A person releases funds | Drafts only; posting tools default OFF |
| Plaid Link and bank-company mapping (Bank Bridge UI) | Bank credentials never pass through an AI | `get_account_topology`, read only |
| Funnel / Tailscale configuration | Network exposure | `get_tailscale_funnel_config`, read only |
| Installing and configuring the backup kit | Runs on the host | `get_backup_status` shows the kit version and whether it is out of date (v0.318.0) |
| Weather Settings writes | Keys, by design | Read only |
| Printing | "Don't print anything without asking" | Print jobs are queued for a person |
| The worker's own direct deposit, bank verification, daily pay, SOP review, training sign-off and check-in | Their own device, signature or Face ID is the proof | HR reads: `list_direct_deposit_changes`, `list_work_notices`, `get_quiz_results` |

## B. Gaps and how each closed

| # | Gap | Pattern | Release |
|---|---|---|---|
| B1 | The cherry degree-day stage table lived only in a Settings text field | Config lifecycle: `crop_phenology` stage tables, per variety, `to_calibrate`; a seeded DRAFT | v0.316.0 |
| B2 | Clients sign in with a static token; no sign-in method in the log; MCP System User never created | `auth_method` on the Action Log; `static_token_callers`; Desk button to create the user from the plan; safe migration steps | v0.317.0 |
| B3 | Deploy and migrate landing could only be checked over SSH | `get_deploy_status`, `run_post_deploy_check`; backup kit `kit_outdated` | v0.318.0 |
| B4a | Phone-only calls with no MCP route: barcode find and add, rehire, title→asset, shift break schedule, open crew tasks, late training certificate | Arguments on the tools that own each register (no new tools) | v0.319.0 |
| B4b | 42 registers had a create tool but no get, list or update | `record` falls back to `query_doctype` / `update_document` | v0.319.0 |
| B4c | Phone self-service still without an MCP route: direct-deposit self-service, bank verification, daily-pay self-service, SOP review, work-notice answers, evidence listing, field card, IPM threshold status, compliance inbox, training check-in and sign-off, labels, contact "where met" | Reads as arguments on owning tools (inbox, field card, threshold status, where met, task SOPs, item label); the worker's own bank, pay, SOP-review, sign-off and check-in calls stay on the phone (§A) | v0.320.0 |
| B5 | Bank Bridge rule company scoping | ERPNext's Bank Categorization Rule already has a required `company` and filters by it: **no ERPNext change**. Bank Bridge's own `create_rule` schema omits `applies_to_company`; that is for the Bank Bridge session | — |

Already covered, so no gap:
- **Field varieties, `rename_field`, `set_field_boundary`:** MCP-editable, confirmed.
- **`create_farm_location`:** the four create tools.
- **`attach_employee_document`:** `commit_staged_file` + `attach_file_to_document` (private).
- **`expense_account_map`:** `search_accounts`.
- **`submit_app_feedback`:** the phone's own channel; MCP reads and resolves it.

Scheduled clients and their sign-in:
- The 3:30 brief and the receipts task use the static `X-MCP-Token` through the Claude Desktop config. They move
  in step 3 of the v0.317.0 deploy file.
- Bank Bridge uses an ERPNext API key and is unaffected.
- The backup kit holds no MCP token.

## C. Alias retirement

Superseded and renamed tools (for example the packer-named portal tools after v0.313.0) stay until
`list_access_inventory` / the Action Log shows **30 days of zero use** on every server. They are then removed in a
release that names them. `superseded.covering()` already hides them from `tools/list` when
`hide_superseded_tools` is ticked.
