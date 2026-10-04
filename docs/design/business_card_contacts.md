# Business card capture → Contact

**Status: BUILT — erpnext_mcp v0.231.0 + app 0.38.0 (2026-10-04; Tim approved building it).** From Tell the Farm AFB-2026-00031 (Tim, Receipt
Capture, app 0.36.0): "This is a business card I thought we had a way of building that in via receipts." Today
nothing is filed: the receipt classifier has no business-card kind. Queued after the current polish releases.
Principle: collect once, use everywhere — the card becomes the ERPNext **Contact** every other screen already
reads.

## 1. What exists and is reused

| Piece | Where | Reused for |
|---|---|---|
| Camera + on-device OCR (Apple Vision) | `ReceiptTextScanner` | reading the card, front and back |
| On-device structuring (FoundationModels) | `ReceiptIntelligence` | name / title / company split; Apple-only rule holds |
| Server classifier with scored signals | `tools/receipts.classify_receipt` (kinds: expense, scale_ticket, settlement, bill, reimbursement) | a new `business_card` kind |
| Merchant resolver (name, domain, phone; placeholder phones ignored since v0.229.1) | `tools/receipts` | matching the card to a Supplier / Customer / Company |
| Queued offline writes, request IDs, resumable photo upload | `SyncManager`, `offline_create`, `ChunkUploader` | offline capture |
| ERPNext **Contact** (+ Contact Email, Contact Phone, Dynamic Link) and **Address** (core) | Frappe | the record — no new doctype |
| office@ triage already looks senders up through Contact Email | `office_mail.py` | downstream, free once contacts exist |

## 2. Phone

- **Entry:** Receipt Capture recognises a card (classifier says `business_card`) and offers "This looks like a
  business card — save as a contact?"; plus a direct **Scan business card** action in the same menu.
- **Reading:** Vision OCR on both sides; Apple's data detectors pull phones, emails, links and the address;
  FoundationModels splits the person's name, title and company. Nothing leaves the phone to be read.
- **Contact form, pre-filled, every field editable:** first / last name, title, company, phones (each with a
  type), emails, website, address, notes ("met at Hort Expo, sells nozzles"), where met (place, defaulted from
  GPS to a nearby block or "Other"), date met (today). Front and back photos attached.
- **Match before save:** the phone shows what the server will do — "Looks like **Coastal Farm Stores**
  (supplier)" or "New company" — and any existing contact with the same email or phone ("Already have Jane
  Doe — update her instead?").
- **Offline:** the card is a draft on disk (like `AssetDraft`); Save queues it with a request ID and the photos;
  a resend is the same contact. "Saved on this phone" until it lands.
- EN/ES labels; 44 pt targets; Dynamic Type — the polish rules.

## 3. Server

New mobile route `save_business_card` and MCP tools (§5), one code path.

1. **Duplicate check first:** an existing Contact with the same email, or the same phone (placeholder numbers
   ignored), is a **merge proposal**, never a second contact. The phone shows the differences; the person picks
   "Update Jane Doe" or "Save as new".
2. **Company match** through the merchant resolver (name, website domain, phone): Supplier, Customer, or
   Company. A confident match links the Contact (Dynamic Link) and the Address; a weak one is offered, not
   applied; no match offers "Create supplier" / "Create customer" / "Leave unlinked" — never created silently.
3. **Writes:** Contact (name, designation, company_name, emails, phones, links, image = front photo), Address
   (linked to the Contact and the party), the photos as private Files on the Contact, a Comment with the notes.
4. **Provenance on the Contact:** `source = Business card`, `met_on`, `met_at`, `captured_by`, `client_request_id`
   (custom fields installed by `install_compliance_fields`-style seeding; the only schema change).
5. Idempotent on `client_request_id`; device time kept beside receive time (the Mill Creek rules).

## 4. Downstream (mostly free once the Contact exists)

- **Supplier / Customer records:** ERPNext already lists linked Contacts on the party.
- **office@ triage and reply drafts:** sender lookup by Contact Email already exists; a draft can greet the
  person by name and cite where they met.
- **Purchasing:** the Supplier's contacts appear on Purchase Orders and RFQs (core).
- **Receipts:** a later receipt from the same domain or phone resolves to the same Supplier.

## 5. MCP

- `search_contacts` (read, on): by name, company, email, phone, met-at, date range; returns links and source.
- `save_contact` (write, **off by default**): create or update, same duplicate and match rules as the phone.
  Merges are explicit (`merge_into`).
- No new config kind needed; if contact capture rules ever become configurable they go through the shared config
  lifecycle (`config_lifecycle_and_tool_consolidation.md`).

## 6. Privacy

Contacts and their photos are visible to **Farm Manager, Owner and Bookkeeper** (plus System Manager): a DocPerm
on Contact for those roles, and the phone route refuses everyone else. Photos are private Files. A worker without
those roles does not see the Scan business card action.

## 7. Effort

One server release (route, two tools, custom fields, classifier kind) and one app release (classifier switch,
contact form, offline draft).

## 8. Open decisions for Tim

1. Entry point: both (classifier switch in Receipt Capture **and** a direct action) — or the direct action only?
2. Unmatched company: offer "Create supplier / customer / leave unlinked" (recommended) — or always leave
   unlinked?
3. "Where met": GPS-suggested place, editable (recommended) — or free text only?
4. Should Crew Leaders be able to capture (but not browse) cards, e.g. a vendor at the shop?
