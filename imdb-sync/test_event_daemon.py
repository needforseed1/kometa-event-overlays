from __future__ import annotations

import json
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from unittest.mock import Mock, patch

from event_daemon import (
    EventQueue,
    TargetResolver,
    event_key,
    merge_scope,
    normalize_event,
    run_cycle,
)


class NormalizeEventTests(unittest.TestCase):
    def test_radarr_upgrade(self) -> None:
        events = normalize_event(
            "radarr",
            {
                "eventType": "Download",
                "isUpgrade": True,
                "movie": {"tmdbId": 550},
                "movieFile": {"size": 123456},
            },
        )
        self.assertEqual(1, len(events))
        self.assertEqual(550, events[0]["tmdb_id"])
        self.assertEqual(123456, events[0]["expected_size"])
        self.assertTrue(events[0]["upgrade"])

    def test_sonarr_batch(self) -> None:
        events = normalize_event(
            "sonarr",
            {
                "eventType": "Download",
                "series": {"tvdbId": 123},
                "episodes": [
                    {"tvdbId": 1001, "seasonNumber": 3, "episodeNumber": 1},
                    {"tvdbId": 1002, "seasonNumber": 3, "episodeNumber": 2},
                ],
                "episodeFile": {"size": 654321},
            },
        )
        self.assertEqual([1001, 1002], [event["tvdb_id"] for event in events])
        self.assertEqual([654321, 654321], [event["expected_size"] for event in events])

    def test_tautulli_item(self) -> None:
        events = normalize_event(
            "tautulli",
            {"action": "created", "rating_key": "42", "media_type": "movie", "library_name": "Movies"},
        )
        self.assertEqual(42, events[0]["rating_key"])
        self.assertEqual("Movies", events[0]["library"])

    def test_test_events_are_ignored(self) -> None:
        self.assertEqual([], normalize_event("radarr", {"eventType": "Test"}))


class EventQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "events.json"
        self.queue = EventQueue(self.path, debounce_seconds=0, retry_seconds=10)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_deduplicates_same_target(self) -> None:
        target = normalize_event("radarr", {"eventType": "Download", "movie": {"tmdbId": 550}})[0]
        self.queue.add([dict(target)])
        self.queue.add([dict(target)])
        self.assertEqual(1, len(json.loads(self.path.read_text())))

    def test_complete_removes_resolved_and_retries_unresolved(self) -> None:
        first = {"source": "generic", "media_type": "movie", "rating_key": 1}
        second = {"source": "generic", "media_type": "movie", "rating_key": 2}
        self.queue.add([first, second])
        ready = self.queue.ready()
        self.queue.complete({ready[0]["id"]}, {ready[1]["id"]})
        remaining = json.loads(self.path.read_text())
        self.assertEqual(1, len(remaining))
        self.assertEqual(1, remaining[0]["attempts"])
        self.assertGreater(remaining[0]["ready_at"], time.time())

    def test_event_key_ignores_delivery_metadata(self) -> None:
        base = {"source": "radarr", "media_type": "movie", "tmdb_id": 550}
        with_metadata = {**base, "received_at": 123, "id": "abc"}
        self.assertEqual(event_key(base), event_key(with_metadata))


class TargetResolverTests(unittest.TestCase):
    def test_media_file_size_must_match_plex(self) -> None:
        config = Mock(plex_url="http://plex", plex_token="token", timeout=1, page_size=10)
        resolver = TargetResolver(config, Mock())
        resolver.plex._xml = Mock(
            return_value=ET.fromstring(
                '<MediaContainer><Video><Media><Part size="123456" /></Media></Video></MediaContainer>'
            )
        )

        self.assertTrue(resolver.media_file_is_current(42, {"expected_size": 123456}))
        self.assertFalse(resolver.media_file_is_current(42, {"expected_size": 999999}))
        self.assertTrue(resolver.media_file_is_current(42, {}))

    def test_tautulli_season_uses_children_and_skips_existing_overlays(self) -> None:
        config = Mock(
            plex_url="http://plex",
            plex_token="token",
            timeout=1,
            page_size=10,
            libraries=("TV Shows",),
        )
        resolver = TargetResolver(config, Mock(movie_libraries=("Movies",)))
        season = ET.fromstring(
            '<MediaContainer><Directory type="season" librarySectionTitle="TV Shows" /></MediaContainer>'
        )
        children = ET.fromstring(
            '<MediaContainer><Video ratingKey="101" /><Video ratingKey="102" /></MediaContainer>'
        )
        resolver.plex._xml = Mock(side_effect=[season, children])
        resolver.plex.item_has_overlay = Mock(side_effect=lambda key: key == 101)

        target = resolver.direct_target(
            {
                "source": "tautulli",
                "media_type": "season",
                "rating_key": 100,
                "library": "TV Shows",
                "received_at": time.time(),
            }
        )

        self.assertEqual(("TV Shows", {102}), target)
        self.assertEqual(
            "/library/metadata/100/children",
            resolver.plex._xml.call_args_list[1].args[0],
        )

    def test_tautulli_duplicate_item_resolves_to_empty_scope(self) -> None:
        config = Mock(
            plex_url="http://plex",
            plex_token="token",
            timeout=1,
            page_size=10,
            libraries=("TV Shows",),
        )
        resolver = TargetResolver(config, Mock(movie_libraries=("Movies",)))
        resolver.plex._xml = Mock(
            return_value=ET.fromstring(
                '<MediaContainer><Video type="episode" librarySectionTitle="TV Shows" /></MediaContainer>'
            )
        )
        resolver.plex.item_has_overlay = Mock(return_value=True)

        self.assertEqual(
            ("TV Shows", set()),
            resolver.direct_target(
                {
                    "source": "tautulli",
                    "media_type": "episode",
                    "rating_key": 101,
                    "library": "TV Shows",
                }
            ),
        )


class RunCycleTests(unittest.TestCase):
    @patch("event_daemon.merge_scope", return_value=1)
    @patch("event_daemon.TargetResolver")
    @patch("event_daemon.ratings.build_scope")
    @patch("event_daemon.ratings.run_sync")
    def test_movie_only_cycle_skips_episode_inventory(
        self,
        run_sync: Mock,
        build_scope: Mock,
        resolver_class: Mock,
        merge_scope: Mock,
    ) -> None:
        event = {"id": "one", "source": "radarr", "media_type": "movie"}
        resolver_class.return_value.resolve.return_value = (
            {"Movies": {42}},
            {"one"},
            set(),
            Counter({"event-radarr-upgrade": 1}),
        )
        config = Mock(state_dir=Path("/state"))
        event_config = Mock(max_attempts=10)
        queue = Mock()

        run_cycle(config, event_config, queue, [event], sync_ratings=False)

        run_sync.assert_not_called()
        staging_path = Path("/state/overlay-scope-staging.json")
        build_scope.assert_called_once_with(
            config,
            defaultdict(set),
            Counter(),
            full=False,
            path=staging_path,
        )
        merge_scope.assert_called_once_with(
            config,
            {"Movies": {42}},
            Counter({"event-radarr-upgrade": 1}),
            base_path=staging_path,
        )
        queue.complete.assert_called_once_with({"one"}, set())

    def test_merge_publishes_staging_and_events_as_one_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            staging = state_dir / "overlay-scope-staging.json"
            staging.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-08-11T12:00:00+00:00",
                        "dry_run": False,
                        "full": False,
                        "libraries": {"TV Shows": [1, 2]},
                        "reason_counts": {"rating-changed": 2},
                    }
                )
            )
            config = Mock(state_dir=state_dir, dry_run=False)

            count = merge_scope(
                config,
                {"TV Shows": {2, 3}, "Movies": {4}},
                Counter({"event-sonarr-import": 2, "event-radarr-upgrade": 1}),
                base_path=staging,
            )

            published = json.loads((state_dir / "overlay-scope.json").read_text())
            self.assertEqual(4, count)
            self.assertEqual({"TV Shows": [1, 2, 3], "Movies": [4]}, published["libraries"])
            self.assertEqual(2, published["reason_counts"]["rating-changed"])
            self.assertEqual(2, published["reason_counts"]["event-sonarr-import"])
            self.assertTrue(published["generation"])


if __name__ == "__main__":
    unittest.main()
