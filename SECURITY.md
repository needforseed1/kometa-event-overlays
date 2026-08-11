# Security

## Supported releases

This repository is an experimental public alpha. Only the latest tagged alpha
release receives fixes.

## Secrets

The following values are secrets and must never be committed or posted in an
issue:

- Plex token
- TMDb API key
- Generated webhook token
- Any copied Arr or Tautulli credentials

`.env` and runtime state are ignored by Git. This protection does not help if a
secret is pasted into another tracked file.

Before publishing logs, remove URLs, tokens, media paths, usernames, and public
hostnames. Prefer counts and status JSON with secrets omitted.

## Webhook exposure

Port 8788 is exposed only to the configured Docker network by default. Do not
publish it to the internet. Every POST requires `X-Kometa-Token`, but the token
is defense in depth rather than a reason to expose the service publicly.

If a sender cannot join the Docker network, bind the port only to a trusted
interface and enforce firewall rules. TLS termination and authentication should
be added before any traffic crosses an untrusted network.

## Container restrictions

Both services run as a non-root user with a read-only root filesystem, dropped
capabilities, and `no-new-privileges`. Only their state volumes and temporary
filesystems are writable.

## Reporting a vulnerability

Do not include live credentials in a public issue. Use the repository host's
private security-reporting mechanism when available. If private reporting is
not configured, open a minimal issue asking the maintainer for a private
contact channel without describing an exploitable secret or token.
