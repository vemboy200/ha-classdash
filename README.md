# ha-classdash

[![Validate with hassfest](https://github.com/vemboy200/ha-classdash/actions/workflows/hassfest.yaml/badge.svg)](https://github.com/vemboy200/ha-classdash/actions/workflows/hassfest.yaml)
[![HACS Action](https://github.com/vemboy200/ha-classdash/actions/workflows/hacs.yaml/badge.svg)](https://github.com/vemboy200/ha-classdash/actions/workflows/hacs.yaml)
[![Test](https://github.com/vemboy200/ha-classdash/actions/workflows/test.yml/badge.svg)](https://github.com/vemboy200/ha-classdash/actions/workflows/test.yml)

A Home Assistant custom integration for [ClassDash](https://github.com/vemboy200/ClassDash)'s
home API — reads due/overdue/ahead assignments and class announcements into
Home Assistant sensors and a calendar per class. Mostly read-only: the
only writes are two buttons that start a collection pass, nothing that
touches assignment/announcement state.

A personal companion project — it only makes sense for someone running
ClassDash's home API on their own network — but with an eye toward
eventually submitting it to Home Assistant core. Current progress against
the [integration quality
scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/)
is tracked in
[`quality_scale.yaml`](custom_components/classdash/quality_scale.yaml).

## Requirements

- ClassDash's home API running and reachable (`npm run api`, or the
  settings-panel toggle), with the bearer token and certificate fingerprint
  it prints on startup handy
- Home Assistant 2024.6 or newer (uses the `runtime_data` config-entry
  pattern)

## Install

**HACS:** add this repository as a custom repository (category:
Integration), then install "ClassDash".

**Manual:** copy `custom_components/classdash` into your Home Assistant
config's `custom_components/` folder and restart Home Assistant.

## Setup

Settings → Devices & Services → Add Integration → ClassDash.

1. Enter the host/IP and port (default `8734`) of the computer running
   ClassDash's home API.
2. Home Assistant fetches whatever certificate that address is currently
   presenting and shows its fingerprint. **Compare it against the
   fingerprint ClassDash printed to its own terminal when the API started**
   — this is trust-on-first-use, the same model SSH uses for host keys, and
   it's the only thing standing between you and a spoofed server on the
   same network.
3. If it matches, enter the bearer token printed alongside it.

The certificate is pinned at this point — Home Assistant will trust that
exact certificate for this server going forward, and nothing else.

### If the token stops working

ClassDash's settings panel can "roll" the token (a new random one, the old
one stops working immediately). When that happens this integration will
prompt for reauthentication — the pinned certificate doesn't need to
change, just the token.

### If the server moves

Settings → Devices & Services → ClassDash → Reconfigure lets you change
the host/port without removing and re-adding the entry. It runs through
the exact same fingerprint-confirmation and token steps as initial setup
— a different address might be a genuinely different server.

## Entities

Everything updates by push, not polling — Home Assistant holds
`/api/stream` open for as long as the entry is loaded; ClassDash sends the
full current state the moment that connection opens, then again only when
a collection pass actually changes something, plus a heartbeat every 60s
regardless (just a status refresh, so "Last collected"'s `minutes_ago`
keeps ticking even during a long quiet stretch) — that heartbeat also
means a truly dead connection is noticed within about 90 seconds instead
of hanging indefinitely. If the connection drops, it's retried with
backoff (5s up to 5 minutes); a brief blip doesn't touch the entities,
but a longer outage marks them unavailable rather than silently going
stale forever.

**One main device ("ClassDash")** with everything not tied to a specific
class:

| Entity | What it is |
|---|---|
| Due soon | Total assignments due soon across every class, with the list (up to 10) as an attribute |
| Overdue | Total overdue assignments, same attribute pattern |
| Ahead | Total assignments due further out |
| Done | Total turned-in work, same attribute pattern |
| Announcements | Total recent teacher announcements, with a trimmed list attribute |
| Classes | Number of classes ClassDash currently tracks |
| Last collected | Timestamp of ClassDash's last successful collection pass, with `minutes_ago` |
| Classroom / Canvas / Edpuzzle status | `ok`, `problem`, or `unknown` — pipeline health for that platform's last collection attempt, with `at` (when) and `detail` (error message, if any) as attributes. `unknown` covers "never checked yet" and "turned off" (no Canvas address configured, Edpuzzle disabled) alike |
| Reload (button) | Starts the quick collection pass (Classroom + Canvas, ~17s) |
| Check now (button) | Starts the full collection pass (+ Edpuzzle, ~1 min) |
| App update | Tracks ClassDash's own macOS app version against its latest GitHub release — the same check `checkForUpdates()` already runs every 24 hours. Home Assistant's "Install" button only *starts the download* in the background; ClassDash's API has no way to actually install it (replace the running app and relaunch) — that step is deliberately only reachable via a native confirmation on the Mac itself. "Skip" hides the badge for that version in Home Assistant only, and doesn't touch ClassDash's own update banner |

Both buttons only *start* the pass and return immediately — same as
pressing the equivalent button on ClassDash's own summary page. There's
nothing to wait on: the push stream's next "update" event (or "Last
collected" ticking forward) is how you'd notice it finished.

**One sub-device per class**, linked to the main device, created the
moment a class is known about — either it has something due/announced,
or it shows up in ClassDash's own merged Classroom+Canvas+Edpuzzle class
roster. That roster only includes a class with nothing currently due if
**ClassDash's own `showEmptyClasses` setting is on** (off by default) —
with it off, a class still only gets a device once something's actually
due or announced for it, same as before.

| Entity | What it is |
|---|---|
| Due soon / Overdue / Ahead / Done | That class's own counts, same attribute pattern as the main device |
| Assignments (calendar) | That class's due-soon + ahead + overdue + done assignments, plus any virtual reminder assigned to it, as calendar events — each due date/time becomes a 30-minute event. An overdue item's title gets an "(overdue)" tag, and a done one gets "(done)" — e.g. "Lab report (overdue)" — since due-soon/ahead don't need one (the due date alone already says when those are), but overdue is worth calling out plainly, and done would otherwise look identical to an undone item sharing the same due date. A virtual reminder gets the same tags: "(done)" if marked done, "(overdue)" if its due date has passed. Overdue/done items don't show up as the calendar's "next" event, they're just present when the range covers them |

A class's entities aren't tied to whether anything's currently due —
zero due items just means the sensors read 0 and the calendar shows no
upcoming events, not that the device disappears. What actually removes a
class's device is the class itself going away: **ClassDash tags each
class in its roster `"known"` or `"orphaned"`** (a real class transfer,
or a class hidden on Classroom's own side, even though old data for it
is still lying around) — an orphaned class never gets a device in the
first place, even if it still has lingering items in due/overdue, and a
device that already existed gets removed automatically the next time
ClassDash pushes an update. The same applies to a class that becomes
excluded, or goes stale, in ClassDash's own settings — it simply stops
appearing, and its device goes with it. If the class ever comes back
(re-enrolled, un-excluded), its device is recreated the same way it was
the first time.

**Hidden/dismissed items don't count or show up.** ClassDash's API no
longer filters these out itself (everything comes back tagged instead,
so a client can decide) — this integration filters `"hidden"`-tagged
items out of every count, attribute list, and calendar, so a dismissed
assignment behaves the same as it always did: gone.

## Services

Four service actions dismiss or restore a specific assignment — the
same thing clicking "hide" or "not urgent" on ClassDash's own summary
page does:

| Service | What it does |
|---|---|
| `classdash.hide` | Dismisses an assignment — hidden ones don't count or show up anywhere in this integration |
| `classdash.unhide` | Reverses `hide` |
| `classdash.mute` | Marks "not urgent" — still counts, just doesn't badge/notify |
| `classdash.unmute` | Reverses `mute` |

Each takes an `id` (find it in the assignment's own list attribute on
any of the Due soon/Overdue/Ahead/Done sensors) and an optional
`config_entry_id`, only needed if more than one ClassDash server is
configured. These act on an assignment *id*, not an entity — an
assignment isn't its own HA entity, it's a list item inside a sensor's
attribute, so there's no natural entity for a hide/mute button to
attach to.

## What's read-only here

Virtual reminders (assignments you type into ClassDash yourself) show up
on the calendar of whatever class they're assigned to, but only for
reading — creating, editing, marking done, hiding, or deleting *those*
(as opposed to a real assignment, which the services above do cover) is
still ClassDash-side only. Pushing a ClassDash setting from Home
Assistant (`/api/settings`) isn't built either. Same for ClassDash's own
`dismissedVersion` on the update check — Home Assistant's own "Skip"
button is entirely local to Home Assistant and doesn't call ClassDash's
`/api/update-status/dismiss`, so dismissing there doesn't quiet
ClassDash's own update banner.

## Diagnostics

Settings → Devices & Services → ClassDash → Download diagnostics gets a
JSON dump for bug reports — counts, class names, and connection state,
not the content of any assignment or announcement (this handles a
minor's school data, and diagnostics dumps tend to end up in public issue
threads). The bearer token and pinned certificate are redacted too.

## License

GPL-3.0, matching ClassDash itself — see [LICENSE](LICENSE).
