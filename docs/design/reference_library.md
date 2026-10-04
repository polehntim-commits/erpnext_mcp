# Reference Library

**Status: APPROVED in principle (Tim, 2026-10-03) — build when its turn comes, before SOP approval.**
Tim's decisions: (1) OCR Mac-side with Apple Vision — text PDFs are indexed on the server at once; scanned PDFs
show **OCR pending** until the Mac job posts their text; (2) tag values editable in settings; (3) build before
SOP approval.

## 1. Purpose

A searchable library of PDFs — OSU/WSU extension guides, research papers, labels, manuals, regulations,
internal documents — to look up and cite when drafting SOPs and rules. First load: the OSU/WSU cherry pruning
and firmness papers from `~/Documents/Farm-Training/Cherry-Pruning/` (folder not yet created on 2026-10-03),
to umbrel.local only.

## 2. Reused

Private File attachments and their access checks; staged chunked uploads (upload links later); `pypdf` (already
installed for the I-9/W-4 work) for the text layer; document-intake pattern; Regulation Feed (a Regulation
reference can link to its feed). Governance Document is not reused.

## 3. Data — one doctype, two child tables

- **Reference Document**: title, authors, publisher (OSU, WSU, Cat, EPA…), year, publication number, source URL
  (required except Internal), type (Extension Guide, Research Paper, Manual, Label, Regulation, Internal),
  tags (crop, variety, topic, region — short fields checked against vocabularies in settings), the PDF
  (private), summary, key takeaways, added on/by, superseded by, SHA-256 (no duplicate loads), **text status**
  (Text layer / OCR pending / OCR'd).
- **Reference Page** (child): page number + text, with a full-text index (with title, summary, takeaways).
- **Reference Citation** (child, shared): reference, page(s), note — added to Compliance Policy (SOPs), Farm
  Task Template, Training Type, Compliance Rule, Inspection Template. "Cited by" is a query over this table.

## 4. Text and OCR

On add, the server reads each page's text layer. Pages with no text → **OCR pending**. A Mac-side job (Apple
Vision) lists pending documents, OCRs them, and posts page text back through `update_reference`. Status shows in
the Desk, search results and the phone.

## 5. MCP

Read (on): `search_references(query, filters)` → ranked hits with id, page, snippet; `get_reference` (metadata,
summary, takeaways, requested or matching pages); `list_references`. Write (off by default): `add_reference`
(staged upload; text extracted on add), `update_reference` (metadata, tags, summary, OCR page text),
`supersede_reference`. SOP and rule drafting searches the library first and cites by id and page.

## 6. Phone

Read-only Library: search, filter by topic/crop, PDFKit viewer opening at the matching page; star = favorite,
kept on the phone for offline. The SOP viewer reuses it.

## 7. Copyright

PDFs private, never on public links, Funnel or exports; source URL stored; "Internal reference copy — see
source" on the record and in the viewer; quotes are short snippets with a citation; nothing re-published.

## 8. Effort

One server release + one smaller app release; the Mac OCR job is a small script run on Tim's Mac.
