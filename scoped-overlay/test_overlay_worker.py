from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from overlay_worker import Worker, atomic_json, generation, scope_age_hours, scope_count


class ScopeTests(unittest.TestCase):
    def test_generation_prefers_explicit_value(self) -> None:
        self.assertEqual("abc", generation({"generation": "abc", "generated_at": "date"}))

    def test_scope_count_validates_lists(self) -> None:
        self.assertEqual(3, scope_count({"libraries": {"TV": [1, 2], "Movies": [3]}}))
        with self.assertRaises(ValueError):
            scope_count({"libraries": {"TV": ["1"]}})

    def test_current_scope_age(self) -> None:
        age = scope_age_hours({"generated_at": datetime.now(timezone.utc).isoformat()})
        self.assertLess(abs(age), 0.01)


class WorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.scope = root / "scope.json"
        self.state = root / "state"
        self.environment = patch.dict(
            os.environ,
            {
                "KOMETA_OVERLAY_SCOPE_FILE": str(self.scope),
                "KOMETA_OVERLAY_WORKER_STATE_DIR": str(self.state),
                "KOMETA_CONFIG": str(root / "config.yml"),
            },
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temp.cleanup()

    def write_scope(self, *, dry_run: bool = False, keys: list[int] | None = None) -> None:
        atomic_json(
            self.scope,
            {
                "generation": "one",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "dry_run": dry_run,
                "full": False,
                "libraries": {"TV Shows": keys if keys is not None else []},
            },
        )

    def test_empty_scope_is_consumed_without_kometa(self) -> None:
        self.write_scope()
        worker = Worker()
        with patch("subprocess.run") as run:
            self.assertEqual("empty-scope", worker.process_once())
            run.assert_not_called()
        self.assertEqual("already-processed", worker.process_once())

    def test_dry_run_scope_is_consumed_without_kometa(self) -> None:
        self.write_scope(dry_run=True, keys=[1])
        worker = Worker()
        with patch("subprocess.run") as run:
            self.assertEqual("dry-run-scope", worker.process_once())
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
