# The customer / first-handler portal — plan (2026-10-10)

Tim's design change: the "Orchard View portal" (v0.276.0, `Packer Share`) becomes a generic **customer / first
handler portal**. Any ERPNext Customer that buys, packs or first-handles our fruit (OVF, Monson, PFI, a future buyer)
can have one; Orchard View is just the first configured instance. Built as a kind on the share-link engine
(pattern #2), not as OVF-specific code.

## What already exists (v0.276.0, v0.285.0) and carries over
- **Sections, toggleable per portal:** sprays (product, EPA reg no., rate, date, REI/PHI, MRL status by destination
  market), IPM observations, yield / harvest-window projections, clear-to-harvest.
- **Blocks by ticker:** only the committed blocks are ever read.
- **Access:** named, revocable credentials on the share-link engine (hash only, shown once, expiry); every view,
  download and feed pull logged (Share Link View).
- **Rate limiting:** 60 a minute per address on the public page and the feed, with an alert on repeated bad tokens
  (`farmops_api/app.py`, `api/guard.py`).
- **Downloads:** CSV, XLSX, a PDF pack per block or season (packer spray record, GlobalG.A.P. fields) and a JSON feed
  pulled with a Bearer token.
- **Safety:** an allow-list per section; never costs, prices, labour, people or HR; tests read every answer for what
  must not be there.

## What changes

### v0.313.0 — generic, customer-scoped
- **Doctypes:** `Packer Share` → `Customer Portal`, `Packer Share Block` → `Customer Portal Block`. A patch renames
  them where an older version installed them; neither server has, both are on v0.269.0.
- **The customer:** `customer` is a required Link to Customer, plus `customer_role` (Buyer / Packer / First Handler).
  The **issuing company is the grower entity** (Constancy once live).
- **Share Link kind:** `Packer` → `Customer Portal` (old rows renamed by the patch).
- **Switch:** `customer_portal_enabled` per company; the old `packer_portal_enabled` is still honoured.
- **Routes:** `/portal/<token>` and `/portal-feed`; `/packer/…` keep working.
- **Tools** (generic names):

  | New | Old name, kept as an alias (still callable, logged as superseded, retired only after 30 days of no use and Tim's OK) |
  |---|---|
  | `create_customer_portal` | `create_packer_share` |
  | `issue_portal_credential` | `issue_packer_credential` |
  | `revoke_portal_access` | `revoke_packer_access` |
  | `get_customer_portal` | `get_packer_share` |
  | `get_customer_portal_pack` | `get_packer_pack` |
  | `list_portal_access_log` | `list_packer_access_log` |

  The `share_link` generic tool gains the `customer_portal` kind.

### v0.314.0 — lots, deliveries and standard formats
- **Scope by what is committed or delivered to the customer:**
  - **Blocks:** the portal's committed blocks, by ticker.
  - **Deliveries:** Scale Tickets whose `customer` is this customer, or whose `settlement` is this customer's
    Settlement Statement (and so its Revenue Contract).
  - **Lots:** the Traceability Lot Codes those tickets resolve to (block, variety, day), plus any lot whose Shipping
    CTE names this customer as receiver.
  - A customer never sees another customer's ticket or lot, and never a block or lot outside that set.
- **New sections:**
  - `deliveries`: scale tickets — date, block ticker, variety, grade, net weight, destination; no price.
  - `lots`: delivered lots with their FSMA 204 KDEs/CTEs as first receiver — traceability lot code, product
    (commodity / variety), quantity and unit, harvest date and location (block ticker), ship date, ship-from and
    ship-to, carrier, and the CTE chain (growing, harvesting / creating, shipping). Never the employee who did it.
- **Standard formats:**
  - FSMA 204 sortable spreadsheet (CSV / XLSX, one row per CTE with its KDEs — the 24-hour request format).
  - The packer spray record and GlobalG.A.P. CB spray fields in CSV / XLSX / PDF.
  - The JSON feed carries every enabled section.

## Not changing
No costs, prices, labour, people or HR, ever. Every view and download is logged. Everything stays OFF per company
until switched on.

## Queue after this
The retirement of superseded tool names (30 days of no use plus Tim's OK) is the only other open item. The
consolidation phases, shop queue, live budget, the v0.276.0 portal and daily pay all shipped (v0.273.0 – v0.290.0).
