#!/usr/bin/env python3
"""Apply the version-pinned incremental-overlay safety patch to Kometa."""

from __future__ import annotations

import sys
from pathlib import Path


target = Path(sys.argv[1] if len(sys.argv) > 1 else "/app/kometa/modules/overlays.py")
source = target.read_text(encoding="utf-8")

if "KOMETA_OVERLAY_SCOPE_FILE" in source:
    raise SystemExit("overlay scope patch is already present")

replacements = [
    (
        "import os\nimport re\nfrom datetime import datetime\n",
        "import json\nimport os\nimport re\nfrom datetime import datetime, timezone\n",
    ),
    (
        "        self.library = library\n        self.overlays = []\n",
        """        self.library = library
        self.overlays = []
        self._scope_keys = None
        self._scope_dry_run = False
        scope_path = os.environ.get("KOMETA_OVERLAY_SCOPE_FILE")
        if scope_path:
            try:
                with open(scope_path, encoding="utf-8") as handle:
                    scope = json.load(handle)
                generated_at = datetime.fromisoformat(scope["generated_at"])
                if generated_at.tzinfo is None:
                    generated_at = generated_at.replace(tzinfo=timezone.utc)
                max_age = float(os.environ.get("KOMETA_OVERLAY_SCOPE_MAX_AGE_HOURS", "14"))
                age_hours = (datetime.now(timezone.utc) - generated_at).total_seconds() / 3600
                if age_hours > max_age:
                    logger.warning(f"Overlay Scope Warning: scope is {age_hours:.1f} hours old; processing no items")
                    self._scope_keys = set()
                elif scope.get("dry_run"):
                    raw_keys = scope.get("libraries", {}).get(self.library.name, [])
                    self._scope_keys = {int(key) for key in raw_keys}
                    self._scope_dry_run = True
                    logger.info(f"Overlay Scope: dry-run testing {len(self._scope_keys)} item(s); writes disabled")
                elif scope.get("full"):
                    logger.info("Overlay Scope: full run requested")
                else:
                    raw_keys = scope.get("libraries", {}).get(self.library.name, [])
                    self._scope_keys = {int(key) for key in raw_keys}
                    logger.info(f"Overlay Scope: {len(self._scope_keys)} item(s) selected for {self.library.name}")
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as err:
                raise Failed(f"Overlay Scope Error: refusing unscoped run: {err}")
""",
    ),
    (
        """        if not self.library.remove_overlays:
            key_to_overlays, properties = self.compile_overlays()
        ignore_list = [rk for rk in key_to_overlays]
""",
        """        if not self.library.remove_overlays:
            if self._scope_keys is not None and not self._scope_keys:
                logger.info(f"Overlay Scope: no items selected for {self.library.name}; skipping compilation")
            else:
                key_to_overlays, properties = self.compile_overlays()
                if self._scope_keys is not None:
                    key_to_overlays = {
                        key: value for key, value in key_to_overlays.items() if int(key) in self._scope_keys
                    }
                if self._scope_dry_run:
                    key_to_overlays = {}
        ignore_list = [rk for rk in key_to_overlays]
""",
    ),
    (
        """                            try:
                                builder.filter_and_save_items(builder.gather_ids(method, value))
                            except Failed as e:
""",
        """                            try:
                                if self._scope_keys is not None and method == "plex_all":
                                    gathered_ids = [(key, "ratingKey") for key in self._scope_keys]
                                else:
                                    gathered_ids = builder.gather_ids(method, value)
                                if self._scope_keys is not None and str(method).startswith("plex"):
                                    gathered_ids = [
                                        item_id for item_id in gathered_ids
                                        if isinstance(item_id, (tuple, list))
                                        and item_id
                                        and str(item_id[0]).isdigit()
                                        and int(item_id[0]) in self._scope_keys
                                    ]
                                builder.filter_and_save_items(gathered_ids)
                            except Failed as e:
""",
    ),
    (
        """    def get_overlay_items(self, label="Overlay", libtype=None, ignore=None):
        items = self.library.search(label=label, libtype=libtype)
        return items if not ignore else [o for o in items if o.ratingKey not in ignore]
""",
        """    def get_overlay_items(self, label="Overlay", libtype=None, ignore=None):
        if self._scope_dry_run:
            return []
        items = self.library.search(label=label, libtype=libtype)
        if self._scope_keys is not None:
            items = [item for item in items if int(item.ratingKey) in self._scope_keys]
        return items if not ignore else [o for o in items if o.ratingKey not in ignore]
""",
    ),
]

for old, new in replacements:
    count = source.count(old)
    if count != 1:
        raise SystemExit(f"refusing to patch {target}: expected one source block, found {count}")
    source = source.replace(old, new)

target.write_text(source, encoding="utf-8")
print(f"Patched {target} with fail-closed overlay scoping")
