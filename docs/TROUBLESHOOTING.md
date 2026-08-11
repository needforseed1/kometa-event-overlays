# Troubleshooting

## Event accepted but no poster yet

Check both logs:

```bash
docker compose logs --tail 100 kometa-imdb-sync
docker compose logs --tail 100 kometa-incremental
```

The normal delay is the configured debounce plus Kometa startup and overlay
compilation. Poster writes occur near the end of the Kometa run.

## Events remain queued

Common causes:

- Plex has not scanned the item.
- Plex still reports the old file size after an upgrade.
- TVDb/TMDb identifiers do not match Plex.
- The library is not present in the configured allowlists.

Inspect non-secret event metadata and retry counts. Never paste the complete
queue into a public issue without checking it for private paths.

## Webhook test fails

- Confirm sender and listener share `MEDIA_NETWORK`.
- Resolve `kometa-imdb-sync` from the sender container.
- Confirm port 8788 is reachable.
- Confirm the `X-Kometa-Token` header matches `/data/webhook-token`.
- Use the correct source path.

## Worker does not start

The worker waits for a healthy listener and a readable final scope. Check:

```bash
docker compose ps
docker compose exec kometa-imdb-sync python /app/event_daemon.py --event-healthcheck
```

## Patch build fails

An error about an expected source block means the pinned Kometa internals have
changed. This is intentional. Follow `KOMETA-UPDATES.md`; do not bypass the
anchor-count checks.

## Duplicate Tautulli event

Tautulli can report Recently Added after Servarr has already finished. Items
with Kometa's Overlay label are acknowledged without another poster run. If a
duplicate run occurs, report sanitized scope generations, reason counts, and
component versions.
