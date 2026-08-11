# Changelog

All notable changes to this project will be documented here.

The project uses semantic versioning once the public interfaces stabilize.
Until then, alpha releases may contain breaking configuration changes.

## Unreleased

## 0.1.0-alpha.2 - 2026-08-11

- Base recurring episode-rating refresh tiers only on air date.
- Keep one immediate first lookup for episodes discovered after initial setup.
- Publish multi-architecture listener and scoped-Kometa images on GHCR.
- Make the standard Compose installation pull version-pinned release images.
- Retain reproducible local image builds through `compose.build.yml`.
- Update GitHub Actions to the current major action releases.

## 0.1.0-alpha.1 - 2026-08-11

- Add scheduled IMDb episode rating refresh tiers.
- Add authenticated Sonarr, Radarr, and Tautulli webhook ingestion.
- Add exact Plex rating-key scoping for incremental Kometa overlay runs.
- Add persistent event queues, generation handoff, health checks, dry-run mode,
  and episode-rating rollback snapshots.
- Pin and document compatibility with Kometa 2.4.6.
