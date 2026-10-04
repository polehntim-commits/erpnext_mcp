# One-time upload links

**Status: PROPOSED (2026-10-03). Not approved; nothing built.** Queued after the training quiz. This doc is also
the threat model the security review asks for.

## 1. Why

- The sidecar rejects any request body over **4 MB** before reading it (`auth.MAX_BODY_BYTES`).
- The inline attach tool caps files at 8 MB (`files.ABSOLUTE_MAX_BYTES`).
- Chunked staging stores every chunk in a database table (a 500 MB video ≈ 640 rows).

So one route is allowed past the 4 MB check — only after its token is verified — and it streams straight to
disk, never to the database or memory.

## 2. `request_upload_url` (MCP write, off by default, admin roles)

- Inputs: target (doctype + docname, or `staged`), allowed types (named sets `photo`, `document`, `video`, or
  extensions), `max_bytes` (25 MB default for photos/documents; up to 2 GB for video via
  `upload_link_max_video_bytes`), private (default) or public, optional sha256, expiry (15 min default, 24 h
  max).
- Checks: target exists; caller may attach to it (same rule as `attach_file_to_document`).
- Returns `https://<farmops host>/farmops/api/upload/<token>`, expiry, `upload_id`.
- Token: 32 random bytes; only its SHA-256 is stored, in a new **Upload Link** doctype (target, limits, status
  Open → Receiving → Done / Failed / Expired / Revoked, issued by/on, note, result: File, size, sha256, IP,
  user agent).

## 3. The upload route `/farmops/api/upload/<token>`

Under the existing public `/farmops` path — **Funnel unchanged**.

1. Per-IP rate limit.
2. Constant-time hash lookup; unknown, expired, used and revoked all → the same 404.
3. Atomic claim (Open → Receiving, row-locked); a concurrent request gets 409.
4. Declared Content-Length vs cap → reject at once if over.
5. Stream to a temporary file in private files in 1 MB pieces, counting; over cap → stop, delete, 413.

- Accepts PUT (raw body) or multipart POST with exactly one file part.
- Filename reduced to its last component, control characters / `..` / slashes / leading dots stripped; stored as
  `<upload_id>-<safe name>` — the path never comes from the client.
- Extension, declared MIME and magic bytes must agree (JPEG, PNG, HEIC, PDF, MP4/MOV `ftyp`). Always refused:
  executables, scripts, SVG, HTML, archives (zip, tar, gz, 7z, rar, dmg, apk/ipa) unless the link names archives;
  video only on a `video` link.
- sha256 computed while streaming; mismatch → delete and fail.
- Finish: move into place, create the File record pointing at it (no read-back into memory), attach to the
  target (private by default), mark Done (one-time, irreversible).
- Every attempt logged (IP, user agent, size, result, reason); refusals and over-cap uploads raised in the
  security watch; repeated bad tokens from one IP raise an alert. No listing, no GET.

## 4. Other tools

`get_upload_status` (read), `list_upload_links` (read), `revoke_upload_link` (write, off). Hourly sweep expires
links and removes leftover temporary files.

## 5. Phone (later app release)

`request_my_upload_link` route scoped to the person's own records; background URLSession upload so a locked
screen doesn't stop it; the queue refers to `upload_id`; a link that died part-way is Failed and a new one is
requested; the chunked path stays as the one-bar fallback. An uploaded MP4 on a Training Type becomes a "file"
course video.

## 6. Tests (standalone suite)

Replay and concurrent use (exactly one wins, other 409); expiry and revocation identical to unknown; oversize
by declared length and by counting (chunked transfer), partial file removed; wrong type (extension vs magic,
executable as `.jpg`, SVG, zip); filenames (`../../etc/passwd`, absolute, NUL/control, very long); bad sha256;
target deleted between issue and upload; caller without attach permission refused at issue; only the hash
stored; unknown and expired tokens time the same; rate limit trips.

## 7. Threat model

256-bit tokens, hash-only storage; nothing in the URL but the token; private by default; a leaked link exposes
one upload into one target until it expires; everything logged and alerted.

## 8. Off by default

Tool switches plus a master `upload_links_enabled`; the route answers 404 when off.

## 9. Open decisions

1. Video cap 2 GB? 2. Public files: private-only by default, public needs an extra admin switch? 3. Test multi-GB
uploads over Funnel on umbrel.local before relying on them.
