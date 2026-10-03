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
- Home Assistant 2026.8 or newer

## Install

**HACS:** add this repository as a custom repository (category:
Integration), then install "ClassDash".

**Manual:** copy `custom_components/classdash` into your Home Assistant
config's `custom_components/` folder and restart Home Assistant.

## Setup

When ClassDash's home API is open to your network, it announces itself (mDNS, as `_classdash._tcp`), and Home Assistant offers it under Settings → Devices & Services → Discovered, as "ClassDash on <computer name>". Adding it from there skips step 1 below. Otherwise: Settings → Devices & Services → Add Integration → ClassDash.

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

If ClassDash announces itself (see Setup), there's nothing to do: Home Assistant recognizes it by its certificate's fingerprint, which the announcement carries, and follows it to its new address. The same announcement when ClassDash starts up also makes Home Assistant reconnect right away, instead of at its next reconnect attempt (up to a minute later).

Otherwise, Settings → Devices & Services → ClassDash → Reconfigure lets you change
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
backoff (5s up to 1 minute); a brief blip doesn't touch the entities,
but a longer outage marks them unavailable rather than silently going
stale forever.

ClassDash doesn't need to be running when Home Assistant starts (it's often on a laptop that's shut down, asleep, or away from home). The integration still loads, its entities show as unavailable, and they fill in as soon as ClassDash answers, the same way they come back after any outage. No reload needed.

**One main device ("ClassDash")** with everything not tied to a specific
class:

| Entity | What it is |
|---|---|
| Due soon | Total assignments due soon across every class, with the list (up to 10) as an attribute. Includes virtual reminders due within a week, same as real assignments |
| Overdue | Total overdue assignments, same attribute pattern. Includes overdue virtual reminders |
| Ahead | Total assignments due further out. Includes virtual reminders due more than a week out |
| Done | Total turned-in work, same attribute pattern. Includes virtual reminders marked done |
| Announcements | Total recent teacher announcements, with a trimmed list attribute |
| Classes | Number of classes ClassDash currently tracks |
| Last collected | Timestamp of ClassDash's last successful collection pass, with `minutes_ago` |
| Checking | On while ClassDash is checking, whether it was started by its own schedule, its page, or a button here |
| Check progress | How far the running check has got, in percent, with `done` and `total` as attributes. Unknown while nothing's running. Disabled by default, since it changes many times during every check; turn it on from the entity's settings if you want it |
| School today | On on a school day (minimum days included), by the school calendar in ClassDash's Settings → Calendar; unknown without one. Attributes: `day_kind` (`no_school` / `minimum_day`, or none on a normal day), its `label`, today's `events`, and `next_school_day` |
| Schedule today | Today's A/B or Odd/Even day from ClassDash's Settings → Schedule; unknown when the schedule doesn't rotate or it isn't a school day. Attributes: `schedule_type`, today's `classes` by period, and the `next_school_day` with its label and classes |

Each assignment in the Due soon / Overdue / Ahead / Done lists has its `id`, `title`, `class`, `due` and `link`, plus `teacher`, `locked` (Canvas work that can't be done yet or any more: `why` is `opens`, `module`, `closed` or `locked`, with `at` and `module` when they apply) and `linked` (for assignments linked in ClassDash, how many parts are `done` out of `total`). Virtual reminders have none of the last three.
| School calendar | The no-school days, minimum days and events from ClassDash's school calendar, as all-day events. ClassDash only shares the next 14 days, so that's as far ahead as it goes |
| Classroom / Canvas / Edpuzzle status | `ok`, `problem`, or `unknown` — pipeline health for that platform's last collection attempt, with `at` (when) and `detail` (error message, if any) as attributes. `unknown` covers "never checked yet" and "turned off" (no Canvas address configured, Edpuzzle disabled) alike |
| To-do list | Every real assignment (due-soon/ahead/overdue/done) and every virtual reminder, combined into one checklist — hidden items excluded, same as the sensors and calendars. Each item's title is suffixed with its class in parentheses, e.g. "Lab report (Physics)"; one with no class stays plain. Checking an item off writes back to ClassDash — but *what* it writes differs by source, since ClassDash's own write API isn't symmetric between the two: a virtual reminder has a real done/undone action (the same one its own "done" button uses), so checking one off there marks it done and un-checking marks it undone. A real assignment has no such action at all — "done" for one of those is purely ClassDash noticing Classroom/Canvas already marked it turned in, never something writable — so checking a real assignment off here hides it instead (the closest real analog available, same action `classdash.hide` already wraps), and un-checking unhides it. Un-checking a real assignment that's genuinely done (not just hidden) is a harmless no-op, since there's nothing to "un-submit." A real assignment ClassDash's own check already found done shows up checked off automatically, no action needed. Deleting an item works for virtual reminders only, and deletes it in ClassDash for good; deleting a real assignment is refused, since ClassDash only reads those (check it off to hide it instead). An item's description has its teacher, whether it's locked, a linked assignment's progress, and its link, one per line |
| Reload (button) | Starts the quick collection pass (Classroom + Canvas, ~17s) |
| Check now (button) | Starts the full collection pass (+ Edpuzzle, ~1 min) |
| Stop check (button) | Stops the check that's running, the same as ClassDash's own Stop button. Does nothing if no check is running |
| App update | Tracks ClassDash's own macOS app version against its latest GitHub release — the same check `checkForUpdates()` already runs every 24 hours. Read-only: there's no "Install" button. Actually downloading and installing an update always happens on the Mac itself, via ClassDash's own update banner or its "Check for Updates…" menu item — Home Assistant just shows whether one's available. Two reasons it stays read-only rather than driving the download the way an earlier version of this entity did: Home Assistant's generic update card has no UI for a "downloading" phase distinct from "installing", so an active install button always just showed "Installing…" the whole time regardless of what was actually happening; and ClassDash's own computed `status` doesn't (yet) notice if a downloaded file goes missing after being marked ready, so trusting it to drive a button risked showing "ready to install" long after that stopped being true. "Skip" hides the badge for that version in Home Assistant only, and doesn't touch ClassDash's own update banner. The more-info dialog's full release notes are fetched straight from GitHub's own API for that release — ClassDash's own update check only ever hands over a version number and a link, not the changelog text itself. Three extra attributes, all purely informational: `status` is ClassDash's own one-word summary of what it's doing right now (`unknown` / `error` / `downloading` / `ready` / `available` / `up_to_date`), `downloaded_version` is whatever's actually sitting downloaded — distinct from the installed version (what's running) and the latest version (what GitHub has) — and `ready_to_install` says whether it's sitting there waiting on the native confirmation |

Classroom/Canvas/Edpuzzle status, Last collected, Checking and Check progress are about ClassDash itself rather than schoolwork, so they're listed under **Diagnostic** on the device page.

Reload and Check now only *start* the pass and return immediately — same as
pressing the equivalent button on ClassDash's own summary page. There's
nothing to wait on: Checking turning off (or "Last collected" ticking
forward) is how you'd notice it finished.

**One sub-device per class**, linked to the main device, created the
moment a class is known about — either it has something due/announced,
or it shows up in ClassDash's own merged Classroom+Canvas+Edpuzzle class
roster. That roster only includes a class with nothing currently due if
**ClassDash's own `showEmptyClasses` setting is on** (off by default) —
with it off, a class still only gets a device once something's actually
due or announced for it, same as before.

Each class device's info shows its teacher ("Class by <teacher>", or "by ClassDash" when the platform doesn't say) and, as its hardware, the platform it's on (Google Classroom, Canvas or Edpuzzle), or "Linked" for classes linked in ClassDash's Settings → Classes. Both follow ClassDash when they change.

| Entity | What it is |
|---|---|
| Due soon / Overdue / Ahead / Done | That class's own counts, same attribute pattern as the main device — including any virtual reminder assigned to this class, bucketed by due date/done state the same way a real assignment already is |
| Assignments (calendar) | That class's due-soon + ahead + overdue + done assignments, plus any virtual reminder assigned to it, as calendar events — each due date/time becomes a 30-minute event. An overdue item's title gets an "(overdue)" tag, and a done one gets "(done)" — e.g. "Lab report (overdue)" — since due-soon/ahead don't need one (the due date alone already says when those are), but overdue is worth calling out plainly, and done would otherwise look identical to an undone item sharing the same due date. A virtual reminder gets the same tags: "(done)" if marked done, "(overdue)" if its due date has passed. Overdue/done items don't show up as the calendar's "next" event, they're just present when the range covers them. Each event's description has the same details as a to-do item |

A virtual reminder due within a week counts as "due soon", further out
as "ahead" — the same 7-day cutoff ClassDash uses for real assignments,
since `/api/virtual` doesn't say which bucket a reminder would land in
itself (only real assignments get that split server-side). An undated
reminder that isn't marked done doesn't count toward any of the four —
same as a real assignment, which never shows up in these buckets without
a due date either.

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
| `classdash.hide` | Dismisses an assignment or a virtual reminder — hidden ones don't count or show up anywhere in this integration |
| `classdash.unhide` | Reverses `hide` |
| `classdash.mute` | Marks "not urgent" — still counts, just doesn't badge/notify |
| `classdash.unmute` | Reverses `mute` |

Each takes an `id` (find it in the assignment's own list attribute on
any of the Due soon/Overdue/Ahead/Done sensors; a virtual reminder's id starts with `v-`, and hide/unhide send those to ClassDash's reminder endpoints automatically) and an optional
`config_entry_id`, only needed if more than one ClassDash server is
configured. These act on an assignment *id*, not an entity — an
assignment isn't its own HA entity, it's a list item inside a sensor's
attribute, so there's no natural entity for a hide/mute button to
attach to.

Three more create, edit and delete virtual reminders — the same thing doing it in ClassDash's own Reminders section does:

| Service | What it does |
|---|---|
| `classdash.create_virtual_reminder` | Adds a reminder — `title` required, `class`/`due` both optional. Returns the created reminder, including its `id` |
| `classdash.edit_virtual_reminder` | Changes an existing reminder's `title`/`class`/`due`, given its `id` |
| `classdash.delete_virtual_reminder` | Deletes a reminder for good, given its `id`. This is the one write with no undo; `classdash.hide` keeps it instead. An assignment's id is refused, since ClassDash can't delete those |

`title`/`class`/`due` are always sent together as a full replacement on
`edit_virtual_reminder`, not merged — leaving `class` or `due` blank
*clears* it, the same way ClassDash's own edit form always overwrites
all three rather than diffing against what's there. A reminder's `id` comes back in `create_virtual_reminder`'s response (call it with "Response variable" set, in a script or automation, to capture it), and also shows up in the Due soon/Overdue/Ahead/Done sensors' list attributes once it lands in one of them.

`class` is a device picker, not free text — it only offers ClassDash's
own class devices, so a typo can't silently create a permanent phantom
class that never goes stale. A class with genuinely nothing due/announced
yet (and no existing device) isn't selectable until it has one; that's
the same "a class only gets a device once something's actually due or
announced for it" rule everything else in this integration already
follows.

## What's read-only here

Pushing a ClassDash setting from Home Assistant (`/api/settings`) isn't built, and neither is linking or unlinking assignments (`/api/link`, `/api/unlink`). Same for
ClassDash's own `dismissedVersion` on the update check — Home
Assistant's own "Skip" button is entirely local to Home Assistant and
doesn't call ClassDash's `/api/update-status/dismiss`, so dismissing
there doesn't quiet ClassDash's own update banner.

## Diagnostics

There's no "Download diagnostics" on purpose. Class names can include a teacher's full name, and the student is usually a minor, sometimes as young as 6, while diagnostics files tend to get posted publicly in bug reports. For a bug report, Home Assistant's own logs (with ClassDash set to debug) are the way to go, and they're worth reading before posting too.

## License

GPL-3.0, matching ClassDash itself — see [LICENSE](LICENSE).
