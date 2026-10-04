# SOP review and approval

**Status: PROPOSED (2026-10-03). Not approved; nothing built.** Queued after the Reference Library (SOPs cite
references from day one). Built on the CCF core and the shared config lifecycle.

## 1. What exists today (reused)

- **Compliance Policy**: `policy_name`, `category` (Spray SOP, Worker Safety, Equipment Sanitation, …),
  `version`, `company`, `policy_owner`, `status` (Draft / Active / Superseded / Retired), `effective_date`,
  `review_due_date`, `supersedes` / `superseded_by`, `attached_document`, `notes`; MCP
  `create_compliance_policy`, `update_compliance_policy`, `supersede_compliance_policy`,
  `get_compliance_policy`, `list_compliance_policies`. No phone routes, no approval.
- **Signing Evidence** + sealed PDFs (`seal_signed_document`, device keys / Face ID): the signature chain.
- Governance Document is **not** used — its categories are corporate legal documents.

**Today, without any build**: the tree-removal SOP PDF can be held as a Draft Compliance Policy (category
Worker Safety, `attached_document`, `version`, `policy_owner` = Tim, review dates) and reviewed in the Desk.
Nothing on OML without Tim's approval.

## 2. Design

- **Statuses**: Draft → **In Review** → **Approved** → Superseded (Retired kept). "Active" is read as Approved
  for existing rows.
- **Approvers are configurable**: a settings table of approvers (user, label such as Owner / Manager,
  required yes/no) and a rule "all required approvers" or "any one". **Default: Tim Polehn is the only
  approver.** His mother can be added later as Owner with no code change. The same list approves Work Timing
  rules.
- **Review**: in the Farm Ops app (PDF viewer, comments, Face ID signature) or the Desk. Approval writes
  Signing Evidence (role `approve`) and a sealed PDF; "Request changes" returns it to Draft with the note.
- **Approval is human-only.** The AI may create, update and submit for review; there is no MCP approve.
- **Links**: a References table to Farm Task Templates, Training Types and Asset Types (and Reference
  Documents, the shared Reference Citation table); review interval.
- **Gating as CCF rules** (no mini-engine): a template citing an SOP whose current version is not Approved →
  `block_start` (template cannot be activated, tasks cannot start); a new version → alert + `notify_supervisor`
  "re-approval needed" and a banner on linked templates; review due → existing clock rule.
- **Workers**: "View SOP" on the task, cached for offline. Reads and an optional **Acknowledge** tap are
  Signing Evidence rows (role `acknowledge`) per person and version — no read-log table. They feed training
  and the audit packet.
- **Lifecycle**: an SOP version is a config kind in the shared lifecycle (draft / stage / preview / publish /
  rollback). Publish = approval, human-only.

## 3. MCP

Through the generic config tools (`draft_config`, `stage_config` = submit for review, `get_config`,
`list_configs`, `diff_config`) plus the existing `supersede_compliance_policy`. No approve tool. Writes off by
default.

## 4. Phone

Approvals list for approvers (PDF, diff vs previous version, comments, approve / request changes); "View SOP"
and Acknowledge for workers; offline cache of the approved version.

## 5. Effort

One server release + one app release.
