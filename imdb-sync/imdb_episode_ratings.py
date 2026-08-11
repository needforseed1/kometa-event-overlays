#!/usr/bin/env python3
"""Incrementally synchronize official IMDb episode ratings into Plex.

The worker deliberately uses IMDb's non-commercial TSV datasets instead of
scraping IMDb pages. It inventories Plex locally, refreshes only episodes that
are due, and emits an overlay scope file containing only newly overlaid or
rating-changed Plex item keys.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable


DATASET_BASE_URL = "https://datasets.imdbws.com"
RATINGS_DATASET = "title.ratings.tsv.gz"
EPISODE_DATASET = "title.episode.tsv.gz"
USER_AGENT = "kometa-event-overlays/0.1.0-alpha.1"
IMDB_ID_RE = re.compile(r"^tt\d+$")


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def epoch(value: datetime) -> int:
    return int(value.timestamp())


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def normalize_imdb_id(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.removeprefix("imdb://")
    return candidate if IMDB_ID_RE.fullmatch(candidate) else None


@dataclass(frozen=True)
class Policy:
    recent_days: int = 30
    medium_days: int = 180
    recent_hours: int = 24
    weekly_hours: int = 24 * 7
    monthly_hours: int = 24 * 30
    missing_retry_hours: int = 6


@dataclass(frozen=True)
class Episode:
    rating_key: int
    library: str
    show_rating_key: int
    season_number: int
    episode_number: int
    aired_at: date | None
    audience_rating: float | None
    imdb_id: str | None
    tvdb_id: int | None
    has_overlay: bool


@dataclass(frozen=True)
class Config:
    plex_url: str
    plex_token: str
    tmdb_api_key: str
    libraries: tuple[str, ...]
    state_dir: Path
    timeout: float
    page_size: int
    poll_hours: float
    dry_run: bool
    policy: Policy

    @classmethod
    def from_env(cls) -> "Config":
        libraries = tuple(
            value.strip()
            for value in os.environ.get("IMDB_SYNC_LIBRARIES", "").split(",")
            if value.strip()
        )
        return cls(
            plex_url=os.environ.get("PLEX_URL", "").rstrip("/"),
            plex_token=os.environ.get("PLEX_TOKEN", ""),
            tmdb_api_key=os.environ.get("TMDB_API_KEY", ""),
            libraries=libraries,
            state_dir=Path(os.environ.get("IMDB_SYNC_STATE_DIR", "/data")),
            timeout=float(os.environ.get("IMDB_SYNC_TIMEOUT", "120")),
            page_size=int(os.environ.get("IMDB_SYNC_PAGE_SIZE", "500")),
            poll_hours=float(os.environ.get("IMDB_SYNC_POLL_HOURS", "6")),
            dry_run=env_bool("IMDB_SYNC_DRY_RUN", True),
            policy=Policy(
                recent_days=int(os.environ.get("IMDB_SYNC_RECENT_DAYS", "30")),
                medium_days=int(os.environ.get("IMDB_SYNC_MEDIUM_DAYS", "180")),
                recent_hours=int(os.environ.get("IMDB_SYNC_RECENT_HOURS", "24")),
                weekly_hours=int(os.environ.get("IMDB_SYNC_WEEKLY_HOURS", str(24 * 7))),
                monthly_hours=int(os.environ.get("IMDB_SYNC_MONTHLY_HOURS", str(24 * 30))),
                missing_retry_hours=int(os.environ.get("IMDB_SYNC_MISSING_RETRY_HOURS", "6")),
            ),
        )

    def validate(self) -> None:
        if not self.plex_url:
            raise ValueError("PLEX_URL is required")
        if not self.plex_token:
            raise ValueError("PLEX_TOKEN is required")
        if not self.libraries:
            raise ValueError("IMDB_SYNC_LIBRARIES must contain at least one library")
        if self.page_size < 1:
            raise ValueError("IMDB_SYNC_PAGE_SIZE must be positive")
        if self.poll_hours <= 0:
            raise ValueError("IMDB_SYNC_POLL_HOURS must be positive")


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;

            CREATE TABLE IF NOT EXISTS episode_state (
                rating_key INTEGER PRIMARY KEY,
                library TEXT NOT NULL,
                show_rating_key INTEGER NOT NULL,
                season_number INTEGER NOT NULL,
                episode_number INTEGER NOT NULL,
                imdb_id TEXT,
                first_seen INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                last_checked INTEGER,
                last_updated INTEGER,
                last_rating REAL,
                status TEXT,
                error TEXT,
                original_rating REAL,
                original_rating_locked INTEGER,
                managed INTEGER NOT NULL DEFAULT 0,
                rolled_back_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS show_map (
                show_rating_key INTEGER PRIMARY KEY,
                imdb_id TEXT,
                tmdb_id INTEGER,
                checked_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS episode_map (
                parent_imdb_id TEXT NOT NULL,
                season_number INTEGER NOT NULL,
                episode_number INTEGER NOT NULL,
                imdb_id TEXT NOT NULL,
                PRIMARY KEY (parent_imdb_id, season_number, episode_number)
            );

            CREATE TABLE IF NOT EXISTS external_id_map (
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                imdb_id TEXT NOT NULL,
                PRIMARY KEY (source, external_id)
            );

            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        show_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(show_map)").fetchall()
        }
        if "tmdb_id" not in show_columns:
            self.connection.execute("ALTER TABLE show_map ADD COLUMN tmdb_id INTEGER")
        episode_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(episode_state)").fetchall()
        }
        for column, definition in (
            ("original_rating", "REAL"),
            ("original_rating_locked", "INTEGER"),
            ("managed", "INTEGER NOT NULL DEFAULT 0"),
            ("rolled_back_at", "INTEGER"),
        ):
            if column not in episode_columns:
                self.connection.execute(f"ALTER TABLE episode_state ADD COLUMN {column} {definition}")
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def get_episode(self, rating_key: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM episode_state WHERE rating_key = ?", (rating_key,)
        ).fetchone()

    def record_seen(self, item: Episode, now_epoch: int) -> sqlite3.Row:
        self.connection.execute(
            """
            INSERT INTO episode_state (
                rating_key, library, show_rating_key, season_number,
                episode_number, imdb_id, first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(rating_key) DO UPDATE SET
                library = excluded.library,
                show_rating_key = excluded.show_rating_key,
                season_number = excluded.season_number,
                episode_number = excluded.episode_number,
                imdb_id = COALESCE(excluded.imdb_id, episode_state.imdb_id),
                last_seen = excluded.last_seen
            """,
            (
                item.rating_key,
                item.library,
                item.show_rating_key,
                item.season_number,
                item.episode_number,
                item.imdb_id,
                now_epoch,
                now_epoch,
            ),
        )
        row = self.get_episode(item.rating_key)
        assert row is not None
        return row

    def mark_new(self, rating_key: int) -> sqlite3.Row:
        self.connection.execute(
            "UPDATE episode_state SET status = 'new' WHERE rating_key = ?",
            (rating_key,),
        )
        row = self.get_episode(rating_key)
        assert row is not None
        return row

    def mark_result(
        self,
        item: Episode,
        now_epoch: int,
        status: str,
        imdb_id: str | None,
        rating: float | None,
        updated: bool,
        error: str | None = None,
    ) -> None:
        self.connection.execute(
            """
            UPDATE episode_state
            SET imdb_id = COALESCE(?, imdb_id),
                last_checked = ?,
                last_updated = CASE WHEN ? THEN ? ELSE last_updated END,
                last_rating = ?,
                status = ?,
                error = ?
            WHERE rating_key = ?
            """,
            (
                imdb_id,
                now_epoch,
                int(updated),
                now_epoch,
                rating,
                status,
                error,
                item.rating_key,
            ),
        )

    def prepare_update(self, item: Episode, original_locked: bool) -> None:
        self.connection.execute(
            """
            UPDATE episode_state
            SET original_rating = ?,
                original_rating_locked = ?,
                managed = 1,
                rolled_back_at = NULL
            WHERE rating_key = ? AND managed = 0
            """,
            (item.audience_rating, int(original_locked), item.rating_key),
        )

    def managed_episodes(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT rating_key, original_rating, original_rating_locked
            FROM episode_state WHERE managed = 1 ORDER BY rating_key
            """
        ).fetchall()

    def mark_rolled_back(self, rating_key: int, now_epoch: int) -> None:
        self.connection.execute(
            """
            UPDATE episode_state
            SET managed = 0, rolled_back_at = ?, status = 'rolled-back'
            WHERE rating_key = ?
            """,
            (now_epoch, rating_key),
        )

    def get_show_ids(self, show_rating_key: int) -> tuple[str | None, int | None]:
        row = self.connection.execute(
            "SELECT imdb_id, tmdb_id FROM show_map WHERE show_rating_key = ?", (show_rating_key,)
        ).fetchone()
        if not row:
            return None, None
        return normalize_imdb_id(row["imdb_id"]), int(row["tmdb_id"]) if row["tmdb_id"] else None

    def set_show_ids(
        self,
        show_rating_key: int,
        imdb_id: str | None,
        tmdb_id: int | None,
        checked_at: int,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO show_map (show_rating_key, imdb_id, tmdb_id, checked_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(show_rating_key) DO UPDATE SET
                imdb_id = COALESCE(excluded.imdb_id, show_map.imdb_id),
                tmdb_id = COALESCE(excluded.tmdb_id, show_map.tmdb_id),
                checked_at = excluded.checked_at
            """,
            (show_rating_key, imdb_id, tmdb_id, checked_at),
        )

    def get_episode_imdb(self, parent: str, season: int, episode: int) -> str | None:
        row = self.connection.execute(
            """
            SELECT imdb_id FROM episode_map
            WHERE parent_imdb_id = ? AND season_number = ? AND episode_number = ?
            """,
            (parent, season, episode),
        ).fetchone()
        return normalize_imdb_id(row["imdb_id"]) if row else None

    def replace_episode_map(self, parents: set[str], mappings: dict[tuple[str, int, int], str]) -> None:
        for parent in parents:
            self.connection.execute("DELETE FROM episode_map WHERE parent_imdb_id = ?", (parent,))
        self.connection.executemany(
            """
            INSERT INTO episode_map (
                parent_imdb_id, season_number, episode_number, imdb_id
            ) VALUES (?, ?, ?, ?)
            """,
            ((parent, season, episode, imdb_id) for (parent, season, episode), imdb_id in mappings.items()),
        )

    def set_episode_map(self, parent: str, season: int, episode: int, imdb_id: str) -> None:
        self.connection.execute(
            """
            INSERT INTO episode_map (parent_imdb_id, season_number, episode_number, imdb_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(parent_imdb_id, season_number, episode_number)
            DO UPDATE SET imdb_id = excluded.imdb_id
            """,
            (parent, season, episode, imdb_id),
        )

    def get_external_imdb(self, source: str, external_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT imdb_id FROM external_id_map WHERE source = ? AND external_id = ?",
            (source, external_id),
        ).fetchone()
        return normalize_imdb_id(row["imdb_id"]) if row else None

    def set_external_imdb(self, source: str, external_id: str, imdb_id: str) -> None:
        self.connection.execute(
            """
            INSERT INTO external_id_map (source, external_id, imdb_id)
            VALUES (?, ?, ?)
            ON CONFLICT(source, external_id) DO UPDATE SET imdb_id = excluded.imdb_id
            """,
            (source, external_id, imdb_id),
        )

    def metadata_get(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def metadata_set(self, key: str, value: str) -> None:
        self.connection.execute(
            """
            INSERT INTO metadata (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    def commit(self) -> None:
        self.connection.commit()


class PlexClient:
    def __init__(self, base_url: str, token: str, timeout: float, page_size: int):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.page_size = page_size
        self._plexapi_server = None

    def _xml(self, path: str, params: dict[str, object] | None = None) -> ET.Element:
        query = urllib.parse.urlencode(params or {})
        url = f"{self.base_url}{path}{'?' + query if query else ''}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/xml",
                "X-Plex-Token": self.token,
                "X-Plex-Product": "Kometa IMDb Episode Sync",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return ET.fromstring(response.read())

    def library_sections(self) -> dict[str, str]:
        root = self._xml("/library/sections")
        return {
            node.attrib["title"]: node.attrib["key"]
            for node in root.findall("Directory")
            if node.attrib.get("type") == "show"
        }

    def episodes(self, section_key: str, library_name: str) -> list[Episode]:
        output: list[Episode] = []
        offset = 0
        while True:
            root = self._xml(
                f"/library/sections/{section_key}/all",
                {
                    "type": 4,
                    "includeGuids": 1,
                    "X-Plex-Container-Start": offset,
                    "X-Plex-Container-Size": self.page_size,
                },
            )
            nodes = root.findall("Video")
            for node in nodes:
                try:
                    rating_key = int(node.attrib["ratingKey"])
                    show_rating_key = int(node.attrib["grandparentRatingKey"])
                    season_number = int(node.attrib["parentIndex"])
                    episode_number = int(node.attrib["index"])
                except (KeyError, TypeError, ValueError):
                    continue
                guid_values = [guid.attrib.get("id") for guid in node.findall("Guid")]
                imdb_id = next(
                    (normalized for value in guid_values if (normalized := normalize_imdb_id(value))),
                    None,
                )
                tvdb_id = None
                for value in guid_values:
                    if value and value.startswith("tvdb://"):
                        try:
                            tvdb_id = int(value.removeprefix("tvdb://"))
                        except ValueError:
                            pass
                        break
                labels = {label.attrib.get("tag", "").lower() for label in node.findall("Label")}
                try:
                    audience_rating = float(node.attrib["audienceRating"])
                except (KeyError, TypeError, ValueError):
                    audience_rating = None
                output.append(
                    Episode(
                        rating_key=rating_key,
                        library=library_name,
                        show_rating_key=show_rating_key,
                        season_number=season_number,
                        episode_number=episode_number,
                        aired_at=parse_date(node.attrib.get("originallyAvailableAt")),
                        audience_rating=audience_rating,
                        imdb_id=imdb_id,
                        tvdb_id=tvdb_id,
                        has_overlay="overlay" in labels,
                    )
                )
            total = int(root.attrib.get("totalSize", root.attrib.get("size", len(nodes))))
            offset += len(nodes)
            if not nodes or offset >= total:
                break
        return output

    def show_ids(self, rating_key: int) -> tuple[str | None, int | None]:
        root = self._xml(f"/library/metadata/{rating_key}", {"includeGuids": 1})
        node = root.find("Directory")
        if node is None:
            node = root.find("Video")
        if node is None:
            return None, None
        imdb_id = None
        tmdb_id = None
        for guid in node.findall("Guid"):
            value = guid.attrib.get("id", "")
            imdb_id = imdb_id or normalize_imdb_id(value)
            if value.startswith("tmdb://"):
                try:
                    tmdb_id = int(value.removeprefix("tmdb://"))
                except ValueError:
                    pass
        return imdb_id, tmdb_id

    def item_state(self, rating_key: int) -> tuple[bool, bool]:
        root = self._xml(f"/library/metadata/{rating_key}")
        node = root.find("Video")
        if node is None:
            node = root.find("Directory")
        if node is None:
            return False, False
        has_overlay = any(
            label.attrib.get("tag", "").lower() == "overlay" for label in node.findall("Label")
        )
        audience_locked = any(
            field.attrib.get("name") == "audienceRating" and field.attrib.get("locked") == "1"
            for field in node.findall("Field")
        )
        return has_overlay, audience_locked

    def item_has_overlay(self, rating_key: int) -> bool:
        return self.item_state(rating_key)[0]

    def update_audience_rating(
        self, rating_key: int, rating: float | None, locked: bool = True
    ) -> None:
        if self._plexapi_server is None:
            from plexapi.server import PlexServer

            self._plexapi_server = PlexServer(self.base_url, self.token, timeout=self.timeout)
        item = self._plexapi_server.fetchItem(rating_key)
        item.editAudienceRating(rating, locked=locked)
        refreshed = self._plexapi_server.fetchItem(rating_key)
        actual = getattr(refreshed, "audienceRating", None)
        if rating is None:
            if actual is not None:
                raise RuntimeError("Plex did not confirm the cleared audience rating")
        elif actual is None or abs(float(actual) - rating) > 0.01:
            raise RuntimeError(f"Plex did not confirm audience rating {rating:.1f}")


class TMDbClient:
    """Use TMDb only to resolve an episode IMDb ID, never as a rating source."""

    def __init__(self, api_key: str, timeout: float):
        self.api_key = api_key
        self.timeout = timeout

    def episode_imdb_id(self, show_id: int, season: int, episode: int) -> str | None:
        if not self.api_key:
            return None
        path = f"https://api.themoviedb.org/3/tv/{show_id}/season/{season}/episode/{episode}/external_ids"
        request = urllib.request.Request(
            f"{path}?{urllib.parse.urlencode({'api_key': self.api_key})}",
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        return normalize_imdb_id(payload.get("imdb_id"))

    def tvdb_episode_imdb_id(self, tvdb_id: int) -> str | None:
        if not self.api_key:
            return None
        find_path = f"https://api.themoviedb.org/3/find/{tvdb_id}"
        find_request = urllib.request.Request(
            f"{find_path}?{urllib.parse.urlencode({'api_key': self.api_key, 'external_source': 'tvdb_id'})}",
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(find_request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        results = payload.get("tv_episode_results") or []
        if not results:
            return None
        match = results[0]
        try:
            return self.episode_imdb_id(
                int(match["show_id"]), int(match["season_number"]), int(match["episode_number"])
            )
        except (KeyError, TypeError, ValueError):
            return None


class DatasetCache:
    def __init__(self, directory: Path, state: State, timeout: float):
        self.directory = directory
        self.state = state
        self.timeout = timeout
        self.directory.mkdir(parents=True, exist_ok=True)

    def ensure(self, name: str) -> Path:
        path = self.directory / name
        headers = {"User-Agent": USER_AGENT}
        etag = self.state.metadata_get(f"dataset:{name}:etag")
        if path.exists() and etag:
            headers["If-None-Match"] = etag
        request = urllib.request.Request(f"{DATASET_BASE_URL}/{name}", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                temp_path = path.with_suffix(path.suffix + ".tmp")
                try:
                    with temp_path.open("wb") as handle:
                        while chunk := response.read(1024 * 1024):
                            handle.write(chunk)
                    validate_dataset(temp_path, name)
                    temp_path.replace(path)
                finally:
                    temp_path.unlink(missing_ok=True)
                if response.headers.get("ETag"):
                    self.state.metadata_set(f"dataset:{name}:etag", response.headers["ETag"])
                if response.headers.get("Last-Modified"):
                    self.state.metadata_set(
                        f"dataset:{name}:last_modified", response.headers["Last-Modified"]
                    )
                self.state.metadata_set(f"dataset:{name}:downloaded_at", str(epoch(utc_now())))
                self.state.commit()
                print(f"Downloaded {name} ({path.stat().st_size / 1024 / 1024:.1f} MiB)")
        except urllib.error.HTTPError as exc:
            if exc.code != 304:
                if path.exists():
                    print(f"warning: using cached {name} after HTTP {exc.code}", file=sys.stderr)
                else:
                    raise
        except (OSError, urllib.error.URLError) as exc:
            if path.exists():
                print(f"warning: using cached {name} after download failure: {exc}", file=sys.stderr)
            else:
                raise
        validate_dataset(path, name)
        return path


def validate_dataset(path: Path, name: str) -> None:
    expected_headers = {
        RATINGS_DATASET: ["tconst", "averageRating", "numVotes"],
        EPISODE_DATASET: ["tconst", "parentTconst", "seasonNumber", "episodeNumber"],
    }
    if not path.exists() or path.stat().st_size == 0:
        raise ValueError(f"IMDb dataset is empty: {path}")
    try:
        with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
            header = next(csv.reader(handle, delimiter="\t"))
    except (OSError, EOFError, StopIteration) as exc:
        raise ValueError(f"IMDb dataset is not a valid gzip TSV: {path}") from exc
    if header != expected_headers[name]:
        raise ValueError(f"Unexpected {name} header: {header!r}")


def scan_episode_mappings(
    path: Path, parents: set[str]
) -> dict[tuple[str, int, int], str]:
    mappings: dict[tuple[str, int, int], str] = {}
    if not parents:
        return mappings
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        for row in reader:
            parent = row["parentTconst"]
            if parent not in parents:
                continue
            try:
                season = int(row["seasonNumber"])
                episode = int(row["episodeNumber"])
            except ValueError:
                continue
            imdb_id = normalize_imdb_id(row["tconst"])
            if imdb_id:
                mappings[(parent, season, episode)] = imdb_id
    return mappings


def scan_ratings(path: Path, imdb_ids: set[str]) -> dict[str, tuple[float, int]]:
    ratings: dict[str, tuple[float, int]] = {}
    if not imdb_ids:
        return ratings
    remaining = set(imdb_ids)
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        for row in reader:
            imdb_id = row["tconst"]
            if imdb_id not in remaining:
                continue
            try:
                ratings[imdb_id] = (round(float(row["averageRating"]), 1), int(row["numVotes"]))
            except ValueError:
                pass
            remaining.discard(imdb_id)
            if not remaining:
                break
    return ratings


def hours_since(timestamp: int | None, now: datetime) -> float | None:
    if timestamp is None:
        return None
    return (epoch(now) - timestamp) / 3600


def due_reason(
    item: Episode,
    row: sqlite3.Row,
    now: datetime,
    policy: Policy,
    full: bool,
) -> str | None:
    if full:
        return "full"
    since_checked = hours_since(row["last_checked"], now)
    since_first_seen = hours_since(row["first_seen"], now) or 0
    if row["status"] in {"missing", "unmapped", "error"}:
        if since_checked is None or since_checked >= policy.missing_retry_hours:
            return f"retry-{row['status']}"

    # A new item found after the initial inventory gets one immediate lookup.
    # Its ongoing refresh interval is based only on air date.
    if row["status"] == "new" and since_checked is None:
        return "new-item"

    today = now.date()
    aired_age = (today - item.aired_at).days if item.aired_at else None
    is_recent = aired_age is not None and 0 <= aired_age <= policy.recent_days
    if is_recent:
        if since_checked is None or since_checked >= policy.recent_hours:
            return "recent-airdate"
        return None

    if aired_age is not None and 0 <= aired_age <= policy.medium_days:
        interval = policy.weekly_hours
        reason = "weekly"
    else:
        interval = policy.monthly_hours
        reason = "monthly"

    if since_checked is not None:
        return reason if since_checked >= interval else None
    # A fresh installation does not immediately sweep the whole library. Old
    # items become due after one normal interval, keeping bootstrap inexpensive.
    return reason if since_first_seen >= interval else None


def atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temp_path.replace(path)


def write_status(config: Config, success: bool, error: str | None, metrics: dict[str, object]) -> None:
    atomic_json(
        config.state_dir / "status.json",
        {
            "checked_at": utc_now().isoformat(),
            "success": success,
            "dry_run": config.dry_run,
            "error": error,
            "metrics": metrics,
        },
    )


def build_scope(
    config: Config,
    scope: dict[str, set[int]],
    reasons: Counter[str],
    full: bool,
    path: Path | None = None,
) -> None:
    atomic_json(
        path or config.state_dir / "overlay-scope.json",
        {
            "generated_at": utc_now().isoformat(),
            "dry_run": config.dry_run,
            "full": full,
            "libraries": {name: sorted(keys) for name, keys in sorted(scope.items())},
            "reason_counts": dict(sorted(reasons.items())),
        },
    )


def run_sync(
    config: Config,
    full: bool = False,
    scope_path: Path | None = None,
) -> dict[str, object]:
    config.validate()
    config.state_dir.mkdir(parents=True, exist_ok=True)
    state = State(config.state_dir / "state.sqlite3")
    plex = PlexClient(config.plex_url, config.plex_token, config.timeout, config.page_size)
    tmdb = TMDbClient(config.tmdb_api_key, config.timeout)
    datasets = DatasetCache(config.state_dir / "datasets", state, config.timeout)
    now = utc_now()
    now_epoch = epoch(now)
    metrics: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    scope: dict[str, set[int]] = defaultdict(set)

    try:
        sections = plex.library_sections()
        missing_libraries = [name for name in config.libraries if name not in sections]
        if missing_libraries:
            raise ValueError(f"Plex show libraries not found: {', '.join(missing_libraries)}")

        inventory_initialized = (
            state.metadata_get("inventory_initialized") == "1"
            or state.metadata_get("inventory_baseline_epoch") is not None
        )
        due: list[Episode] = []
        for library in config.libraries:
            items = plex.episodes(sections[library], library)
            metrics["episodes_seen"] += len(items)
            for item in items:
                was_known = state.get_episode(item.rating_key) is not None
                row = state.record_seen(item, now_epoch)
                if inventory_initialized and not was_known:
                    row = state.mark_new(item.rating_key)
                reason = due_reason(
                    item,
                    row,
                    now,
                    config.policy,
                    full,
                )
                if reason:
                    due.append(item)
                    reason_counts[reason] += 1
        if not inventory_initialized:
            state.metadata_set("inventory_initialized", "1")
        state.commit()
        metrics["episodes_due"] = len(due)

        resolved: dict[int, str] = {}
        fallback_items: list[Episode] = []
        for item in due:
            imdb_id = item.imdb_id or normalize_imdb_id(state.get_episode(item.rating_key)["imdb_id"])
            if imdb_id:
                resolved[item.rating_key] = imdb_id
                metrics["direct_episode_ids"] += 1
            else:
                fallback_items.append(item)

        parent_by_item: dict[int, str] = {}
        tmdb_parent_by_item: dict[int, int] = {}
        show_ids: dict[int, tuple[str | None, int | None]] = {}
        for item in fallback_items:
            if item.show_rating_key not in show_ids:
                parent, show_tmdb_id = state.get_show_ids(item.show_rating_key)
                if not parent or not show_tmdb_id:
                    plex_imdb_id, plex_tmdb_id = plex.show_ids(item.show_rating_key)
                    parent = parent or plex_imdb_id
                    show_tmdb_id = show_tmdb_id or plex_tmdb_id
                    state.set_show_ids(item.show_rating_key, parent, show_tmdb_id, now_epoch)
                show_ids[item.show_rating_key] = (parent, show_tmdb_id)
            parent, show_tmdb_id = show_ids[item.show_rating_key]
            if parent:
                parent_by_item[item.rating_key] = parent
                mapped = state.get_episode_imdb(parent, item.season_number, item.episode_number)
                if mapped:
                    resolved[item.rating_key] = mapped
            if show_tmdb_id:
                tmdb_parent_by_item[item.rating_key] = show_tmdb_id
        state.commit()

        unresolved_for_map = [item for item in fallback_items if item.rating_key not in resolved]
        parents_to_refresh = {
            parent_by_item[item.rating_key]
            for item in unresolved_for_map
            if item.rating_key in parent_by_item
        }
        if parents_to_refresh:
            episode_path = datasets.ensure(EPISODE_DATASET)
            mappings = scan_episode_mappings(episode_path, parents_to_refresh)
            state.replace_episode_map(parents_to_refresh, mappings)
            state.commit()
            metrics["episode_map_rows"] = len(mappings)
            for item in unresolved_for_map:
                parent = parent_by_item.get(item.rating_key)
                if parent:
                    mapped = state.get_episode_imdb(parent, item.season_number, item.episode_number)
                    if mapped:
                        resolved[item.rating_key] = mapped

        unresolved_after_dataset = [item for item in fallback_items if item.rating_key not in resolved]
        for item in unresolved_after_dataset:
            show_tmdb_id = tmdb_parent_by_item.get(item.rating_key)
            if not show_tmdb_id:
                continue
            try:
                mapped = tmdb.episode_imdb_id(
                    show_tmdb_id, item.season_number, item.episode_number
                )
            except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
                print(
                    f"warning: TMDb IMDb-ID lookup failed for Plex key {item.rating_key}: {exc}",
                    file=sys.stderr,
                )
                continue
            if mapped:
                resolved[item.rating_key] = mapped
                parent = parent_by_item.get(item.rating_key)
                if parent:
                    state.set_episode_map(parent, item.season_number, item.episode_number, mapped)
                metrics["tmdb_imdb_ids"] += 1

        unresolved_after_position = [item for item in fallback_items if item.rating_key not in resolved]
        for item in unresolved_after_position:
            if item.tvdb_id is None:
                continue
            mapped = state.get_external_imdb("tvdb", str(item.tvdb_id))
            if not mapped:
                try:
                    mapped = tmdb.tvdb_episode_imdb_id(item.tvdb_id)
                except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
                    print(
                        f"warning: TVDb-to-IMDb lookup failed for Plex key {item.rating_key}: {exc}",
                        file=sys.stderr,
                    )
                    continue
            if mapped:
                resolved[item.rating_key] = mapped
                state.set_external_imdb("tvdb", str(item.tvdb_id), mapped)
                parent = parent_by_item.get(item.rating_key)
                if parent:
                    state.set_episode_map(parent, item.season_number, item.episode_number, mapped)
                metrics["tmdb_tvdb_imdb_ids"] += 1
        state.commit()

        metrics["mapped_episode_ids"] = len(resolved) - metrics["direct_episode_ids"]
        metrics["unmapped"] = len(due) - len(resolved)

        ratings: dict[str, tuple[float, int]] = {}
        if resolved:
            ratings_path = datasets.ensure(RATINGS_DATASET)
            ratings = scan_ratings(ratings_path, set(resolved.values()))

        for item in due:
            imdb_id = resolved.get(item.rating_key)
            if not imdb_id:
                state.mark_result(item, now_epoch, "unmapped", None, None, False)
                metrics["unmapped_recorded"] += 1
                if not plex.item_has_overlay(item.rating_key):
                    scope[item.library].add(item.rating_key)
                    reason_counts["missing-overlay"] += 1
                continue
            rating_data = ratings.get(imdb_id)
            if not rating_data:
                state.mark_result(item, now_epoch, "missing", imdb_id, None, False)
                metrics["ratings_missing"] += 1
                if not plex.item_has_overlay(item.rating_key):
                    scope[item.library].add(item.rating_key)
                    reason_counts["missing-overlay"] += 1
                continue
            rating, _votes = rating_data
            changed = item.audience_rating is None or abs(item.audience_rating - rating) > 0.01
            try:
                if changed and not config.dry_run:
                    _has_overlay, original_locked = plex.item_state(item.rating_key)
                    state.prepare_update(item, original_locked)
                    state.commit()
                    plex.update_audience_rating(item.rating_key, rating, locked=True)
                if changed:
                    metrics["ratings_would_update" if config.dry_run else "ratings_updated"] += 1
                    scope[item.library].add(item.rating_key)
                    reason_counts["rating-changed"] += 1
                elif not plex.item_has_overlay(item.rating_key):
                    scope[item.library].add(item.rating_key)
                    reason_counts["missing-overlay"] += 1
                else:
                    metrics["ratings_unchanged"] += 1
                if not config.dry_run:
                    state.mark_result(item, now_epoch, "ok", imdb_id, rating, changed)
            except Exception as exc:
                if not config.dry_run:
                    state.mark_result(item, now_epoch, "error", imdb_id, rating, False, str(exc))
                metrics["update_errors"] += 1
                print(f"error: Plex rating update failed for key {item.rating_key}: {exc}", file=sys.stderr)

        if not config.dry_run:
            state.commit()
        build_scope(config, scope, reason_counts, full=False, path=scope_path)
        metrics["overlay_scope_items"] = sum(len(keys) for keys in scope.values())
        result = dict(sorted(metrics.items()))
        print(
            f"IMDb sync complete: due={metrics['episodes_due']}, "
            f"updated={metrics['ratings_updated']}, "
            f"would_update={metrics['ratings_would_update']}, "
            f"missing={metrics['ratings_missing']}, "
            f"unmapped={metrics['unmapped']}, scope={metrics['overlay_scope_items']}, "
            f"dry_run={config.dry_run}"
        )
        write_status(config, True, None, result)
        return result
    except Exception as exc:
        write_status(config, False, str(exc), dict(sorted(metrics.items())))
        raise
    finally:
        state.close()


def rollback(config: Config, dry_run: bool) -> dict[str, int]:
    config.validate()
    state = State(config.state_dir / "state.sqlite3")
    plex = PlexClient(config.plex_url, config.plex_token, config.timeout, config.page_size)
    now_epoch = epoch(utc_now())
    metrics: Counter[str] = Counter()
    try:
        rows = state.managed_episodes()
        metrics["managed"] = len(rows)
        for row in rows:
            rating = float(row["original_rating"]) if row["original_rating"] is not None else None
            locked = bool(row["original_rating_locked"])
            if not dry_run:
                plex.update_audience_rating(int(row["rating_key"]), rating, locked=locked)
                state.mark_rolled_back(int(row["rating_key"]), now_epoch)
            metrics["would_restore" if dry_run else "restored"] += 1
        if not dry_run:
            state.commit()
        print(
            f"Rollback complete: managed={metrics['managed']}, restored={metrics['restored']}, "
            f"would_restore={metrics['would_restore']}, dry_run={dry_run}"
        )
        return dict(sorted(metrics.items()))
    finally:
        state.close()


def healthcheck(state_dir: Path, max_age_hours: float) -> int:
    path = state_dir / "status.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        checked_at = datetime.fromisoformat(payload["checked_at"])
        age_hours = (utc_now() - checked_at).total_seconds() / 3600
        if not payload.get("success") or age_hours > max_age_hours:
            return 1
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return 1
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--daemon", action="store_true", help="Run immediately, then repeat")
    mode.add_argument("--healthcheck", action="store_true", help="Check the last run status")
    mode.add_argument("--rollback", action="store_true", help="Restore pre-sync Plex ratings")
    write_mode = parser.add_mutually_exclusive_group()
    write_mode.add_argument("--dry-run", action="store_true", help="Never write ratings to Plex")
    write_mode.add_argument("--write", action="store_true", help="Allow verified rating writes")
    parser.add_argument("--full", action="store_true", help="Process every episode once")
    parser.add_argument(
        "--max-health-age-hours",
        type=float,
        default=float(os.environ.get("IMDB_SYNC_HEALTH_MAX_AGE_HOURS", "14")),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = Config.from_env()
    if args.healthcheck:
        return healthcheck(config.state_dir, args.max_health_age_hours)
    if args.rollback:
        rollback(config, dry_run=not args.write)
        return 0
    if args.dry_run:
        config = Config(**{**config.__dict__, "dry_run": True})
    elif args.write:
        config = Config(**{**config.__dict__, "dry_run": False})

    if not args.daemon:
        run_sync(config, full=args.full)
        return 0

    while True:
        started = time.monotonic()
        try:
            run_sync(config, full=False)
        except Exception as exc:
            print(f"error: IMDb sync run failed: {exc}", file=sys.stderr, flush=True)
        elapsed = time.monotonic() - started
        sleep_seconds = max(60.0, config.poll_hours * 3600 - elapsed)
        print(f"Next IMDb sync in {sleep_seconds / 3600:.2f} hours", flush=True)
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
