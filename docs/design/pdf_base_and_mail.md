# PDFs inside the container; outgoing mail sender — contract

Server `erpnext_mcp` **v0.216.1** (with Security Alert Recipients). No app change.

## 1. The bug (OML, v0.215)

Email Queue rows with a print attached failed:
`wkhtmltopdf exited with non-zero code 1 … network error: HostNotFoundError`. Likely the same
cause as Tim's blank Desk PDF (`download_multi_pdf`, Employee).

## 2. Root cause

`frappe.utils.pdf.get_pdf` (Frappe 15) begins with `scrub_urls(html)`, which makes every relative
`src`, `href` and `url()` in the print absolute on `frappe.utils.get_url()`:

- `host_name` from site config when it is set (on OML: `http://100.69.162.122`), else
- the request's host (in the Desk: whatever address the browser used — a Tailscale name, the
  Funnel address with its `/erpnext` path), else `http://<site>` in a background job.

wkhtmltopdf runs **inside** `fafo-erpnext_server_1` and fetches those URLs itself. From there the
public name does not resolve, port 80 is not this nginx, and `/erpnext/...` does not exist. A
stylesheet that cannot be fetched is not one of the four errors `get_pdf` tolerates
(`PDF_CONTENT_ERRORS`: ContentNotFound, ContentOperationNotPermitted, UnknownContent,
RemoteHostClosed), so `pdfkit` raises and the whole PDF fails.

What the container can reach: its own nginx on `127.0.0.1:8080` (the image's "frontend" program).
It serves `/assets` and `/files` and answers for the one site whatever the Host header
(`FRAPPE_SITE_NAME_HEADER=frontend`). Gunicorn on `127.0.0.1:8000` does not serve `/assets`.

## 3. Fix — a PDF-only base URL (`erpnext_mcp/pdf_base.py`)

- Setting **PDF Asset Base URL** (`pdf_base_url`, ERPNext MCP Settings): empty = detect (first of
  `http://127.0.0.1:8080` that accepts a connection); a URL = use it; `off` = Frappe's own behaviour.
  A bench where nothing answers is left exactly as Frappe has it.
- `install()` replaces two names **inside `frappe.utils.pdf` only**:
  - `scrub_urls`: Frappe's own, with `host_name` set to the internal base for the call; then any
    absolute URL on one of the site's own addresses (`get_url()`, `host_name`, `public_url`,
    `farmops_public_url`, `http(s)://<site>`) is moved to the base — for **resources only** (`src`,
    `<link href>`, CSS `url()`); `<a href>` links are put back on the public address, so a link
    printed in the PDF still works for its reader. Other hosts are left alone.
  - `get_cookie_options`: Frappe's own with the same `host_name`, so the session cookie is scoped to
    the host it now fetches from; plus `load-media-error-handling: ignore`, so an image on an
    unreachable host is left out rather than failing the PDF.
- Installed by `before_request` and `before_job` (hooks.py): every Desk print and download, every
  Email Queue attachment (`frappe.attach_print`), every `download_multi_pdf`, every `get_pdf` this
  app calls. Idempotent.
- **Not changed:** `host_name`, `frappe.utils.get_url()`, `frappe.utils.scrub_urls` (email bodies),
  password-reset / two-factor / "view online" links. The ID-card renderer does not use wkhtmltopdf
  (it inlines its images) and is unaffected either way.

## 4. Self-test

**`test_pdf_rendering(doctype?, name?, print_format?)`** — read tool, on by default. Renders a
one-page test page through `get_pdf` with a stylesheet linked by a relative URL (the case that
failed), and optionally the named document's real print (print permission required). Answers `ok`,
per render `bytes`/`pages`/`ms` or wkhtmltopdf's error plus a diagnosis, the wkhtmltopdf version,
the base in use and whether `<base>/assets/assets.json` answers. Saves, attaches and sends nothing.

`get_server_status.pdf`: `{base_url, source: setting|auto|off|none, installed}` — no rendering.

## 5. Outgoing mail sender

Frappe sends a person-initiated email with `From:` = that person unless the Email Account has
"Always use this email address as sender" ticked. Zoho refuses a From: other than the signed-in
mailbox (553), so those emails fail in Email Queue while system emails go through.

- `get_server_status.email`: every enabled outgoing Email Account — address, SMTP server, default,
  both sender ticks, whether the provider requires the account sender — and warnings.
- Patch `zoho_accounts_send_as_account` (runs once on migrate): ticks
  `always_use_account_email_id_as_sender` on enabled outgoing accounts whose SMTP server contains
  `zoho`. Every other account is untouched.

## Surfaces

MCP tools 984 (+1 read: `test_pdf_rendering`). Settings: `pdf_base_url`, `allow_test_pdf_rendering`.
Hooks: `before_request`, `before_job`. Patch: `zoho_accounts_send_as_account`. Mobile routes unchanged.

## Decisions for Tim

1. **The fix is in the app, not the image.** It replaces two names inside `frappe.utils.pdf` at
   runtime — the first time this app changes Frappe's own behaviour (hooks.py said it never would;
   the docstring now names this as the third, PDF-only exception, and `pdf_base_url = off` undoes it
   without a deploy). The image alternative (rewriting the HTML in a wkhtmltopdf wrapper script) is cruder and
   the image's Dockerfile has another session's uncommitted changes.
2. **Missing images are tolerated** (`load-media-error-handling: ignore`) once the base is in use:
   an unreachable external image is left out instead of failing an emailed invoice.
3. **The Zoho tick is set by migrate**, on Zoho accounts only.
