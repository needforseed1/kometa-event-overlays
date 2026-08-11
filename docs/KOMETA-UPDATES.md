# Updating Kometa

The scoped image is coupled to Kometa's internal overlay implementation. Treat
every Kometa update as a manual compatibility change.

## Procedure

1. Create a branch.
2. Update the base image in `scoped-overlay/Dockerfile`.
3. Bump `VERSION` and both GHCR image tags in `compose.yml`.
4. Build without deploying:

   ```bash
   docker compose -f compose.yml -f compose.build.yml build --no-cache kometa-incremental
   ```

5. If the build reports an expected source block count other than one, stop.
   Compare the new upstream `modules/overlays.py` with the pinned version and
   adapt every patch anchor while preserving its guard.
6. Run all unit tests and Compose validation.
7. Run the canary matrix in `TESTING.md`.
8. Update `COMPATIBILITY.md` only after the canaries pass.

## Required canaries

- invalid scope fails closed
- stale scope processes nothing
- empty scope does not launch Kometa poster work
- one episode
- one movie
- file-size-matched movie upgrade
- deliberately mismatched file size remains queued
- season batch becomes one generation
- late Tautulli notification does not duplicate Servarr work

## Rollback

Stop the companion services, restore the prior published image pin, and start
them again. A separate normal Kometa container is unaffected.

Do not configure Renovate or Dependabot to automatically merge Kometa base
image changes.
