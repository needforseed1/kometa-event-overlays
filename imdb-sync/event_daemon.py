#!/usr/bin/env python3
"""Event-aware scheduler for IMDb synchronization and scoped Kometa overlays."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import signal
import sys
import threading
import time
import urllib.parse
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import imdb_episode_ratings as ratings


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def as_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def normalize_event(source: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert Tautulli/Servarr webhook payloads to persistent targets."""
    source = source.lower()
    targets: list[dict[str, Any]] = []
    event_type = str(payload.get("eventType") or payload.get("action") or "unknown").lower()

    if source == "radarr":
        if event_type == "test":
            return []
        movie = payload.get("movie") if isinstance(payload.get("movie"), dict) else {}
        movie_file = (
            payload.get("movieFile") if isinstance(payload.get("movieFile"), dict) else {}
        )
        tmdb_id = as_int(movie.get("tmdbId"))
        if tmdb_id:
            targets.append(
                {
                    "source": source,
                    "event_type": event_type,
                    "media_type": "movie",
                    "tmdb_id": tmdb_id,
                    "expected_size": as_int(movie_file.get("size")),
                    "upgrade": as_bool(payload.get("isUpgrade")),
                }
            )
    elif source in {"sonarr", "sonarr-anime"}:
        if event_type == "test":
            return []
        series = payload.get("series") if isinstance(payload.get("series"), dict) else {}
        episode_file = (
            payload.get("episodeFile") if isinstance(payload.get("episodeFile"), dict) else {}
        )
        episodes = payload.get("episodes") if isinstance(payload.get("episodes"), list) else []
        for episode in episodes:
            if not isinstance(episode, dict):
                continue
            target = {
                "source": source,
                "event_type": event_type,
                "media_type": "episode",
                "tvdb_id": as_int(episode.get("tvdbId")),
                "series_tvdb_id": as_int(series.get("tvdbId")),
                "season_number": as_int(episode.get("seasonNumber")),
                "episode_number": as_int(episode.get("episodeNumber")),
                "expected_size": as_int(episode_file.get("size")),
                "upgrade": as_bool(payload.get("isUpgrade")),
            }
            if target["tvdb_id"] or (
                target["series_tvdb_id"]
                and target["season_number"] is not None
                and target["episode_number"] is not None
            ):
                targets.append(target)
    elif source == "tautulli":
        rating_key = as_int(payload.get("rating_key"))
        media_type = str(payload.get("media_type") or "").lower()
        library = str(payload.get("library_name") or payload.get("library") or "").strip()
        if rating_key:
            targets.append(
                {
                    "source": source,
                    "event_type": event_type,
                    "media_type": media_type,
                    "rating_key": rating_key,
                    "library": library,
                    "upgrade": False,
                }
            )
    elif source == "generic":
        rating_key = as_int(payload.get("rating_key"))
        tmdb_id = as_int(payload.get("tmdb_id"))
        tvdb_id = as_int(payload.get("tvdb_id"))
        if rating_key or tmdb_id or tvdb_id:
            targets.append(
                {
                    "source": source,
                    "event_type": event_type,
                    "media_type": str(payload.get("media_type") or "").lower(),
                    "rating_key": rating_key,
                    "tmdb_id": tmdb_id,
                    "tvdb_id": tvdb_id,
                    "expected_size": as_int(payload.get("expected_size")),
                    "library": str(payload.get("library") or "").strip(),
                    "upgrade": as_bool(payload.get("upgrade")),
                }
            )
    return targets


def event_key(event: dict[str, Any]) -> str:
    identity = {
        key: event.get(key)
        for key in (
            "source",
            "media_type",
            "rating_key",
            "tmdb_id",
            "tvdb_id",
            "series_tvdb_id",
            "season_number",
            "episode_number",
        )
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]


class EventQueue:
    def __init__(self, path: Path, debounce_seconds: float, retry_seconds: float):
        self.path = path
        self.debounce_seconds = debounce_seconds
        self.retry_seconds = retry_seconds
        self.lock = threading.Lock()
        self.wake = threading.Event()

    def _read_unlocked(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, list) else []
        except (OSError, ValueError, json.JSONDecodeError):
            return []

    def _write_unlocked(self, events: list[dict[str, Any]]) -> None:
        atomic_json(self.path, events)

    def add(self, targets: list[dict[str, Any]]) -> int:
        if not targets:
            return 0
        timestamp = time.time()
        ready_at = timestamp + self.debounce_seconds
        with self.lock:
            events = self._read_unlocked()
            by_key = {event["key"]: event for event in events if event.get("key")}
            for target in targets:
                key = event_key(target)
                existing = by_key.get(key, {})
                target.update(
                    {
                        "id": existing.get("id", uuid.uuid4().hex),
                        "key": key,
                        "received_at": timestamp,
                        "ready_at": ready_at,
                        "attempts": int(existing.get("attempts", 0)),
                    }
                )
                by_key[key] = target
            # Debounce the whole batch so a season import becomes one Kometa run.
            for event in by_key.values():
                event["ready_at"] = max(float(event.get("ready_at", 0)), ready_at)
            self._write_unlocked(sorted(by_key.values(), key=lambda item: item["key"]))
        self.wake.set()
        return len(targets)

    def ready(self) -> list[dict[str, Any]]:
        timestamp = time.time()
        with self.lock:
            return [event for event in self._read_unlocked() if float(event.get("ready_at", 0)) <= timestamp]

    def seconds_until_ready(self) -> float | None:
        with self.lock:
            events = self._read_unlocked()
        if not events:
            return None
        return max(0.0, min(float(event.get("ready_at", 0)) for event in events) - time.time())

    def complete(self, resolved_ids: set[str], unresolved_ids: set[str]) -> None:
        timestamp = time.time()
        with self.lock:
            output: list[dict[str, Any]] = []
            for event in self._read_unlocked():
                event_id = str(event.get("id"))
                if event_id in resolved_ids:
                    continue
                if event_id in unresolved_ids:
                    event["attempts"] = int(event.get("attempts", 0)) + 1
                    event["ready_at"] = timestamp + self.retry_seconds
                output.append(event)
            self._write_unlocked(output)
        self.wake.set()


@dataclass(frozen=True)
class EventConfig:
    host: str
    port: int
    token: str
    debounce_seconds: float
    retry_seconds: float
    max_attempts: int
    movie_libraries: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "EventConfig":
        return cls(
            host=os.environ.get("IMDB_SYNC_WEBHOOK_HOST", "0.0.0.0"),
            port=int(os.environ.get("IMDB_SYNC_WEBHOOK_PORT", "8788")),
            token=os.environ.get("IMDB_SYNC_WEBHOOK_TOKEN", ""),
            debounce_seconds=float(os.environ.get("IMDB_SYNC_EVENT_DEBOUNCE_SECONDS", "90")),
            retry_seconds=float(os.environ.get("IMDB_SYNC_EVENT_RETRY_SECONDS", "120")),
            max_attempts=int(os.environ.get("IMDB_SYNC_EVENT_MAX_ATTEMPTS", "10")),
            movie_libraries=tuple(
                part.strip()
                for part in os.environ.get("IMDB_SYNC_MOVIE_LIBRARIES", "").split(",")
                if part.strip()
            ),
        )

    def validate(self) -> None:
        if not self.token:
            raise ValueError("IMDB_SYNC_WEBHOOK_TOKEN is required")
        if not (1 <= self.port <= 65535):
            raise ValueError("IMDB_SYNC_WEBHOOK_PORT is invalid")
        if self.debounce_seconds < 0 or self.retry_seconds <= 0 or self.max_attempts < 1:
            raise ValueError("event timing and retry values must be positive")


def load_or_create_token(state_dir: Path, configured: str) -> str:
    if configured:
        return configured
    path = state_dir / "webhook-token"
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if not token:
        token = secrets.token_urlsafe(32)
        path.write_text(token + "\n", encoding="utf-8")
        path.chmod(0o600)
    return token


class TargetResolver:
    def __init__(self, config: ratings.Config, event_config: EventConfig):
        self.config = config
        self.event_config = event_config
        self.plex = ratings.PlexClient(
            config.plex_url, config.plex_token, config.timeout, config.page_size
        )

    def sections(self) -> dict[str, tuple[str, str]]:
        root = self.plex._xml("/library/sections")
        return {
            node.attrib["title"]: (node.attrib["key"], node.attrib.get("type", ""))
            for node in root.findall("Directory")
            if node.attrib.get("title") and node.attrib.get("key")
        }

    def media_file_is_current(self, rating_key: int, event: dict[str, Any]) -> bool:
        """Confirm Plex has indexed the exact Servarr import before rendering."""
        expected_size = as_int(event.get("expected_size"))
        if expected_size is None:
            return True
        root = self.plex._xml(f"/library/metadata/{rating_key}")
        sizes = {
            size
            for part in root.findall("Video/Media/Part")
            if (size := as_int(part.attrib.get("size"))) is not None
        }
        return expected_size in sizes

    def direct_target(self, event: dict[str, Any]) -> tuple[str, set[int]] | None:
        rating_key = as_int(event.get("rating_key"))
        if rating_key is None:
            return None
        root = self.plex._xml(f"/library/metadata/{rating_key}", {"includeGuids": 1})
        node = root.find("Video")
        if node is None:
            node = root.find("Directory")
        if node is None:
            return None
        library = node.attrib.get("librarySectionTitle") or str(event.get("library") or "")
        media_type = node.attrib.get("type", "")
        allowed = set(self.config.libraries) | set(self.event_config.movie_libraries)
        if library not in allowed:
            return None
        if media_type in {"episode", "movie"}:
            if event.get("source") == "tautulli" and self.plex.item_has_overlay(rating_key):
                return library, set()
            return library, {rating_key}
        if media_type not in {"season", "show"}:
            return None
        child_path = "children" if media_type == "season" else "allLeaves"
        leaves = self.plex._xml(f"/library/metadata/{rating_key}/{child_path}")
        cutoff = float(event.get("received_at", time.time())) - 3600
        keys = {
            int(video.attrib["ratingKey"])
            for video in leaves.findall("Video")
            if video.attrib.get("ratingKey")
            and (media_type == "season" or float(video.attrib.get("addedAt", 0)) >= cutoff)
        }
        if event.get("source") == "tautulli":
            keys = {key for key in keys if not self.plex.item_has_overlay(key)}
        return library, keys

    def movie_targets(self, tmdb_ids: set[int]) -> dict[int, tuple[str, int]]:
        found: dict[int, tuple[str, int]] = {}
        if not tmdb_ids:
            return found
        sections = self.sections()
        for library in self.event_config.movie_libraries:
            section = sections.get(library)
            if not section or section[1] != "movie":
                continue
            offset = 0
            while len(found) < len(tmdb_ids):
                root = self.plex._xml(
                    f"/library/sections/{section[0]}/all",
                    {
                        "type": 1,
                        "includeGuids": 1,
                        "X-Plex-Container-Start": offset,
                        "X-Plex-Container-Size": self.config.page_size,
                    },
                )
                nodes = root.findall("Video")
                for node in nodes:
                    values = {guid.attrib.get("id", "") for guid in node.findall("Guid")}
                    for tmdb_id in tmdb_ids - set(found):
                        if f"tmdb://{tmdb_id}" in values:
                            found[tmdb_id] = (library, int(node.attrib["ratingKey"]))
                offset += len(nodes)
                total = int(root.attrib.get("totalSize", root.attrib.get("size", len(nodes))))
                if not nodes or offset >= total:
                    break
        return found

    def episode_targets(self, events: list[dict[str, Any]]) -> dict[str, tuple[str, int]]:
        output: dict[str, tuple[str, int]] = {}
        if not events:
            return output
        show_tvdb_cache: dict[int, int | None] = {}
        sections = self.sections()
        for library in self.config.libraries:
            section = sections.get(library)
            if not section or section[1] != "show":
                continue
            for item in self.plex.episodes(section[0], library):
                for event in events:
                    event_id = str(event["id"])
                    if event_id in output:
                        continue
                    tvdb_id = as_int(event.get("tvdb_id"))
                    if tvdb_id and item.tvdb_id == tvdb_id:
                        if self.media_file_is_current(item.rating_key, event):
                            output[event_id] = (library, item.rating_key)
                        continue
                    series_tvdb_id = as_int(event.get("series_tvdb_id"))
                    if not series_tvdb_id:
                        continue
                    if item.show_rating_key not in show_tvdb_cache:
                        root = self.plex._xml(
                            f"/library/metadata/{item.show_rating_key}", {"includeGuids": 1}
                        )
                        node = root.find("Directory")
                        values = [guid.attrib.get("id", "") for guid in node.findall("Guid")] if node is not None else []
                        show_tvdb_cache[item.show_rating_key] = next(
                            (
                                as_int(value.removeprefix("tvdb://"))
                                for value in values
                                if value.startswith("tvdb://")
                            ),
                            None,
                        )
                    if (
                        show_tvdb_cache[item.show_rating_key] == series_tvdb_id
                        and item.season_number == as_int(event.get("season_number"))
                        and item.episode_number == as_int(event.get("episode_number"))
                    ):
                        if self.media_file_is_current(item.rating_key, event):
                            output[event_id] = (library, item.rating_key)
                if len(output) == len(events):
                    return output
        return output

    def resolve(
        self, events: list[dict[str, Any]]
    ) -> tuple[dict[str, set[int]], set[str], set[str], Counter[str]]:
        scope: dict[str, set[int]] = defaultdict(set)
        resolved: set[str] = set()
        reasons: Counter[str] = Counter()

        for event in events:
            if event.get("rating_key"):
                try:
                    target = self.direct_target(event)
                except Exception:
                    target = None
                if target:
                    scope[target[0]].update(target[1])
                    resolved.add(str(event["id"]))
                    reasons[f"event-{event['source']}"] += len(target[1])

        movie_events = [event for event in events if event.get("tmdb_id")]
        try:
            movies = self.movie_targets({int(event["tmdb_id"]) for event in movie_events})
        except Exception:
            movies = {}
        for event in movie_events:
            target = movies.get(int(event["tmdb_id"]))
            try:
                current = bool(target and self.media_file_is_current(target[1], event))
            except Exception:
                current = False
            if target and current:
                scope[target[0]].add(target[1])
                resolved.add(str(event["id"]))
                reasons["event-radarr-upgrade" if event.get("upgrade") else "event-radarr-import"] += 1

        episode_events = [
            event
            for event in events
            if event.get("tvdb_id") or event.get("series_tvdb_id")
        ]
        try:
            episodes = self.episode_targets(episode_events)
        except Exception:
            episodes = {}
        for event in episode_events:
            target = episodes.get(str(event["id"]))
            if target:
                scope[target[0]].add(target[1])
                resolved.add(str(event["id"]))
                reasons["event-sonarr-upgrade" if event.get("upgrade") else "event-sonarr-import"] += 1

        unresolved = {str(event["id"]) for event in events} - resolved
        return scope, resolved, unresolved, reasons


def merge_scope(
    config: ratings.Config,
    event_scope: dict[str, set[int]],
    reasons: Counter[str],
    base_path: Path | None = None,
) -> int:
    path = config.state_dir / "overlay-scope.json"
    source = base_path or path
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        payload = {"libraries": {}, "reason_counts": {}}
    libraries = payload.setdefault("libraries", {})
    for library, keys in event_scope.items():
        libraries[library] = sorted({int(key) for key in libraries.get(library, [])} | keys)
    reason_counts = Counter(payload.get("reason_counts") or {})
    reason_counts.update(reasons)
    payload.update(
        {
            "generated_at": now_iso(),
            "generation": uuid.uuid4().hex,
            "dry_run": config.dry_run,
            "full": False,
            "libraries": libraries,
            "reason_counts": dict(sorted(reason_counts.items())),
        }
    )
    atomic_json(path, payload)
    return sum(len(keys) for keys in libraries.values())


class WebhookServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], queue: EventQueue, config: EventConfig):
        super().__init__(address, WebhookHandler)
        self.event_queue = queue
        self.event_config = config


class WebhookHandler(BaseHTTPRequestHandler):
    server: WebhookServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def response(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if urllib.parse.urlsplit(self.path).path == "/health":
            self.response(200, {"status": "ok"})
        else:
            self.response(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        source = parsed.path.removeprefix("/webhook/").strip("/").lower()
        query_token = urllib.parse.parse_qs(parsed.query).get("token", [""])[0]
        header_token = self.headers.get("X-Kometa-Token", "")
        supplied = header_token or query_token
        if not hmac.compare_digest(supplied, self.server.event_config.token):
            self.response(401, {"error": "unauthorized"})
            return
        if source not in {"radarr", "sonarr", "sonarr-anime", "tautulli", "generic"}:
            self.response(404, {"error": "unknown source"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 1024 * 1024:
                raise ValueError("invalid payload size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            targets = normalize_event(source, payload)
            accepted = self.server.event_queue.add(targets)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            self.response(400, {"error": str(exc)})
            return
        self.response(202, {"accepted": accepted})


def run_cycle(
    config: ratings.Config,
    event_config: EventConfig,
    queue: EventQueue,
    events: list[dict[str, Any]],
    sync_ratings: bool = True,
) -> None:
    staging_path = config.state_dir / "overlay-scope-staging.json"
    if sync_ratings:
        ratings.run_sync(config, full=False, scope_path=staging_path)
    else:
        # Movie imports and upgrades do not need a TV episode inventory. Start
        # from a fresh scope so completed work is never carried into this run.
        ratings.build_scope(
            config,
            defaultdict(set),
            Counter(),
            full=False,
            path=staging_path,
        )
    if not events:
        # Publish once after the ratings pass is complete. The staging file is
        # never watched by the overlay worker.
        merge_scope(config, {}, Counter(), base_path=staging_path)
        return
    resolver = TargetResolver(config, event_config)
    event_scope, resolved, unresolved, reasons = resolver.resolve(events)
    exhausted = {
        str(event["id"])
        for event in events
        if int(event.get("attempts", 0)) + 1 >= event_config.max_attempts
    }
    if exhausted:
        unresolved -= exhausted
        resolved |= exhausted
        reasons["event-expired"] += len(exhausted)
    count = merge_scope(config, event_scope, reasons, base_path=staging_path)
    queue.complete(resolved, unresolved)
    print(
        f"Event scope complete: events={len(events)}, resolved={len(resolved) - len(exhausted)}, "
        f"retry={len(unresolved)}, expired={len(exhausted)}, scope={count}",
        flush=True,
    )


def daemon() -> int:
    config = ratings.Config.from_env()
    config.validate()
    ready_path = config.state_dir / "event-daemon-ready.json"
    try:
        ready_path.unlink()
    except FileNotFoundError:
        pass
    event_config = EventConfig.from_env()
    event_config = replace(
        event_config,
        token=load_or_create_token(config.state_dir, event_config.token),
    )
    event_config.validate()
    queue = EventQueue(
        config.state_dir / "pending-events.json",
        event_config.debounce_seconds,
        event_config.retry_seconds,
    )
    server = WebhookServer((event_config.host, event_config.port), queue, event_config)
    server_thread = threading.Thread(target=server.serve_forever, name="webhook-server", daemon=True)
    server_thread.start()
    print(f"Event webhook listening on {event_config.host}:{event_config.port}", flush=True)

    stopping = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stopping.set()
        queue.wake.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    next_periodic = 0.0
    try:
        while not stopping.is_set():
            ready = queue.ready()
            periodic = time.monotonic() >= next_periodic
            if periodic or ready:
                started = time.monotonic()
                try:
                    needs_episode_sync = any(
                        event.get("tvdb_id")
                        or event.get("series_tvdb_id")
                        or event.get("media_type") in {"episode", "season", "show"}
                        for event in ready
                    )
                    run_cycle(
                        config,
                        event_config,
                        queue,
                        ready,
                        sync_ratings=periodic or needs_episode_sync,
                    )
                    if periodic:
                        atomic_json(
                            ready_path,
                            {"ready_at": now_iso(), "instance": uuid.uuid4().hex},
                        )
                except Exception as exc:
                    print(f"error: synchronization cycle failed: {exc}", file=sys.stderr, flush=True)
                    # Preserve the batch but back off after a transient Plex or
                    # dataset failure instead of immediately spinning on it.
                    if ready:
                        queue.complete(set(), {str(event["id"]) for event in ready})
                if periodic:
                    next_periodic = started + config.poll_hours * 3600
                continue
            seconds_to_event = queue.seconds_until_ready()
            timeout = max(0.25, next_periodic - time.monotonic())
            if seconds_to_event is not None:
                timeout = min(timeout, max(0.25, seconds_to_event))
            queue.wake.clear()
            queue.wake.wait(timeout=timeout)
    finally:
        server.shutdown()
        server.server_close()
    return 0


def healthcheck() -> int:
    config = ratings.Config.from_env()
    try:
        ready = json.loads((config.state_dir / "event-daemon-ready.json").read_text())
        datetime.fromisoformat(str(ready["ready_at"]))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return 1
    if ratings.healthcheck(config.state_dir, float(os.environ.get("IMDB_SYNC_HEALTH_MAX_AGE_HOURS", "14"))):
        return 1
    port = int(os.environ.get("IMDB_SYNC_WEBHOOK_PORT", "8788"))
    try:
        import urllib.request

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
            return 0 if response.status == 200 else 1
    except Exception:
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--event-daemon", action="store_true")
    parser.add_argument("--event-healthcheck", action="store_true")
    known, _unknown = parser.parse_known_args()
    if known.event_healthcheck:
        return healthcheck()
    if known.event_daemon or len(sys.argv) == 1:
        return daemon()
    return ratings.main()


if __name__ == "__main__":
    raise SystemExit(main())
