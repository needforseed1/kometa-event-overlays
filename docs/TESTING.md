# Testing

## Automated tests

```bash
python3 -m unittest discover -s imdb-sync -p 'test_*.py'
python3 -m unittest discover -s scoped-overlay -p 'test_*.py'
python3 -m py_compile imdb-sync/*.py scoped-overlay/*.py
docker compose config --quiet
```

Building `kometa-incremental` is also a compatibility test because the source
patch refuses unexpected upstream code.

## Dry-run gate

Start with `IMDB_SYNC_DRY_RUN=true`. Confirm:

- expected Plex libraries are found
- episode-ID mapping coverage is reasonable
- no Plex metadata writes occur
- dry-run scopes are consumed without poster writes

## Live canaries

Activate writes only after the dry run is understood. Test in this order:

1. One recently added episode.
2. One movie through Tautulli Recently Added.
3. One Radarr movie upgrade.
4. One full season import.

For every canary verify:

- accepted event count
- exact `libraries` counts in `overlay-scope.json`
- expected `reason_counts`
- `pending-events.json` returns to an empty list
- worker status generation matches the scope
- selected Plex items have Kometa's `Overlay` label
- no unrelated rating keys appear

## Restart test

Restart the listener with an event pending. The queue must survive and process
after its debounce/retry time. Restart the worker after a completed generation;
it must not repeat that generation.

Never use personal credentials in public CI or committed fixtures.
