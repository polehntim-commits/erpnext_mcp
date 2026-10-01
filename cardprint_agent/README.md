# Card print agent

The Mac half of the card print queue (`docs/design/card_print_queue.md`).
ERPNext holds the queue; this small service claims the oldest job for its
station, prints it on the Evolis Primacy 2 through CUPS, and reports back. The
iPhone and the Desk only ever ask ERPNext — they never talk to this Mac.

- Python 3.9 or later, standard library only. No inbound ports.
- One job at a time. A printer that is paused, in error or not accepting claims
  nothing: the cards wait in the queue, in order, and print when it is back.
- Keeps nothing but a temp PDF, deleted after each job.

## Before installing

1. The printer is added in **System Settings › Printers & Scanners** with the
   Evolis driver, and has printed once by hand. (`lpstat -p` shows it.)
2. In ERPNext, after migrating to v0.208.0, create the agent's own user:
   - a User (e.g. `cardprint@your-farm`) with **only** the role
     **Card Print Station** — no Desk access is needed;
   - on that User, **API Access › Generate Keys**; note the API key and secret.
3. Give **Card Print Requester** to the people who may print cards.

## Install

```bash
cd cardprint_agent
./install.sh
```

It asks for the CUPS queue (found automatically), the ERPNext URL, the station
name and the API key and secret, then:

- reads the driver's real option names with `lpoptions -p <queue> -l` and writes
  `~/.config/cardprint/config.toml`;
- stores `key:secret` in the login Keychain (service `cardprint`);
- loads the LaunchAgent `farm.fafo.cardprint` (starts at login, restarts if it
  exits);
- offers one **test card** — a yes/no question, default no. Nothing prints
  unless you say yes.

## Day to day

```bash
python3 ~/Library/Application\ Support/cardprint/cardprint_agent.py --status     # printer state, config, credentials; prints nothing
python3 ~/Library/Application\ Support/cardprint/cardprint_agent.py --test-card  # one local test card
tail -f ~/Library/Logs/cardprint.log
launchctl kickstart -k gui/$(id -u)/farm.fafo.cardprint                          # restart after a config change
./uninstall.sh
```

## If a card comes out wrong

The driver's page (`PageSize=Card`) is portrait, 54.9 × 86.0 mm; the artwork is
a landscape 85.6 × 54 mm page. `fit-to-page` lets CUPS rotate and fit it. If the
test card is sideways or small, add the driver's rotation to `extra_options` in
the config and restart:

```toml
extra_options = ["fit-to-page", "Orientation=LANDSCAPE_CC90"]
```

Other driver options (ribbon type, varnish, contrast) can be added the same way
— take the names from `lpoptions -p Primacy_2 -l`, never guess them.

## What the statuses mean

| The queue shows | Why | What to do |
|---|---|---|
| Printer **Offline** | no check-in from this agent for 2 minutes | the Mac is asleep, off the network, or the agent is stopped |
| Printer **Paused** | the CUPS queue is paused or not accepting | resume it in Printers & Scanners |
| **Printer error** | the driver reports a reason (ribbon, cards, cover) | fix the printer; cards print when it clears |
| Job back to **Queued** | the printer failed mid-card; up to 3 tries | nothing — it retries |
| Job **Failed** | 3 tries used, or the file was refused | Retry from the queue screen |

## Tests

```bash
python3 -m unittest discover -s cardprint_agent/tests -t cardprint_agent
```

A fake `lp` / `lpstat` / `ipptool` and a fake ERPNext: no printer is touched.
