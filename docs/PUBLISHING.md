# Publishing an experimental release

Publish this as its own repository. Do not graft it onto a private Compose
repository: that can expose unrelated history even if the current files look
clean.

## Before publishing

1. Run the automated tests, Compose validation, and both image builds.
2. Search the full Git history and working tree for credentials, private URLs,
   media paths, usernames, hostnames, and real library names.
3. Confirm `.env`, runtime JSON, SQLite files, and IMDb datasets are untracked.
4. Review the pinned Kometa version and `COMPATIBILITY.md`.
5. Keep the release marked experimental or alpha until other installations
   have passed the canary matrix.

## Add a remote

Create an empty repository on the chosen Git host. Do not let the host create a
README or license, because both already exist here. Then run:

```bash
git remote add origin <repository-url>
git push -u origin main
```

Create a prerelease tag only after the pushed commit passes CI:

```bash
git tag -a v0.1.0-alpha.1 -m "Experimental public alpha"
git push origin v0.1.0-alpha.1
```

Use the changelog as the release-note base and prominently repeat the Kometa
2.4.6 compatibility constraint, IMDb non-commercial-use requirement, and
dry-run-first installation path.
