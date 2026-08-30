# ha-classdash

[![Validate with hassfest](https://github.com/vemboy200/ha-classdash/actions/workflows/hassfest.yaml/badge.svg)](https://github.com/vemboy200/ha-classdash/actions/workflows/hassfest.yaml)
[![HACS Action](https://github.com/vemboy200/ha-classdash/actions/workflows/hacs.yaml/badge.svg)](https://github.com/vemboy200/ha-classdash/actions/workflows/hacs.yaml)
[![Test](https://github.com/vemboy200/ha-classdash/actions/workflows/test.yml/badge.svg)](https://github.com/vemboy200/ha-classdash/actions/workflows/test.yml)

A Home Assistant custom integration for [ClassDash](https://github.com/vemboy200/ClassDash)'s
home API — reads due/overdue/ahead assignments and class announcements into
Home Assistant sensors. Read-only, same as the API it talks to.

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

## Entities

Everything updates by push, not polling — Home Assistant holds
`/api/stream` open for as long as the entry is loaded; ClassDash sends the
full current state the moment that connection opens, then again only when
a collection pass actually changes something. If the connection drops,
it's retried with backoff (5s up to 5 minutes); a brief blip doesn't
touch the entities, but a longer outage marks them unavailable rather
than silently going stale forever.

**One main device ("ClassDash")** with everything not tied to a specific
class:

| Entity | What it is |
|---|---|
| Due soon | Total assignments due soon across every class, with the list (up to 10) as an attribute |
| Overdue | Total overdue assignments, same attribute pattern |
| Ahead | Total assignments due further out |
| Announcements | Total recent teacher announcements, with a trimmed list attribute |
| Classes | Number of classes ClassDash currently tracks |
| Last collected | Timestamp of ClassDash's last successful collection pass, with `minutes_ago` |

**One sub-device per class**, linked to the main device, created the
moment a class shows up with anything due or announced (there's no
"list of all classes" endpoint that covers Canvas and Edpuzzle, only
Google Classroom — so this is derived from live data rather than seeded
upfront):

| Entity | What it is |
|---|---|
| Due soon / Overdue / Ahead | That class's own counts, same attribute pattern as the main device |
| Assignments (calendar) | That class's due-soon + ahead + overdue assignments as calendar events — each due date/time becomes a 30-minute event; overdue ones stay on the calendar too, they just don't show as the "next" event |

A class's entities are created once and kept — a class with nothing
currently due still has its device, its sensors just read 0 and its
calendar shows no upcoming events, rather than flickering in and out as
things get assigned and turned in.

## License

GPL-3.0, matching ClassDash itself — see [LICENSE](LICENSE).
