# SPDX-License-Identifier: MIT
"""Fetch one document from a URL a phone pasted, without becoming a proxy.

v0.191.0. `attach_asset_document` lets a worker paste the link to a machine's
operator manual — a manufacturer's PDF — and have the SERVER download it and file
the bytes against the asset. A link would rot the day the manufacturer moves its
site; the bytes do not.

THAT IS A SERVER-SIDE REQUEST FORGERY SURFACE AND THIS MODULE IS ITS WHOLE
DEFENCE. The bench this runs on sits on a home network behind Tailscale: the
router, the Umbrel dashboard, the Frappe port and every other tailnet machine are
one private address away. So a URL is only fetched when EVERY address its host
resolves to is a public one, the connection goes to the address that was checked
rather than to a second lookup a rebinding DNS server could answer differently,
each redirect is checked the same way from the start, and the body is capped
while it streams rather than measured after it has landed.

WHAT COMES BACK HAS TO BE A DOCUMENT. The magic bytes decide — PDF, PNG, JPEG,
HEIC, WebP, or plain text a server labelled as such — because a Content-Type
header is whatever the far end chose to say, and an HTML login page served as
`application/pdf` is still a login page.

STANDARD LIBRARY ONLY. `requests` is importable wherever Frappe is but is not a
declared dependency of this app, and it resolves the host itself — which is the
second lookup this module exists to avoid.
"""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlsplit

from .errors import ToolError

#: The most one fetched document may weigh. An operator's manual with photographs
#: is ten or fifteen megabytes; twenty-five is room for that and not for a video.
MAX_BYTES = 25 * 1024 * 1024
#: Hops followed before giving up. A manufacturer's CDN link commonly redirects
#: once or twice (http → https, then a signed storage URL); a chain longer than
#: this is a loop or somebody steering.
MAX_REDIRECTS = 3
#: Seconds for the connection and for each read, and for the whole fetch.
TIMEOUT = 15
TOTAL_DEADLINE = 90
CHUNK = 64 * 1024
USER_AGENT = "erpnext_mcp-document-fetch/1"
TAIL = "Nothing was attached."

REDIRECTS = frozenset({301, 302, 303, 307, 308})


@dataclass
class Fetched:
	"""One downloaded document: its bytes and what was learned about them."""

	content: bytes
	extension: str
	mime_type: str
	final_url: str
	content_type: str
	disposition_name: str


# ── the address check ────────────────────────────────────────────────────────
def is_public_address(value: str) -> bool:
	"""Whether one resolved address is somewhere on the public internet.

	`is_global` is the test and the named flags are belt and braces, because each
	of them is a way onto this bench's own network: loopback is the bench, the
	RFC 1918 ranges are the farm's LAN, link-local is the cloud metadata address,
	and 100.64/10 — which `is_global` excludes — is the tailnet itself. An IPv4
	address wrapped in IPv6 (`::ffff:10.0.0.5`) is judged as the IPv4 it is.
	"""
	try:
		address = ipaddress.ip_address(str(value).split("%", 1)[0])
	except ValueError:
		return False
	mapped = getattr(address, "ipv4_mapped", None)
	if mapped is not None:
		address = mapped
	if (
		address.is_loopback
		or address.is_private
		or address.is_link_local
		or address.is_reserved
		or address.is_multicast
		or address.is_unspecified
	):
		return False
	return bool(address.is_global)


def check_url(url: str) -> tuple[str, str, int, str]:
	"""(scheme, host, port, request target) for a URL this module may fetch.

	Refuses before anything is resolved: a scheme other than http or https — so
	`file:///etc/passwd` and `ftp://` never reach a socket — a URL with no host,
	and a URL carrying a user name or password, which is a credential in a place
	that gets logged.
	"""
	text = str(url or "").strip()
	parts = urlsplit(text)
	scheme = (parts.scheme or "").lower()
	if scheme not in ("http", "https"):
		raise ToolError(
			f"{text!r} is not an http or https link. Only a web address can be fetched — paste "
			f"the link to the document itself. {TAIL}"
		)
	if parts.username or parts.password:
		raise ToolError(f"that link carries a user name or password, which this will not send. {TAIL}")
	host = (parts.hostname or "").strip()
	if not host:
		raise ToolError(f"{text!r} names no host. {TAIL}")
	try:
		port = parts.port or (443 if scheme == "https" else 80)
	except ValueError:
		raise ToolError(f"{text!r} has a port that is not a number. {TAIL}") from None
	target = parts.path or "/"
	if parts.query:
		target = f"{target}?{parts.query}"
	return scheme, host, port, target


def resolve_public(host: str, port: int) -> str:
	"""The address to connect to, when EVERY address `host` resolves to is public.

	Every one and not the first, because a name that answers one public and one
	private address is a name whose owner chooses which one a client uses — and
	refusing only when the first is private is a coin toss an attacker can load.
	"""
	try:
		infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
	except (OSError, UnicodeError):
		raise ToolError(f"{host} could not be found. Check the link. {TAIL}") from None
	addresses = []
	for info in infos:
		address = str(info[4][0])
		if address not in addresses:
			addresses.append(address)
	if not addresses:
		raise ToolError(f"{host} could not be found. Check the link. {TAIL}")
	refused = [address for address in addresses if not is_public_address(address)]
	if refused:
		raise ToolError(
			f"{host} points at {', '.join(refused)}, which is not on the public internet. This "
			f"only fetches documents from public web servers. {TAIL}"
		)
	return addresses[0]


# ── the connection ───────────────────────────────────────────────────────────
class _PinnedHTTP(http.client.HTTPConnection):
	"""An HTTP connection to the address that was checked, whatever DNS says now."""

	def __init__(self, host, port, address, timeout):
		super().__init__(host, port, timeout=timeout)
		self._address = address

	def connect(self):
		self.sock = socket.create_connection((self._address, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
	"""The same, with TLS verified against the NAME — so the pin costs no security."""

	def __init__(self, host, port, address, timeout):
		super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
		self._address = address

	def connect(self):
		raw = socket.create_connection((self._address, self.port), self.timeout)
		self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def _open(scheme: str, host: str, port: int, address: str, target: str, timeout: float):
	"""One GET, to a pinned address. Returns an `http.client.HTTPResponse`.

	Split out so a test can stand in for the network without standing in for the
	checks around it.
	"""
	kind = _PinnedHTTPS if scheme == "https" else _PinnedHTTP
	connection = kind(host, port, address, timeout)
	connection.request(
		"GET",
		target,
		headers={
			"User-Agent": USER_AGENT,
			"Accept": "application/pdf, image/*, text/plain;q=0.5, */*;q=0.1",
			# Identity, so the cap below counts the bytes that land on disk.
			"Accept-Encoding": "identity",
		},
	)
	return connection.getresponse()


# ── the fetch ────────────────────────────────────────────────────────────────
def fetch(url: str, *, max_bytes: int = MAX_BYTES, max_redirects: int = MAX_REDIRECTS, timeout: float = TIMEOUT) -> Fetched:
	"""Download one document. Raises `ToolError`, in a sentence, for every refusal."""
	current = str(url or "").strip()
	deadline = time.monotonic() + TOTAL_DEADLINE
	for _hop in range(max_redirects + 1):
		scheme, host, port, target = check_url(current)
		address = resolve_public(host, port)
		try:
			response = _open(scheme, host, port, address, target, timeout)
		except ToolError:
			raise
		except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
			raise ToolError(f"{host} could not be reached ({type(exc).__name__}). {TAIL}") from None
		try:
			status = int(getattr(response, "status", 0) or 0)
			if status in REDIRECTS:
				location = str(response.getheader("Location") or "").strip()
				if not location:
					raise ToolError(f"{host} answered a redirect with nowhere to go. {TAIL}")
				current = urljoin(current, location)
				continue
			if status != 200:
				raise ToolError(
					f"{host} answered HTTP {status} rather than the document. Open the link in a "
					f"browser to check it still works. {TAIL}"
				)
			content = _read_capped(response, max_bytes, deadline)
			content_type = str(response.getheader("Content-Type") or "")
			disposition = str(response.getheader("Content-Disposition") or "")
		finally:
			close = getattr(response, "close", None)
			if callable(close):
				close()
		extension, mime = sniff(content, content_type)
		return Fetched(
			content=content,
			extension=extension,
			mime_type=mime,
			final_url=current,
			content_type=content_type,
			disposition_name=disposition_file_name(disposition),
		)
	raise ToolError(f"that link redirected more than {max_redirects} times. {TAIL}")


def _read_capped(response, max_bytes: int, deadline: float) -> bytes:
	"""The body, refused the moment it passes `max_bytes` — never after it landed."""
	declared = response.getheader("Content-Length")
	try:
		if declared is not None and int(declared) > max_bytes:
			raise ToolError(
				f"that document is {_size(int(declared))}, over the {_size(max_bytes)} this "
				f"will fetch. {TAIL}"
			)
	except ValueError:
		pass
	received = bytearray()
	while True:
		if time.monotonic() > deadline:
			raise ToolError(f"that download took longer than {TOTAL_DEADLINE} seconds. {TAIL}")
		chunk = response.read(CHUNK)
		if not chunk:
			break
		received.extend(chunk)
		if len(received) > max_bytes:
			raise ToolError(
				f"that document is larger than the {_size(max_bytes)} this will fetch. {TAIL}"
			)
	if not received:
		raise ToolError(f"that link returned an empty document. {TAIL}")
	return bytes(received)


def _size(count: int) -> str:
	return f"{count / (1024 * 1024):.0f} MB" if count >= 1024 * 1024 else f"{count} bytes"


# ── what the bytes are ───────────────────────────────────────────────────────
#: HEIF brands an iPhone or a converter writes into the `ftyp` box.
HEIC_BRANDS = frozenset({b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"})

#: What may be filed, by extension. The phone's viewer opens every one.
DOCUMENT_EXTENSIONS = ("pdf", "png", "jpg", "jpeg", "heic", "heif", "webp", "txt")


def sniff(content: bytes, content_type: str = "") -> tuple[str, str]:
	"""(extension, mime type) off the magic bytes, or a refusal naming what IS allowed.

	Plain text is the one type with no magic number, so it is the one where the
	server's own label counts — and only alongside bytes that decode as UTF-8,
	carry no NUL and do not open with a tag, which is what an HTML page served as
	text/plain would.
	"""
	head = bytes(content[:16])
	if head.startswith(b"%PDF-"):
		return "pdf", "application/pdf"
	if head.startswith(b"\x89PNG\r\n\x1a\n"):
		return "png", "image/png"
	if head.startswith(b"\xff\xd8\xff"):
		return "jpg", "image/jpeg"
	if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
		return "webp", "image/webp"
	if head[4:8] == b"ftyp" and head[8:12] in HEIC_BRANDS:
		return "heic", "image/heic"
	declared = content_type.split(";", 1)[0].strip().lower()
	if declared == "text/plain" and b"\x00" not in content[:65536]:
		try:
			text = content.decode("utf-8")
		except UnicodeDecodeError:
			text = None
		if text is not None and not text.lstrip().startswith("<"):
			return "txt", "text/plain"
	raise ToolError(
		f"that link returned {declared or 'something'} that is not a PDF, a photograph "
		f"(PNG, JPEG, HEIC, WebP) or plain text. If it opens a web page, find the direct link "
		f"to the document on it. {TAIL}"
	)


def disposition_file_name(header: str) -> str:
	"""The filename a Content-Disposition header offers, or "". `filename*` wins."""
	text = str(header or "")
	match = re.search(r"filename\*\s*=\s*(?:[\w-]+'[^']*')?\"?([^\";]+)\"?", text, re.IGNORECASE)
	if match:
		return unquote(match.group(1)).strip()
	match = re.search(r"filename\s*=\s*\"?([^\";]+)\"?", text, re.IGNORECASE)
	return match.group(1).strip() if match else ""


def safe_file_name(title: str, url: str, disposition_name: str, extension: str) -> str:
	"""A filename for the stored document, ending in the extension the BYTES earned.

	`title` first, because it is what the worker typed about the machine; then
	what the server offered; then the last segment of the URL; then "document".
	Path separators, control characters and anything outside a plain set are
	dropped, so nothing here can name a directory.
	"""
	candidates = [title, disposition_name, unquote(urlsplit(str(url or "")).path.rsplit("/", 1)[-1])]
	stem = ""
	for candidate in candidates:
		cleaned = _clean_stem(candidate)
		if cleaned:
			stem = cleaned
			break
	stem = stem or "document"
	return f"{stem[:120].rstrip(' .-')}.{extension}"


def _clean_stem(value: str) -> str:
	text = str(value or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
	base, dot, tail = text.rpartition(".")
	if dot and tail.lower() in DOCUMENT_EXTENSIONS + ("htm", "html", "php", "aspx", "bin"):
		text = base
	text = re.sub(r"[^A-Za-z0-9 ._()\-]+", " ", text)
	text = re.sub(r"\s+", " ", text).strip(" .-")
	return text
