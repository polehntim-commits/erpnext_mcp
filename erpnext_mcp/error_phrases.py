# SPDX-License-Identifier: MIT
"""The refusals a worker hits on the phone, recognised from their English. v0.254.0.

WHY MATCH THE ENGLISH. `ToolError(key=…)` has been the way to give a refusal a translation since
v0.85.0, and in all that time no raise site passed one: the keys in the catalogue were never sent, so
every refusal reached a Spanish-speaking phone as `error.unspecified` ("something went wrong") or as
English. There are several hundred raise sites, most of them read by a model and not a person. The ones
a WORKER hits are a few dozen, and this table names them: a pattern over the English the code already
writes (the start of the sentence, with the names in it captured), a short English line, and Spanish in
the informal register (tú) the app uses everywhere.

`api/guard._stamp_error` asks `match()` only when the exception carries no key of its own, so a key at a
raise site always wins. The English message is never changed — it stays what a model corrects itself
from and what the Action Log records. The rows are ordinary Farm Translation rows (category Error
Messages): an operator can reword one in the Desk and the next migrate leaves it alone.

ORDER MATTERS: the first pattern that matches wins, so a specific sentence goes above a general one.
"""

from __future__ import annotations

import re

#: (key, pattern over the English, short English, Spanish). `{name}` in the lines is filled from the
#: pattern's named groups.
_PHRASES = (
	# ── work: claim, start, pause, resume, complete, hand back ───────────────
	("error.task.on_hold", r"(?P<task>\S+) is on Hold: (?P<reasons>.+?)\. A supervisor can let it start today",
	 "{task} is on Hold: {reasons}. A supervisor can let it start today.",
	 "{task} está en espera: {reasons}. Un supervisor puede dejar que empiece hoy."),
	("error.task.draft", r"(?P<task>\S+) is still a Draft and is not in the pool",
	 "{task} is not published yet.", "{task} todavía no está publicado."),
	("error.task.held_by_other_state", r"(?P<task>\S+) is (?P<state>[\w-]+) and held by (?P<who>.+?)\. Two people",
	 "{task} is already being done by {who}.", "{who} ya está haciendo {task}."),
	("error.task.crew_task", r"(?P<task>\S+) is a crew task: nobody takes it from the pool",
	 "{task} is a crew task — your foreman puts you on the crew.",
	 "{task} es una tarea de cuadrilla: tu mayordomo te agrega a la cuadrilla."),
	("error.task.dispatched_only", r"(?P<task>\S+) is dispatch_mode Dispatched",
	 "{task} is assigned by name — ask your foreman.", "{task} se asigna por nombre: pregúntale a tu mayordomo."),
	("error.task.claim_limit", r"(?P<who>.+?) is already holding (?P<count>\d+) task\(s\)",
	 "You are holding {count} tasks already. Finish one first.",
	 "Ya tienes {count} tareas. Termina una primero."),
	("error.task.not_yours", r"(?P<task>\S+) is held by (?P<who>.+?), not (?P<worker>\S+?)\.",
	 "{task} is held by {who}, not you.", "{task} lo tiene {who}, no tú."),
	("error.task.already_held", r"(?P<task>\S+) is already held by (?P<who>.+?)\. Nothing",
	 "{task} is already held by {who}.", "{task} ya lo tiene {who}."),
	("error.task.already_started", r"(?P<assignment>\S+) was already started at (?P<at>[^.]+)\.",
	 "This task was already started at {at}.", "Esta tarea ya se empezó a las {at}."),
	("error.task.already_paused", r"(?P<assignment>\S+) was already paused at (?P<at>[^.]+)\.",
	 "This task was already paused at {at}.", "Esta tarea ya se pausó a las {at}."),
	("error.task.not_in_progress", r"(?P<assignment>\S+) is (?P<state>[\w-]+), not In-Progress\.",
	 "Only work in progress can be paused.", "Solo se puede pausar un trabajo que está en curso."),
	("error.task.already_in_progress", r"(?P<assignment>\S+) is already in progress\.",
	 "This task is already in progress.", "Esta tarea ya está en curso."),
	("error.task.nothing_to_claim", r"(?P<task>\S+) is (?P<state>Completed|Cancelled|Rejected|Merged|Awaiting Review)\. There is nothing to claim",
	 "{task} is {state} — there is nothing to claim.", "{task} ya no está disponible ({state})."),
	("error.task.waiting_on", r"(?P<task>\S+) is waiting on ",
	 "{task} is waiting on another task to finish first.", "{task} espera a que termine otra tarea primero."),
	("error.task.steps_open", r"(?P<task>\S+) has (?P<count>\d+) step\(s\) still open",
	 "{task} has {count} steps still open.", "{task} todavía tiene {count} pasos abiertos."),
	("error.task.checklist_open", r"(?P<task>\S+) cannot be completed: (?P<count>\d+) required checklist item",
	 "{task}: {count} checklist items are not marked done.", "{task}: faltan {count} puntos de la lista por marcar."),
	("error.task.evidence_contract", r"(?P<task>\S+) cannot be completed: its evidence contract is not met",
	 "{task} needs its evidence (photos, readings or signature) before it can be filed.",
	 "{task} necesita su evidencia (fotos, lecturas o firma) antes de registrarse."),
	("error.task.crew_led", r"(?P<task>\S+) is a crew task led by (?P<who>.+?)\. One supervisor closes it",
	 "{task} is closed by its crew leader, {who}.", "{task} lo cierra el jefe de cuadrilla, {who}."),
	("error.task.cannot_complete", r"(?P<assignment>\S+) is (?P<state>[\w-]+) and cannot be completed",
	 "This task cannot be completed ({state}).", "Esta tarea no se puede completar ({state})."),
	("error.task.cannot_reject", r"(?P<assignment>\S+) is (?P<state>[\w-]+) and cannot be rejected",
	 "This task cannot be handed back ({state}).", "Esta tarea no se puede devolver ({state})."),
	("error.task.reason_to_reject", r"A reason is required to hand a task back",
	 "Say why you are handing it back.", "Di por qué la devuelves."),
	("error.task.findings_missing", r"clean_pass=false says something was found, and findings_text is empty",
	 "You said something was found — write what.", "Dijiste que encontraste algo: escribe qué."),
	("error.task.hours_reading", r"hours_reading (?:must be the number on the hour meter|cannot be negative)",
	 "Type the number on the hour meter.", "Escribe el número del horómetro."),
	("error.task.phi", r"(?P<block>.+?) is inside a pre-harvest interval until (?P<until>[^ .,;]+)",
	 "{block} is inside a pre-harvest interval until {until}.",
	 "{block} está dentro del intervalo antes de cosecha hasta {until}."),
	("error.task.minor", r"(?P<who>.+?) is (?P<age>\d+[^,]*), and (?P<task>\S+) is a (?P<kind>[^.]+?) task\.",
	 "{who} is too young for this work.", "{who} es menor de edad para este trabajo."),
	("error.task.no_employee", r"(?P<user>\S+) has no Employee record on this site",
	 "Your login is not linked to an employee record. Ask the office.",
	 "Tu cuenta no está vinculada a un registro de empleado. Pregunta en la oficina."),
	("error.report.rate_limited", r"(?P<who>.+?) has already filed (?P<count>\d+) field reports in the last hour",
	 "You have filed {count} reports in the last hour. Wait a little.",
	 "Ya enviaste {count} reportes en la última hora. Espera un poco."),
	("error.report.critical_role", r"Critical urgency on a field report is restricted",
	 "Only a foreman or manager can mark a report Critical.",
	 "Solo un mayordomo o gerente puede marcar un reporte como Crítico."),
	# ── Go / Hold ─────────────────────────────────────────────────────────────
	("error.hold.reason", r"a reason is required \(a few words: why it is safe to start today\)",
	 "Write why it is safe to start today.", "Escribe por qué es seguro empezar hoy."),
	("error.hold.not_supervisor", r"(?P<user>\S+) cannot override a Hold",
	 "Only a foreman or manager can override a Hold.", "Solo un mayordomo o gerente puede quitar una espera."),
	# ── shifts and punches ────────────────────────────────────────────────────
	("error.shift.on_other", r"(?P<who>.+?) is already on (?P<shift>\S+), an OPEN shift",
	 "{who} is already on open shift {shift}.", "{who} ya está en el turno abierto {shift}."),
	("error.shift.over", r"(?P<shift>\S+) ended at (?P<at>[^.]+)\. Nobody joins a shift that is over",
	 "{shift} is over.", "{shift} ya terminó."),
	("error.shift.already_on_crew", r"(?P<who>.+?) is already on this crew, joined at (?P<at>.+)",
	 "{who} is already on this crew.", "{who} ya está en esta cuadrilla."),
	("error.shift.not_on_crew", r"(?P<who>\S+) is not on the crew of (?P<shift>\S+)\.",
	 "{who} is not on this crew.", "{who} no está en esta cuadrilla."),
	("error.shift.already_left", r"(?P<who>.+?) already left (?P<shift>\S+) at (?P<at>[^.]+)\.",
	 "{who} already left at {at}.", "{who} ya salió a las {at}."),
	("error.shift.already_cancelled", r"(?P<shift>\S+) was already cancelled at",
	 "{shift} was already cancelled.", "{shift} ya se canceló."),
	("error.shift.already_closed", r"(?P<shift>\S+) was (?:already closed|CLOSED) at",
	 "{shift} is already closed.", "{shift} ya está cerrado."),
	("error.shift.cancel_reason", r"cancellation_reason is required",
	 "Write why the shift is cancelled.", "Escribe por qué se cancela el turno."),
	("error.shift.signature", r"supervisor_signature_file_token is required to close a shift",
	 "Closing a shift needs the supervisor's signature.", "Cerrar un turno necesita la firma del supervisor."),
	("error.shift.ends_before_start", r"this call (?:ends|cancels) the shift at (?P<end>.+?) and it started at",
	 "That time is before the shift started.", "Esa hora es antes de que empezara el turno."),
	("error.shift.needs_location", r"you have no shift open, so this starts one — and a shift needs a location",
	 "Pick the block or yard to open a shift.", "Elige el bloque o el patio para abrir un turno."),
	("error.shift.break_end", r"ended_at \((?P<end>[^)]+)\) is before the break started",
	 "A break cannot end before it began.", "Un descanso no puede terminar antes de empezar."),
	("error.punch.locked", r"(?:.+?'s punch on \S+ was reviewed|that punch was reviewed and is locked)",
	 "This punch was reviewed and is locked for payroll. A manager can reopen it.",
	 "Esta marca de tiempo ya se revisó y está bloqueada para la nómina. Un gerente puede reabrirla."),
	("error.punch.reason", r"(?P<action>approve|fix|reopen) needs a reason",
	 "Write a reason (a few words).", "Escribe un motivo (unas palabras)."),
	# ── access ────────────────────────────────────────────────────────────────
	("error.contacts.role", r"The contact register is restricted",
	 "Contacts are for a foreman, manager or the office.",
	 "Los contactos son para un mayordomo, un gerente o la oficina."),
	("error.role.restricted", r"(?P<what>.+?) is restricted to (?P<roles>[^.]+)\.",
	 "That is for {roles}.", "Eso es solo para: {roles}."),
	("error.contacts.where_met", r"give met_at and/or met_on",
	 "Say where or when you met.", "Di dónde o cuándo se conocieron."),
	# ── receipts, items, assets ───────────────────────────────────────────────
	("error.asset.duplicate_tag", r"Asset Register already has a record called '(?P<tag>[^']+)'",
	 "Tag {tag} is already registered.", "La etiqueta {tag} ya está registrada."),
	("error.item.no_uom", r"no UOM called '(?P<uom>[^']+)'",
	 "The unit '{uom}' is not on the farm's list.", "La unidad '{uom}' no está en la lista de la granja."),
	("error.receipt.nothing_changed", r"expense receipt (?P<receipt>\S+) already reads what was asked for",
	 "Nothing changed — the receipt already says that.", "No cambió nada: el recibo ya dice eso."),
	("error.upload.too_big", r"file_content is over the 8 MB inline limit",
	 "That file is too big to send this way.", "Ese archivo es demasiado grande para enviarlo así."),
)

_COMPILED = tuple((key, re.compile(pattern, re.S), en, es) for key, pattern, en, es in _PHRASES)


def catalogue() -> list:
	"""(key, English, Spanish) for the shipped Farm Translation rows."""
	return [(key, en, es) for key, _pattern, en, es in _PHRASES]


def match(message: str) -> tuple:
	"""(key, fill) for the first phrase the English message starts with, else ("", {})."""
	text = str(message or "").strip()
	for key, pattern, _en, _es in _COMPILED:
		found = pattern.match(text)
		if found:
			return key, {name: (value or "").strip() for name, value in found.groupdict().items()}
	return "", {}
