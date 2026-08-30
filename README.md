# ha-classdash

[![Validate with hassfest](https://github.com/vemboy200/ha-classdash/actions/workflows/hassfest.yaml/badge.svg)](https://github.com/vemboy200/ha-classdash/actions/workflows/hassfest.yaml)
[![HACS Action](https://github.com/vemboy200/ha-classdash/actions/workflows/hacs.yaml/badge.svg)](https://github.com/vemboy200/ha-classdash/actions/workflows/hacs.yaml)
[![Test](https://github.com/vemboy200/ha-classdash/actions/workflows/test.yml/badge.svg)](https://github.com/vemboy200/ha-classdash/actions/workflows/test.yml)

A Home Assistant custom integration for [ClassDash](https://github.com/vemboy200/ClassDash)'s
home API — reads due/overdue/ahead assignments and class announcements into
Home Assistant sensors. Read-only, same as the API it talks to.

This is a personal companion project, not something aimed at Home Assistant
core: it only makes sense for someone running ClassDash's home API on their
own network.

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

One device ("ClassDash"), six sensors, polled every 5 minutes:

| Entity | What it is |
|---|---|
| Due soon | Count of assignments due soon, with the list (up to 10) as an attribute |
| Overdue | Count of overdue assignments, same attribute pattern |
| Ahead | Count of assignments due further out |
| Announcements | Count of recent teacher announcements, with a trimmed list attribute |
| Classes | Number of classes ClassDash currently tracks |
| Last collected | Timestamp of ClassDash's last successful collection pass, with `minutes_ago` |

## Not (yet) implemented

ClassDash's home API also exposes `/api/stream` (Server-Sent Events) for
push updates instead of polling. This integration polls for now — SSE
would mean holding a long-lived connection and reacting to a push instead
of a fixed interval, which is a real change in shape, not a small addition.

## License

GPL-3.0, matching ClassDash itself — see [LICENSE](LICENSE).
