from __future__ import annotations

import gzip
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from imdb_episode_ratings import (
    EPISODE_DATASET,
    RATINGS_DATASET,
    Episode,
    Policy,
    State,
    due_reason,
    scan_episode_mappings,
    scan_ratings,
    validate_dataset,
)


NOW = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)


def episode(*, aired_days: int = 100, rating_key: int = 1) -> Episode:
    return Episode(
        rating_key=rating_key,
        library="TV Shows",
        show_rating_key=10,
        season_number=1,
        episode_number=2,
        aired_at=date(2026, 8, 11) - timedelta(days=aired_days),
        audience_rating=8.1,
        imdb_id="tt1234567",
        tvdb_id=123456,
        has_overlay=True,
    )


class DuePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.temp.name) / "state.sqlite3")
        self.policy = Policy()

    def tearDown(self) -> None:
        self.state.close()
        self.temp.cleanup()

    def row(self, item: Episode, *, first_seen_hours: int = 0, status: str | None = None, checked_hours: int | None = None):
        row = self.state.record_seen(item, int((NOW - timedelta(hours=first_seen_hours)).timestamp()))
        if status is not None or checked_hours is not None:
            checked = int((NOW - timedelta(hours=checked_hours or 0)).timestamp()) if checked_hours is not None else None
            self.state.connection.execute(
                "UPDATE episode_state SET status = ?, last_checked = ? WHERE rating_key = ?",
                (status, checked, item.rating_key),
            )
            row = self.state.get_episode(item.rating_key)
        return row

    def test_newly_discovered_episode_is_due_immediately(self) -> None:
        item = episode(aired_days=1000)
        self.row(item)
        row = self.state.mark_new(item.rating_key)
        self.assertEqual(
            "new-item",
            due_reason(item, row, NOW, self.policy, False),
        )

    def test_recent_airdate_is_due_immediately(self) -> None:
        item = episode(aired_days=3)
        self.assertEqual(
            "recent-airdate",
            due_reason(item, self.row(item), NOW, self.policy, False),
        )

    def test_recent_item_waits_until_daily_interval(self) -> None:
        item = episode(aired_days=1)
        row = self.row(item, status="ok", checked_hours=5)
        self.assertIsNone(due_reason(item, row, NOW, self.policy, False))

    def test_old_episode_uses_airdate_after_initial_lookup(self) -> None:
        item = episode(aired_days=1000)
        row = self.row(item, status="ok", checked_hours=24)
        self.assertIsNone(due_reason(item, row, NOW, self.policy, False))

    def test_missing_rating_retries_after_six_hours(self) -> None:
        item = episode(aired_days=100)
        row = self.row(item, status="missing", checked_hours=7)
        self.assertEqual("retry-missing", due_reason(item, row, NOW, self.policy, False))

    def test_bootstrap_defers_old_items_for_one_interval(self) -> None:
        item = episode(aired_days=500)
        self.assertIsNone(due_reason(item, self.row(item), NOW, self.policy, False))
        row = self.state.get_episode(item.rating_key)
        self.state.connection.execute(
            "UPDATE episode_state SET first_seen = ? WHERE rating_key = ?",
            (int((NOW - timedelta(days=31)).timestamp()), item.rating_key),
        )
        row = self.state.get_episode(item.rating_key)
        self.assertEqual("monthly", due_reason(item, row, NOW, self.policy, False))

    def test_full_always_runs(self) -> None:
        item = episode()
        self.assertEqual("full", due_reason(item, self.row(item), NOW, self.policy, True))

    def test_first_inventory_defers_old_items(self) -> None:
        item = episode(aired_days=1000)
        self.assertIsNone(due_reason(item, self.row(item), NOW, self.policy, False))

    def test_original_rating_and_lock_are_captured_for_rollback(self) -> None:
        item = episode()
        self.row(item)
        self.state.prepare_update(item, original_locked=False)
        self.state.commit()
        managed = self.state.managed_episodes()
        self.assertEqual(1, len(managed))
        self.assertEqual(8.1, managed[0]["original_rating"])
        self.assertEqual(0, managed[0]["original_rating_locked"])
        self.state.mark_rolled_back(item.rating_key, int(NOW.timestamp()))
        self.state.commit()
        self.assertEqual([], self.state.managed_episodes())


class DatasetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_gzip(self, name: str, text: str) -> Path:
        path = self.root / name
        with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
            handle.write(text)
        return path

    def test_rating_scan_selects_only_requested_ids(self) -> None:
        path = self.write_gzip(
            RATINGS_DATASET,
            "tconst\taverageRating\tnumVotes\n"
            "tt0000001\t7.1\t10\n"
            "tt0000002\t8.25\t20\n",
        )
        validate_dataset(path, RATINGS_DATASET)
        self.assertEqual({"tt0000002": (8.2, 20)}, scan_ratings(path, {"tt0000002"}))

    def test_episode_scan_handles_specials_and_missing_numbers(self) -> None:
        path = self.write_gzip(
            EPISODE_DATASET,
            "tconst\tparentTconst\tseasonNumber\tepisodeNumber\n"
            "tt0000101\ttt0000001\t0\t1\n"
            "tt0000102\ttt0000001\t\\N\t\\N\n"
            "tt0000103\ttt0000002\t1\t1\n",
        )
        validate_dataset(path, EPISODE_DATASET)
        self.assertEqual(
            {("tt0000001", 0, 1): "tt0000101"},
            scan_episode_mappings(path, {"tt0000001"}),
        )


if __name__ == "__main__":
    unittest.main()
