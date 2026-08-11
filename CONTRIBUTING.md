# Contributing

Contributions and compatibility reports are welcome.

## Development checks

Run before opening a pull request:

```bash
python3 -m unittest discover -s imdb-sync -p 'test_*.py'
python3 -m unittest discover -s scoped-overlay -p 'test_*.py'
python3 -m py_compile imdb-sync/*.py scoped-overlay/*.py
docker compose config --quiet
docker compose -f compose.yml -f compose.build.yml build
```

Use `.env.example` values or disposable test credentials. Never run public CI
against a personal Plex server.

## Pull requests

- Keep changes focused.
- Add tests for event normalization, scope safety, or worker behavior.
- Update `COMPATIBILITY.md` when testing new application versions.
- Preserve fail-closed behavior.
- Never weaken the exact Kometa patch-anchor checks to make a build pass.
- Do not commit downloaded IMDb datasets or fixtures derived from real private
  libraries.

## Bug reports

Include component versions, expected scope counts, sanitized reason counts,
worker result, and whether the queue returned to zero. Do not include tokens or
full media paths.
