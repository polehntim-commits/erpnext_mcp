# Proximity push-to-talk ("walkie-talkie") in Farm Ops

**Status: DROPPED (2026-10-04). Tim dropped push-to-talk; this note is kept for the record only and is off the build queue. Crew messaging (no-work notices, etc.) is unaffected and stays as designed.**

**At a glance**

- **Effort:** M — one app release (no server work for the local mode; a small server piece only for the optional
  tower mode).
- **Risks:** it must never be mistaken for the emergency channel; range on Bluetooth / peer Wi-Fi is short and
  terrain eats it; a phone in a pocket on a running machine is a hands-free and hearing problem.
- **Open questions for Tim:** see §7.

## 1. The problem

Flaggers and machine operators (track hoe, tractor, the D-6C) need to talk across 50–300 m without walking over or
shouting over an engine. Handheld radios exist but are not always on the person; every worker already carries the
phone with Farm Ops. The crew must be able to talk to each other (decision 36).

## 2. What already exists and is reused

- **The crew**: shifts, crew task members and badges already say who is working together today — the talk group
  is "my crew on this shift", no new roster.
- **Offline-first app shell**: the queue, background sync and push registration (APNs device tokens).
- **Spanish**: the app's language setting; PTT system UI is localised by iOS.

## 3. Apple's pieces (Apple-only, no third-party SDKs)

| Piece | What it gives | Limits |
|---|---|---|
| **PushToTalk framework** (iOS 16+) | The system PTT UI: a talk button on the Lock Screen and Dynamic Island, transmit/receive while the app is in the background, and a special PTT push that wakes the app to play incoming audio. | Needs Apple's PushToTalk entitlement; the APP supplies the audio transport (PushToTalk does not carry audio); incoming audio arrives via a PTT push, which needs the internet. |
| **Multipeer Connectivity** / **Network.framework peer-to-peer** | Phone-to-phone over Bluetooth and peer-to-peer Wi-Fi, no network or tower needed. | Typical range ~30–100 m line of sight, less through trees and terrain; foreground or recently backgrounded only; no store-and-forward between distant phones unless the app relays. |
| **AVAudioEngine** | Capture and play short voice bursts (Opus/AAC), noise suppression via voice processing I/O. | — |

## 4. Proposed design

Two modes, one button:

1. **Local mode (offline, default):** a crew talk group over Multipeer — press, speak, release; every crew phone in
   range plays it. Works with no signal (the orchard edge, the creek). The app stays in the foreground while a
   machine operator is talking (a "talk" screen with one big button, screen kept on while on a machine).
2. **Tower mode (optional, when there is signal):** the same button through the PushToTalk framework, audio relayed
   through the farm's sidecar (short bursts, deleted after delivery), so a phone in a pocket on the Lock Screen
   still hears the crew. Needs the entitlement.

- **Group** = today's crew on the shift (or a crew task's members), shown by name; join is automatic, leave is one
  tap.
- **Priority tone + "STOP" button**: a one-tap all-call that plays a loud tone and "STOP / ALTO" on every phone in
  range — for the flagger who sees a person behind the hoe.
- **Nothing is recorded** by default; an optional log keeps only who talked when (not audio), for incident review.

## 5. Safety

- **Not the emergency channel.** The screen says so; 911 and the farm's emergency procedure stay as they are.
- **Hands-free on machines:** no use while operating unless the phone is mounted and the talk button is the screen;
  operators on noisy machines need earpieces / helmet speakers (Bluetooth headsets work with the system audio
  route); the STOP tone is loud regardless.
- **Hearing:** volume limits respect iOS headphone safety; no audio straight into ear protection at full volume.
- **Range honesty:** the app shows who is in range right now (Multipeer peers); a message to someone out of range is
  not "sent".

## 6. MCP / iOS screens

- **MCP:** none in local mode. Tower mode: `list_talk_groups` (read) and a setting to switch it on.
- **iOS:** a Talk tab or a tile — one big press-to-talk button, the group's names with in-range dots, the STOP
  all-call, a volume slider; Lock Screen PTT controls in tower mode.

## 7. Open questions for Tim

1. Local-only first (no entitlement, works offline), or apply for the PushToTalk entitlement for tower mode too?
2. Who is in a talk group: the whole shift crew, or per crew task (the flagger + the operator only)?
3. Keep a talk log (who / when, no audio) for incident review, or keep nothing?
4. Which machines get a mounted phone, and do operators have Bluetooth headsets or helmet speakers?
