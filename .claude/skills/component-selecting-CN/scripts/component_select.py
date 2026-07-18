#!/usr/bin/env python3
"""component-selecting-CN thin shell → shared engine (component-selecting-JP/scripts).

The engine is locale-driven via locale_mapping.yaml; this wrapper injects the
CN skill identity (schema strings / next_command) and pins --locale 中国大陆
(this shell is CN-only; pass an explicit --locale to override).
"""
from __future__ import annotations

import sys
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parents[2] / "component-selecting-JP" / "scripts"
sys.path.insert(0, str(ENGINE_DIR))
import component_select as _engine  # noqa: E402


def _has_flag(argv: list[str], flag: str) -> bool:
    return any(a == flag or a.startswith(flag + "=") for a in argv)


def main() -> int:
    argv = list(sys.argv[1:])
    if not _has_flag(argv, "--caller-skill"):
        argv = ["--caller-skill", "component-selecting-CN"] + argv
    # CN-only shell: pin the locale so a workspace-global USER.md §0 set to a
    # different locale can't silently reroute this skill's runs.
    if not _has_flag(argv, "--locale"):
        argv += ["--locale", "中国大陆"]
    return _engine.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
