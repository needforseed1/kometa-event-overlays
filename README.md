# Kometa Event Overlays

Experimental companion containers for event-driven, Plex-rating-key-scoped
Kometa overlays and scheduled IMDb episode rating refreshes.

This project does not replace a normal Kometa installation. Keep normal Kometa
for collections, metadata, and periodic full reconciliation. These containers
handle small, immediate overlay runs when Sonarr, Radarr, or Tautulli reports
new or upgraded media.

> Public alpha: Plex only, tested against Kometa 2.4.6. The scoped image patches
> an internal Kometa module and must be reviewed for every Kometa update.

This is an unofficial community project. It is not affiliated with or endorsed
by Kometa, Plex, IMDb, Radarr, Sonarr, Tautulli, TMDb, or LinuxServer.io.

## Why

A normal Kometa overlay run is library-oriented. Running it after every import
can be expensive, while waiting for a daily or weekly pass leaves new episodes
without ratings and overlays.

This project adds a fail-closed scope file containing exact Plex rating keys.
Only those keys are eligible for poster work.

```text
Sonarr / Radarr / Tautulli
           |
           | authenticated webhook
           v
   kometa-imdb-sync
   - persistent queue
   - 90-second debounce
   - IMDb episode ratings
   - Plex/Servarr file check
           |
           | one atomic scope generation
           v
   kometa-incremental
   - immutable scope snapshot
   - Kometa --overlays-only
           |
           v
          Plex
```

## Features

- IMDb episode audience ratings from IMDb's daily non-commercial datasets.
- Daily, weekly, and monthly episode refresh tiers with a six-hour scheduler.
- Immediate Sonarr episode import and upgrade events.
- Immediate Radarr movie import and upgrade events.
- Tautulli Recently Added support for media added outside Servarr.
- Persistent, deduplicated event queue with retries.
- Season-wide debounce: many episode webhooks become one Kometa run.
- Plex media-part size verification before processing Servarr upgrades.
- Cross-source suppression when Tautulli reports an item already overlaid by
  Sonarr or Radarr.
- Exact library/rating-key overlay allowlist.
- Dry-run default and metadata rollback snapshots.
- No published webhook port by default.

## What it does not do

- It does not discover or configure Plex, Radarr, Sonarr, or Tautulli.
- It does not require or store Arr API keys.
- It does not replace normal full Kometa runs.
- It does not distribute IMDb datasets.
- It does not promise compatibility with untested Kometa versions.

Users explicitly configure Plex and library names in `.env` and
`config/config.yml`, then manually add webhook connections in their own Arr and
Tautulli applications.

## Requirements

- Docker Engine with Compose v2.
- Plex.
- A normal Kometa installation for periodic full reconciliation.
- TMDb API key for episode-ID fallbacks.
- Sonarr/Radarr and/or Tautulli if event-driven updates are desired.
- One existing Docker network reachable by the webhook senders.

The Plex server itself may be reachable by any valid `PLEX_URL`; it does not
need to be a container on the shared webhook network.

## Quick start

1. Copy and edit the environment file:

   ```bash
   cp .env.example .env
   $EDITOR .env
   ```

2. Edit `config/config.yml` so every library key exactly matches Plex. The TV
   library names must also match `TV_LIBRARIES`; movie names must match
   `MOVIE_LIBRARIES`.

3. At the bottom of `compose.yml`, set `networks.media.name` to an existing
   Docker network shared with the containers that will send webhooks:

   ```yaml
   networks:
     media:
       external: true
       name: media
   ```

   Replace the final `media` with the actual network name. If you need to
   create that network first, run:

   ```bash
   docker network create media
   ```

   Do not run that command if the chosen network already exists. Also attach
   Sonarr, Radarr, and Tautulli to it in their own Compose files.

4. Build and start in dry-run mode:

   ```bash
   docker compose build
   docker compose up -d
   docker compose ps
   ```

5. Retrieve the generated webhook token:

   ```bash
   docker compose exec kometa-imdb-sync cat /data/webhook-token
   ```

   Treat this token like a password. Add it to webhook headers, but never post
   it in an issue or log excerpt.

6. Add the webhooks manually using [docs/INSTALLATION.md](docs/INSTALLATION.md).

7. Review dry-run logs and status files. Activate writes only when the detected
   libraries and target counts are correct:

   ```bash
   # Set IMDB_SYNC_DRY_RUN=false in .env, then recreate the listener.
   docker compose up -d --force-recreate kometa-imdb-sync
   ```

## Services

### `kometa-imdb-sync`

Maintains the episode inventory and IMDb rating state, receives authenticated
webhooks on port 8788, verifies that Plex has indexed Servarr files, and
publishes the final scope.

State is stored in the `imdb-sync-data` named volume. The listener first writes
`overlay-scope-staging.json`; only the final `overlay-scope.json` is watched by
the worker.

### `kometa-incremental`

Builds from `lscr.io/linuxserver/kometa:2.4.6`, applies the scoped-overlay patch,
and watches for scope generations. It invokes Kometa with `--overlays-only` and
records completed generations in the `incremental-config` volume.

The regular Kometa container is intentionally outside this Compose project.

## Useful commands

```bash
docker compose ps
docker compose logs --tail 100 kometa-imdb-sync
docker compose logs --tail 100 kometa-incremental
docker compose exec kometa-imdb-sync python /app/event_daemon.py --event-healthcheck
docker compose exec kometa-incremental python3 /app/overlay-worker.py --healthcheck
```

Preview and perform restoration of episode audience ratings managed by this
project:

```bash
docker compose run --rm kometa-imdb-sync --rollback
docker compose run --rm kometa-imdb-sync --rollback --write
```

## Safe shutdown

```bash
docker compose stop kometa-imdb-sync kometa-incremental
```

This does not affect a separate normal Kometa installation. Webhook connections
can be disabled independently in each sender.

## Documentation

- [Installation and manual webhook setup](docs/INSTALLATION.md)
- [Architecture and safety model](docs/ARCHITECTURE.md)
- [Kometa update procedure](docs/KOMETA-UPDATES.md)
- [Testing and canaries](docs/TESTING.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Publishing an experimental release](docs/PUBLISHING.md)
- [Compatibility](COMPATIBILITY.md)
- [Security](SECURITY.md)

## IMDb data terms

No IMDb data is included in this repository. IMDb permits limited personal,
non-commercial use of its published datasets subject to its conditions. Users
are responsible for confirming that their use is permitted.

Information courtesy of IMDb (<https://www.imdb.com>). Used with permission.

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and IMDb's current terms.

## License

Project code is released under the MIT License. Kometa and other third-party
notices are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
