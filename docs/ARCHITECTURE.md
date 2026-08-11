# Architecture and safety model

## Event queue

The listener accepts authenticated JSON from known source paths, normalizes it
to media identifiers, and persists it in `pending-events.json` before returning
HTTP 202.

Events are deduplicated by stable media identity. A global debounce combines a
season import into one cycle. Unresolved items retry because Plex may not have
indexed the new media yet.

## Upgrade verification

Servarr Download/Upgrade payloads include the imported file size. The resolver
compares it with the Plex `Media/Part` size. A movie or episode is not released
for poster work while Plex still reports the previous file.

## IMDb schedule

Episode audience ratings are refreshed according to age and state:

- missing, unmapped, or failed episodes: every six hours
- downloaded within 7 days, or aired within 30 days: every 24 hours
- aired 31–180 days ago: every 7 days
- aired more than 180 days ago, or with no air date: every 30 days

The scheduler wakes every six hours to select due episodes; it does not refresh
the whole library every six hours. A Sonarr import triggers an additional cycle
so a newly downloaded episode does not wait for the scheduler.

On first startup, old weekly and monthly items are inventoried and deferred for
one normal interval instead of causing a full-library bootstrap sweep.

## Single scope publication

Rating synchronization writes `overlay-scope-staging.json`. The worker never
watches this file. After ratings and events are resolved, the daemon atomically
publishes one `overlay-scope.json` generation.

This avoids separate Kometa runs for an intermediate rating scope and the final
event-merged scope.

## Scoped Kometa patch

The derived image patches `modules/overlays.py` to:

1. Validate the scope timestamp and library allowlist.
2. Restrict Plex builder IDs.
3. Filter compiled overlay assignments.
4. Restrict existing-overlay cleanup.
5. Prevent dry-run writes.

Missing or invalid scopes abort. Stale or empty scopes process nothing. A
trusted manual scope with `full: true` is the only escape hatch.

## Immutable worker snapshot

The worker copies each generation to `overlay-scope-current.json` before
starting Kometa. Events published during a run cannot change the active target
set; they become a later generation.

Completed generation IDs are persisted so container restarts do not repeat
finished work.

## Reconciliation

This project deliberately retains two fallback paths:

- the six-hour rating inventory catches missed webhooks
- the user's separate normal Kometa installation performs periodic full overlay
  reconciliation
