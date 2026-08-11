# Installation and manual integrations

## 1. Configure the project

Copy `.env.example` to `.env` and provide:

- `PLEX_URL`
- `PLEX_TOKEN`
- `TMDB_API_KEY`
- comma-separated `TV_LIBRARIES`
- comma-separated `MOVIE_LIBRARIES`, or an empty value

Edit `config/config.yml`. Every library key must exactly match both Plex and the
corresponding environment list. Copy the supplied TV or movie block when more
libraries are needed.

The project intentionally has no Arr URLs or API keys. It receives outbound
webhooks and never calls Arr APIs.

## 2. Networking

Put these containers on the same Docker network as the rest of your media
stack. Set `networks.media.name` in `compose.yml` to that network's name.
Webhook senders can then use the hostname `kometa-imdb-sync`.

Plex does not have to share this network if `PLEX_URL` is otherwise reachable.

The Compose file does not publish port 8788 on the host. If applications run on
another host, add a narrowly bound port mapping and firewall it. Do not expose
the listener directly to the internet.

## 3. Start dry-run mode

Keep `IMDB_SYNC_DRY_RUN=true` initially:

```bash
docker compose build
docker compose up -d
docker compose ps
```

Retrieve the token:

```bash
docker compose exec kometa-imdb-sync cat /data/webhook-token
```

## 4. Radarr

Add a Webhook connection manually:

- Name: `Kometa Scoped Overlays`
- URL: `http://kometa-imdb-sync:8788/webhook/radarr`
- Method: `POST`
- Triggers: Download/Import and Upgrade
- Header key: `X-Kometa-Token`
- Header value: the generated token

Run Radarr's built-in connection test. Test payloads are authenticated but do
not create overlay work.

## 5. Sonarr

Add a Webhook connection manually:

- Name: `Kometa Scoped Overlays`
- URL: `http://kometa-imdb-sync:8788/webhook/sonarr`
- Method: `POST`
- Triggers: Download/Import and Upgrade
- Header key: `X-Kometa-Token`
- Header value: the generated token

For a second Anime Sonarr instance, use:

```text
http://kometa-imdb-sync:8788/webhook/sonarr-anime
```

Both Sonarr endpoints use the same configured `TV_LIBRARIES` allowlist.

## 6. Tautulli

Add a Webhook notification agent manually:

- Friendly name: `Kometa New Media`
- URL: `http://kometa-imdb-sync:8788/webhook/tautulli`
- Method: `POST`
- Enabled trigger: Recently Added only

Set the webhook's custom headers JSON to:

```json
{"X-Kometa-Token":"REPLACE_WITH_GENERATED_TOKEN"}
```

Use this notification body:

```json
{
  "action": "created",
  "rating_key": "{rating_key}",
  "media_type": "{media_type}",
  "library_name": "{library_name}"
}
```

Tautulli covers items added outside Servarr. If Sonarr or Radarr already caused
an overlay, the later Recently Added event is acknowledged without repeating
the poster run.

## 7. Activate writes

Review dry-run logs and confirm the expected libraries and counts. Change:

```text
IMDB_SYNC_DRY_RUN=false
```

Then recreate the listener:

```bash
docker compose up -d --force-recreate kometa-imdb-sync
```

Follow `TESTING.md` for controlled canaries before relying on automation.
