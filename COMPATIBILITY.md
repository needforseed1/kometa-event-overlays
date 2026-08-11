# Compatibility

## Tested versions

| Component | Tested version |
|---|---:|
| Kometa base image | LinuxServer Kometa 2.4.6 |
| Plex Media Server | 1.43.3 |
| Tautulli | 2.17.2 |
| Radarr | 6.3.0 |
| Sonarr | 4.0.19 |
| Python sync image | 3.14 Alpine |

Other versions may work but are not yet part of the compatibility claim.

## Kometa version coupling

`scoped-overlay/patch_overlay_scope.py` patches exact source blocks in Kometa's
internal `modules/overlays.py`. The build requires each expected block to occur
exactly once and stops if upstream code has changed.

Do not change the base-image tag without following `docs/KOMETA-UPDATES.md`.
Kometa updates must not be automatically merged or deployed.

## Platform assumptions

- Plex library metadata is available through the Plex XML API.
- Sonarr and Radarr send their standard Webhook Download/Upgrade payloads.
- Webhook senders can resolve and reach `kometa-imdb-sync:8788` on the shared
  Docker network.
- Library names in `.env` exactly match Plex and `config/config.yml`.

## Known limitations

- The scoped Kometa process still initializes configured libraries and compiles
  overlay definitions; only the actual poster targets are restricted.
- IMDb datasets are daily rather than real-time.
- Only Plex is implemented.
- The supplied overlay config contains example TV and movie libraries and must
  be edited for other names or library types.
- Tautulli is optional, but without it media added outside Servarr will wait for
  the six-hour inventory fallback.
