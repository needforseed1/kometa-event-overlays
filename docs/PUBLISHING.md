# Publishing an experimental release

Release tags automatically build and publish these packages for Linux AMD64 and
ARM64:

- `ghcr.io/needforseed1/kometa-event-sync`
- `ghcr.io/needforseed1/kometa-event-overlays`

The workflow publishes the full semantic version and commit-SHA tags. Stable
semantic versions also receive `latest`; prereleases do not. `compose.yml`
always uses a full version instead of `latest`.

## Before publishing

1. Run the automated tests, Compose validation, and both image builds.
2. Search the full Git history and working tree for credentials, private URLs,
   media paths, usernames, hostnames, and real library names.
3. Confirm `.env`, runtime JSON, SQLite files, and IMDb datasets are untracked.
4. Review the pinned Kometa version and `COMPATIBILITY.md`.
5. Keep the release marked experimental or alpha until other installations
   have passed the canary matrix.

## Release procedure

1. Move the Unreleased changelog entries under a dated version heading.
2. Put the same version in `VERSION` and both image references in `compose.yml`.
3. Run the development checks and merge through CI.
4. Create the matching `v`-prefixed tag:

```bash
git tag -a v0.1.0-alpha.2 -m "Experimental public alpha 2"
git push origin v0.1.0-alpha.2
```

5. Wait for the **Publish container images** workflow to finish.
6. On the first publication of each package, set its visibility to public in
   the GitHub package settings. Later versions inherit package visibility.
7. Verify both version tags can be inspected and pulled without credentials.

Use the changelog as the GitHub release-note base and prominently repeat the
Kometa 2.4.6 compatibility constraint, IMDb non-commercial-use requirement,
and dry-run-first installation path.
