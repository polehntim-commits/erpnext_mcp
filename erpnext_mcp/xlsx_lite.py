# SPDX-License-Identifier: MIT
"""Read the VALUES out of an .xlsx workbook with the standard library only. v0.274.0.

An .xlsx is a zip of XML. A pro forma is read for the numbers it shows — the cached value of each cell, formulas
already calculated by the program that saved it — so this needs no spreadsheet library: shared strings, inline
strings, numbers and booleans, by sheet name. Dates come back as Excel serial numbers (`as_date` converts one).
Nothing is evaluated, nothing is executed, external links are ignored; a workbook over the size cap is refused.
"""

from __future__ import annotations

import datetime
import io
import re
import zipfile
from xml.etree import ElementTree

MAX_BYTES = 20 * 1024 * 1024
MAX_CELLS = 400_000
_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


class XlsxError(Exception):
	pass


def _xml(zf: zipfile.ZipFile, name: str):
	try:
		return ElementTree.fromstring(zf.read(name))
	except KeyError:
		return None
	except ElementTree.ParseError as exc:
		raise XlsxError(f"{name} is not readable XML ({exc}).") from None


def _col(ref: str) -> int:
	letters = re.match(r"[A-Z]+", ref or "")
	number = 0
	for ch in letters.group(0) if letters else "":
		number = number * 26 + (ord(ch) - 64)
	return number - 1


def _text(node) -> str:
	return "".join(t.text or "" for t in node.iter(f"{{{_NS['m']}}}t"))


def open_workbook(content: bytes) -> dict:
	"""{sheet name: [[value, ...], ...]} — rows as lists, None where a cell is empty."""
	if len(content or b"") > MAX_BYTES:
		raise XlsxError("the workbook is over 20 MB.")
	try:
		zf = zipfile.ZipFile(io.BytesIO(content))
	except zipfile.BadZipFile:
		raise XlsxError("that is not an .xlsx workbook.") from None
	book = _xml(zf, "xl/workbook.xml")
	if book is None:
		raise XlsxError("the workbook has no xl/workbook.xml.")
	rels = _xml(zf, "xl/_rels/workbook.xml.rels")
	targets = {r.get("Id"): r.get("Target") for r in (rels if rels is not None else [])}
	shared = []
	strings = _xml(zf, "xl/sharedStrings.xml")
	if strings is not None:
		shared = [_text(si) for si in strings.findall("m:si", _NS)]
	out, cells = {}, 0
	for sheet in book.findall("m:sheets/m:sheet", _NS):
		target = targets.get(sheet.get(_REL)) or ""
		path = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
		root = _xml(zf, path)
		if root is None:
			continue
		rows = []
		for row in root.findall("m:sheetData/m:row", _NS):
			index = int(row.get("r") or len(rows) + 1) - 1
			while len(rows) < index:
				rows.append([])
			values: list = []
			for cell in row.findall("m:c", _NS):
				cells += 1
				if cells > MAX_CELLS:
					raise XlsxError(f"the workbook has more than {MAX_CELLS} cells.")
				col = _col(cell.get("r") or "")
				kind = cell.get("t")
				raw = cell.find("m:v", _NS)
				if kind == "s" and raw is not None:
					value = shared[int(raw.text)] if raw.text and int(raw.text) < len(shared) else None
				elif kind == "inlineStr":
					inline = cell.find("m:is", _NS)
					value = _text(inline) if inline is not None else None
				elif kind == "b" and raw is not None:
					value = raw.text == "1"
				elif kind in ("str", "e") and raw is not None:
					value = raw.text
				elif raw is not None and raw.text not in (None, ""):
					try:
						number = float(raw.text)
						value = int(number) if number.is_integer() and "." not in raw.text and "E" not in raw.text else number
					except ValueError:
						value = raw.text
				else:
					value = None
				col = col if col >= 0 else len(values)
				while len(values) < col:
					values.append(None)
				values.append(value)
			rows.append(values)
		out[sheet.get("name")] = rows
	return out


def as_date(serial) -> str | None:
	"""An Excel serial day (1900 system) as YYYY-MM-DD."""
	try:
		return (datetime.date(1899, 12, 30) + datetime.timedelta(days=float(serial))).isoformat()
	except (TypeError, ValueError, OverflowError):
		return None


def find(rows: list, text: str) -> tuple | None:
	"""(row, col) of the first cell whose text equals `text` (case and spacing ignored)."""
	wanted = re.sub(r"\s+", " ", str(text)).strip().casefold()
	for r, row in enumerate(rows):
		for c, value in enumerate(row):
			if isinstance(value, str) and re.sub(r"\s+", " ", value).strip().casefold() == wanted:
				return r, c
	return None
