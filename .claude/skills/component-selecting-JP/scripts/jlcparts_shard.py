#!/usr/bin/env python3
"""Offline parametric discovery over jlcparts category shards (key-free).

Serves the `--discover` categories jlcsearch's typed endpoints lack (inductors,
ferrite beads, …). Downloads only the per-category browse shards + the shared
attributes LUT from the jlcparts gh-pages snapshot — never the 11 GB full DB.

Data layout (verified 2026-07 against yaqwsx.github.io/jlcparts):
  manifest.json                categories[] with {category, subcategory,
                               browseShards[], componentCount}
  browse-components-*.jsonl.gz line 1 = {"col": idx} header, then array rows;
                               `attributes` = int ids into attributes-lut
  attributes-lut.json.gz       list of [name, {format, primary, values}]

Cache: lib_cache/sources/jlcparts/ (workspace "按需重建" convention), 7-day TTL,
stale-if-refresh-fails. Snapshot upstream refreshes 3×/day.

CLI:
  jlcparts_shard.py --list-categories
  jlcparts_shard.py --role inductor_smd --query 10uH --min-stock 100 --limit 10
  jlcparts_shard.py --category "Ferrite Beads" --package 0603 --output rows.json
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

DATA_BASE = "https://yaqwsx.github.io/jlcparts/data"
WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
CACHE_DIR = WORKSPACE_ROOT / "lib_cache" / "sources" / "jlcparts"
SHARD_DIR = CACHE_DIR / "shards"
STATE_FILE = CACHE_DIR / "state.json"
LUT_FILE = CACHE_DIR / "attributes-lut.json.gz"
MANIFEST_FILE = CACHE_DIR / "manifest.json"
DEFAULT_MAX_AGE_DAYS = 7
USER_AGENT = "Mozilla/5.0 jlcparts-shard-discover/1.0"

# role → manifest subcategory substring (case-insensitive). Deliberately only
# the categories jlcsearch cannot serve; parametric roles jlcsearch covers
# should use its typed endpoints instead (see JLCSEARCH_LIST_OF_ROLE).
ROLE_SUBCATEGORY = {
    "inductor": "Inductors (SMD)",
    "inductor_smd": "Inductors (SMD)",
    "power_inductor": "Power Inductors",
    "inductor_th": "Through Hole Inductors",
    "ferrite_bead": "Ferrite Beads",
    "common_mode_choke": "Common Mode Filters",
}

# LUT attribute names that are metadata, not parametrics.
_META_ATTRS = {
    "Category", "Description", "Manufacturer", "Minimum Order Quantity",
    "Basic/Extended", "Status", "Datasheet", "Stock", "Price", "Package",
}


# ---------------------------------------------------------------------------
# cache plumbing


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def _fetch(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        dest.write_bytes(resp.read())


def _ensure_file(
    name: str,
    dest: Path,
    notes: list[str],
    *,
    max_age_days: float,
    refresh: bool,
) -> Path | None:
    """Fetch `name` from the snapshot into `dest` unless a fresh copy exists.
    Refresh failure degrades to the stale copy (offline-graceful)."""
    state = _load_state()
    fetched_at = float(state.get(name, {}).get("fetched_at") or 0)
    age_days = (time.time() - fetched_at) / 86400 if fetched_at else float("inf")
    if dest.exists() and not refresh and age_days <= max_age_days:
        return dest
    try:
        _fetch(f"{DATA_BASE}/{name}", dest)
        state.setdefault(name, {})["fetched_at"] = time.time()
        _save_state(state)
        return dest
    except Exception as exc:
        if dest.exists():
            notes.append(f"jlcparts_stale_cache_used:{name}:{type(exc).__name__}")
            return dest
        notes.append(f"jlcparts_fetch_failed:{name}:{type(exc).__name__}")
        return None


def _load_manifest(notes: list[str], *, max_age_days: float, refresh: bool) -> dict[str, Any] | None:
    path = _ensure_file("manifest.json", MANIFEST_FILE, notes, max_age_days=max_age_days, refresh=refresh)
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        notes.append(f"jlcparts_manifest_parse_failed:{type(exc).__name__}")
        return None


def _load_lut(notes: list[str], *, max_age_days: float, refresh: bool) -> list[Any]:
    path = _ensure_file("attributes-lut.json.gz", LUT_FILE, notes, max_age_days=max_age_days, refresh=refresh)
    if path is None:
        return []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            lut = json.load(fh)
        return lut if isinstance(lut, list) else []
    except Exception as exc:
        notes.append(f"jlcparts_lut_parse_failed:{type(exc).__name__}")
        return []


# ---------------------------------------------------------------------------
# decoding


def _attr_value(entry: Any) -> str:
    """Flatten one LUT entry's value spec to a display string. Conservative:
    prefer the `primary` slot, fall back to the first value; drop on surprise."""
    try:
        spec = entry[1]
        values = spec.get("values") or {}
        key = spec.get("primary") or spec.get("default")
        if key not in values:
            key = next(iter(values), None)
        if key is None:
            return ""
        slot = values[key]
        return str(slot[0]) if isinstance(slot, list) and slot else str(slot)
    except Exception:
        return ""


def _decode_attributes(ids: Any, lut: list[Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(ids, list) or not lut:
        return out
    for aid in ids:
        if not isinstance(aid, int) or not 0 <= aid < len(lut):
            continue
        entry = lut[aid]
        try:
            name = str(entry[0])
        except Exception:
            continue
        val = _attr_value(entry)
        if name and val:
            out[name] = val
    return out


def _first_tier_price(price_field: Any) -> float | None:
    if not isinstance(price_field, list):
        return None
    best: tuple[float, float] | None = None
    for tier in price_field:
        if not isinstance(tier, dict):
            continue
        price = tier.get("price")
        if not isinstance(price, (int, float)):
            continue
        q_from = tier.get("qFrom") or 0
        if best is None or q_from < best[0]:
            best = (q_from, float(price))
    return best[1] if best else None


def _match_categories(manifest: dict[str, Any], needle: str) -> list[dict[str, Any]]:
    needle_l = needle.lower()
    out = []
    for cat in manifest.get("categories") or []:
        sub = str(cat.get("subcategory") or "")
        top = str(cat.get("category") or "")
        if needle_l in sub.lower() or needle_l in top.lower():
            out.append(cat)
    return out


def _iter_shard_rows(path: Path):
    """Yield (header_map, row_list) pairs from one columnar browse shard."""
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        header: dict[str, int] | None = None
        for line in fh:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if header is None:
                if isinstance(obj, dict):
                    header = obj
                    continue
                return
            if isinstance(obj, list):
                yield header, obj


# ---------------------------------------------------------------------------
# public API


def discover_rows(
    role: str | None = None,
    query: str | None = None,
    limit: int = 30,
    *,
    category: str | None = None,
    package: str | None = None,
    min_stock: int = 1,
    basic_only: bool = False,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    refresh: bool = False,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (discover_rows, notes) for a role/category from cached shards."""
    notes: list[str] = []
    needle = category or ROLE_SUBCATEGORY.get(str(role or "").strip().lower())
    if not needle:
        notes.append(f"jlcparts_skipped:no_category_mapping:{role}")
        return [], notes
    manifest = _load_manifest(notes, max_age_days=max_age_days, refresh=refresh)
    if manifest is None:
        return [], notes
    cats = _match_categories(manifest, needle)
    if not cats:
        notes.append(f"jlcparts_no_category_match:{needle}")
        return [], notes
    lut = _load_lut(notes, max_age_days=max_age_days, refresh=refresh)

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    query_l = (query or "").lower()
    package_l = (package or "").lower()
    for cat in cats:
        for shard_name in cat.get("browseShards") or []:
            shard_path = _ensure_file(
                shard_name, SHARD_DIR / shard_name, notes,
                max_age_days=max_age_days, refresh=refresh,
            )
            if shard_path is None:
                continue
            try:
                for header, arr in _iter_shard_rows(shard_path):
                    idx = {k: header[k] for k in header}

                    def col(name: str) -> Any:
                        i = idx.get(name)
                        return arr[i] if isinstance(i, int) and i < len(arr) else None

                    lcsc_id = str(col("lcsc") or "")
                    mpn = str(col("mfr") or "")
                    if not mpn or lcsc_id in seen:
                        continue
                    stock = col("stock")
                    if isinstance(stock, (int, float)) and stock < min_stock:
                        continue
                    attrs = _decode_attributes(col("attributes"), lut)
                    if str(attrs.get("Status", "Active")).lower() != "active":
                        continue
                    if basic_only and "basic" not in str(attrs.get("Basic/Extended", "")).lower():
                        continue
                    description = str(col("description") or "")
                    pkg = attrs.get("Package") or ""
                    if package_l and package_l not in (pkg + " " + description).lower():
                        continue
                    if query_l and query_l not in (mpn + " " + description + " " + json.dumps(list(attrs.values()), ensure_ascii=False)).lower():
                        continue
                    key_params = {k: v for k, v in attrs.items() if k not in _META_ATTRS}
                    seen.add(lcsc_id)
                    rows.append(
                        {
                            "mpn": mpn,
                            "role": role,
                            "source": "jlcparts_shard",
                            "source_keyword": str(cat.get("subcategory") or needle),
                            "manufacturer": attrs.get("Manufacturer", ""),
                            "stock": stock,
                            "price": _first_tier_price(col("price")),
                            "currency": "CNY",
                            "lcsc_id": lcsc_id,
                            "package": pkg,
                            "package_hint": pkg,
                            "distributor_package": pkg,
                            "description": description,
                            "datasheet_url": col("datasheet"),
                            "key_parameters": key_params,
                            "is_basic": "basic" in str(attrs.get("Basic/Extended", "")).lower(),
                        }
                    )
            except Exception as exc:
                notes.append(f"jlcparts_shard_decode_failed:{shard_name}:{type(exc).__name__}")
    rows.sort(key=lambda r: (r["price"] is None, r["price"] if r["price"] is not None else 0))
    if len(rows) > limit:
        notes.append(f"jlcparts_truncated:{len(rows)}->{limit}")
        rows = rows[:limit]
    return rows, notes


# ---------------------------------------------------------------------------
# CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="jlcparts shard parametric discovery (key-free)")
    parser.add_argument("--role")
    parser.add_argument("--category", help="manifest category/subcategory substring (overrides --role)")
    parser.add_argument("--list-categories", action="store_true")
    parser.add_argument("--query")
    parser.add_argument("--package")
    parser.add_argument("--min-stock", type=int, default=1)
    parser.add_argument("--basic-only", action="store_true")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--max-age-days", type=float, default=DEFAULT_MAX_AGE_DAYS)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args(argv)

    notes: list[str] = []
    if args.list_categories:
        manifest = _load_manifest(notes, max_age_days=args.max_age_days, refresh=args.refresh)
        if manifest is None:
            print("\n".join(notes), file=sys.stderr)
            return 2
        for cat in manifest.get("categories") or []:
            print(f"{cat.get('category')} / {cat.get('subcategory')} ({cat.get('componentCount')})")
        return 0

    if not args.role and not args.category:
        parser.error("provide --role, --category, or --list-categories")
    rows, notes = discover_rows(
        role=args.role,
        query=args.query,
        limit=args.limit,
        category=args.category,
        package=args.package,
        min_stock=args.min_stock,
        basic_only=args.basic_only,
        max_age_days=args.max_age_days,
        refresh=args.refresh,
    )
    payload = {"rows": rows, "count": len(rows), "notes": notes}
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"json: {args.output} count={len(rows)}")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    return 0 if rows else 2


if __name__ == "__main__":
    raise SystemExit(main())
