"""Read user 4-axis preferences from
Projects/<name>/_artifacts/component_selecting/user_preferences.json.

Returns the dict if found and schema-valid, else None. release SKILL.md 检 None
后再 AskUser 4 轴 + 调 component-selecting-JP/scripts/record_preferences.py 回写。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

REQUIRED_KEYS = ("schema_version", "channel", "brand", "price_vs_stock", "blacklist_mpns")
KNOWN_CHANNELS = ("jp_domestic_fast", "cn_domestic_fast", "lcsc_jlcpcb", "auto_cheapest")


def load_preferences(project_dir: Path) -> Optional[dict]:
    path = (project_dir / "_artifacts" / "component_selecting"
            / "user_preferences.json")
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    if not all(k in data for k in REQUIRED_KEYS):
        return None
    if data.get("schema_version") != "v1":
        return None
    if data.get("channel") not in KNOWN_CHANNELS:
        return None
    return data


def channel_to_recommended_path(channel: str) -> str:
    """Map user channel choice → ORDER_GUIDE primary path label.

    Aligns with `recommended_paths` semantic used by coverage_scan, but driven by
    user intent rather than algorithmic coverage.
    """
    return {
        "lcsc_jlcpcb":      "lcsc",        # Path A: JLCPCB 拼单
        "jp_domestic_fast": "jp_domestic", # Path B/C-1/C-2: DK JP / Mouser JP
        "cn_domestic_fast": "lcsc",        # CN locale: 立创直发（同一 LCSC 采购路径）
        "auto_cheapest":    "auto",        # 让 coverage_scan 算
    }.get(channel, "auto")
