#!/usr/bin/env python3
"""Run scoped Kometa overlays immediately when a new scope is published."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected an object in {path}")
    return payload


def generation(scope: dict[str, object]) -> str:
    value = scope.get("generation") or scope.get("generated_at")
    if not value:
        raise ValueError("scope has no generation")
    return str(value)


def scope_count(scope: dict[str, object]) -> int:
    libraries = scope.get("libraries")
    if not isinstance(libraries, dict):
        raise ValueError("scope libraries must be an object")
    count = 0
    for keys in libraries.values():
        if not isinstance(keys, list) or any(not isinstance(key, int) for key in keys):
            raise ValueError("scope library values must be integer lists")
        count += len(keys)
    return count


def scope_age_hours(scope: dict[str, object]) -> float:
    generated_at = datetime.fromisoformat(str(scope["generated_at"]))
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    return (utc_now() - generated_at).total_seconds() / 3600


class Worker:
    def __init__(self) -> None:
        self.scope_path = Path(
            os.environ.get("KOMETA_OVERLAY_SCOPE_FILE", "/imdb-sync/overlay-scope.json")
        )
        self.state_dir = Path(os.environ.get("KOMETA_OVERLAY_WORKER_STATE_DIR", "/config"))
        self.state_path = self.state_dir / "overlay-worker-state.json"
        self.status_path = self.state_dir / "overlay-worker-status.json"
        self.snapshot_path = self.state_dir / "overlay-scope-current.json"
        self.lock_path = self.state_dir / "overlay-worker.lock"
        self.config_path = Path(os.environ.get("KOMETA_CONFIG", "/config/config.yml"))
        self.poll_seconds = max(
            1.0, float(os.environ.get("KOMETA_OVERLAY_WORKER_POLL_SECONDS", "2"))
        )
        self.retry_seconds = max(
            5.0, float(os.environ.get("KOMETA_OVERLAY_WORKER_RETRY_SECONDS", "300"))
        )
        self.max_age_hours = float(
            os.environ.get("KOMETA_OVERLAY_SCOPE_MAX_AGE_HOURS", "14")
        )
        self.last_failure_generation: str | None = None
        self.retry_at = 0.0

    def last_processed(self) -> str | None:
        try:
            return str(read_json(self.state_path)["last_processed_generation"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def write_status(
        self,
        *,
        success: bool,
        current_generation: str | None,
        items: int,
        result: str,
        error: str | None = None,
    ) -> None:
        atomic_json(
            self.status_path,
            {
                "checked_at": utc_now().isoformat(),
                "success": success,
                "generation": current_generation,
                "items": items,
                "result": result,
                "error": error,
            },
        )

    def mark_processed(self, current_generation: str, items: int, result: str) -> None:
        atomic_json(
            self.state_path,
            {
                "last_processed_at": utc_now().isoformat(),
                "last_processed_generation": current_generation,
                "items": items,
                "result": result,
            },
        )
        self.write_status(
            success=True,
            current_generation=current_generation,
            items=items,
            result=result,
        )

    def kometa_command(self) -> list[str]:
        return [
            sys.executable,
            "/app/kometa/kometa.py",
            "--config",
            str(self.config_path),
            "--run",
            "--overlays-only",
        ]

    def process_once(self, force: bool = False) -> str:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as lock_handle:
            fcntl.flock(lock_handle, fcntl.LOCK_EX)
            scope = read_json(self.scope_path)
            current_generation = generation(scope)
            items = scope_count(scope)
            if not force and current_generation == self.last_processed():
                return "already-processed"
            age = scope_age_hours(scope)
            if age < -0.1 or age > self.max_age_hours:
                raise ValueError(f"scope age {age:.2f}h is outside the allowed window")
            if scope.get("dry_run"):
                self.mark_processed(current_generation, items, "dry-run-scope")
                return "dry-run-scope"
            if items == 0:
                self.mark_processed(current_generation, 0, "empty-scope")
                return "empty-scope"

            # Kometa reads this immutable snapshot for every library in the run,
            # even if the synchronizer publishes a newer scope concurrently.
            atomic_json(self.snapshot_path, scope)
            environment = dict(os.environ)
            environment["KOMETA_OVERLAY_SCOPE_FILE"] = str(self.snapshot_path)
            environment["KOMETA_LINUXSERVER"] = "True"
            started = time.monotonic()
            completed = subprocess.run(self.kometa_command(), env=environment, check=False)
            elapsed = time.monotonic() - started
            if completed.returncode != 0:
                raise RuntimeError(f"Kometa exited with status {completed.returncode}")
            self.mark_processed(current_generation, items, f"applied in {elapsed:.1f}s")
            return "applied"

    def daemon(self) -> int:
        print(f"Watching {self.scope_path} for scoped overlay work", flush=True)
        while True:
            try:
                scope = read_json(self.scope_path)
                current_generation = generation(scope)
                if current_generation != self.last_processed():
                    if (
                        current_generation != self.last_failure_generation
                        or time.monotonic() >= self.retry_at
                    ):
                        result = self.process_once()
                        print(
                            f"Overlay worker: generation={current_generation}, "
                            f"items={scope_count(scope)}, result={result}",
                            flush=True,
                        )
                        self.last_failure_generation = None
            except Exception as exc:
                current_generation = locals().get("current_generation")
                self.last_failure_generation = str(current_generation) if current_generation else None
                self.retry_at = time.monotonic() + self.retry_seconds
                self.write_status(
                    success=False,
                    current_generation=self.last_failure_generation,
                    items=0,
                    result="failed",
                    error=str(exc),
                )
                print(f"error: overlay worker failed: {exc}", file=sys.stderr, flush=True)
            time.sleep(self.poll_seconds)


def healthcheck() -> int:
    worker = Worker()
    try:
        status = read_json(worker.status_path)
        checked_at = datetime.fromisoformat(str(status["checked_at"]))
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        age = (utc_now() - checked_at).total_seconds() / 3600
        return 0 if status.get("success") and age <= worker.max_age_hours else 1
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        # Before first scope processing, a live readable scope is sufficient.
        try:
            scope = read_json(worker.scope_path)
            generation(scope)
            scope_count(scope)
            return 0
        except Exception:
            return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    if args.healthcheck:
        return healthcheck()
    worker = Worker()
    if args.once:
        print(worker.process_once(force=args.force))
        return 0
    return worker.daemon()


if __name__ == "__main__":
    raise SystemExit(main())
