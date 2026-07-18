#!/usr/bin/env python3
"""Deterministic component selection pipeline for component-selecting-JP Version 2.

This script intentionally replaces "agent browser + parallel subagents" with a
serial, hard-coded pipeline:

  input -> locale -> fixed vendor URLs -> optional fixed fetch -> adapter parse
        -> library gate -> solderability gate -> buyable gate -> scoring

It does not invent product URLs and it does not ask an LLM to classify HTML.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
V2_SCRIPTS = Path(__file__).resolve().parent
OLD_SCRIPTS = V2_SCRIPTS  # legacy alias retained for in-script callsites

sys.path.insert(0, str(V2_SCRIPTS))
from _dk_throttle import throttled_urlopen as _v2_dk_urlopen  # noqa: E402
# (sourcing skill removed 2026-05; fetch_datasheet_digikey.py now lives locally)
LOCALE_MAPPING = V2_SCRIPTS / "locale_mapping.yaml"
USER_MD = WORKSPACE_ROOT / "USER.md"

FETCH_TIMEOUT_SEC = 20
FIRECRAWL_TIMEOUT_SEC = 30
FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY", "")
VENDOR_CACHE_TTL_SEC = 12 * 60 * 60
VENDOR_CACHE_STALE_FALLBACK_SEC = 7 * 24 * 60 * 60
VENDOR_CACHE_SCHEMA = 1
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36 component-selecting-JP"
)
VENV_PYTHON = WORKSPACE_ROOT / ".venv" / "bin" / "python"

GOOD_LIBRARY_STATUSES = {
    "vendored_complete",
    "standard_ready",
    "external_cache_exact",
    # external_cache_compatible 故意不在此 set——cache 里只有同 package 占位
    # footprint，pinout 不属于此 MPN。等同于"库实际没有，要从 datasheet 重画
    # symbol + 校 footprint + 找 3D"，不能当 selecting 阶段的 pass 用。
    #
    # browser_vendorable 也不在此 set——V2 已删 Tier 5 browser probe（library_probe.py:770
    # "never actually worked"），此 status 现在永不会被产生；留在集合里只是误导。
    "lcsc_vendorable",
    "passive_generic",
}

# Generic-passive roles: KiCad standard footprint library covers all standard
# sizes (0402/0603/0805/1206/...) so per-MPN library_probe is meaningless for
# these. Phase 1 short-circuits to a synthetic pass and goes straight to
# vendor API. Pass --expected-role component to force the original probe path.
PASSIVE_GENERIC_ROLES = {
    "capacitor",
    "resistor",
    "ferrite_bead",
    "inductor_smd",
    "inductor_th",
}

REFDES_ROLE_RE = re.compile(r"^[A-Za-z]{1,3}\d")
VENDOR_CACHE_LOCK = threading.Lock()

VERDICT_ORDER = {
    "pass": 0,
    "warn_single_source": 1,
    "pending_web_data": 2,
    "pending_user_input": 3,
    "fail": 4,
}

ROLE_PROFILES: dict[str, dict[str, Any]] = {
    "isolated_dcdc": {
        "keywords": [
            "isolated dc dc converter",
            "0505 isolated dc dc",
            "5V 5V isolated converter SIP",
        ],
        "prefer_through_hole": True,
        "parameter_aliases": {
            "isolation": ("電圧 - 絶縁", "Voltage - Isolation"),
            "vout": ("電圧 - 出力1", "Voltage - Output 1"),
            "vin_min": ("電圧 - 入力（最小）", "Voltage - Input (Min)"),
            "vin_max": ("電圧 - 入力（最大）", "Voltage - Input (Max)"),
            "power": ("電力（ワット）", "Power (Watts)"),
            "mounting": ("取り付けタイプ", "Mounting Type"),
            "package": ("パッケージ/ケース", "Package / Case"),
        },
    },
    "ldo": {
        "keywords": ["LDO voltage regulator", "linear regulator fixed output"],
        "prefer_through_hole": False,
        "parameter_aliases": {
            "vout": ("電圧 - 出力（最小/固定）", "Voltage - Output (Min/Fixed)", "Voltage - Output"),
            "vin_max": ("電圧 - 入力（最大）", "Voltage - Input (Max)"),
            "current": ("電流 - 出力", "Current - Output"),
            "mounting": ("取り付けタイプ", "Mounting Type"),
            "package": ("パッケージ/ケース", "Package / Case"),
        },
    },
    "iso_amp": {
        "keywords": ["isolated amplifier", "isolation amplifier"],
        "prefer_through_hole": False,
        "parameter_aliases": {
            "isolation": ("電圧 - 絶縁", "Voltage - Isolation"),
            "channels": ("チャンネル数", "Number of Channels"),
            "mounting": ("取り付けタイプ", "Mounting Type"),
            "package": ("パッケージ/ケース", "Package / Case"),
        },
    },
    "tvs": {
        "keywords": ["TVS diode", "transient voltage suppressor"],
        "prefer_through_hole": False,
        "parameter_aliases": {
            "working_voltage": ("電圧 - 逆スタンドオフ", "Voltage - Reverse Standoff (Typ)"),
            "breakdown_voltage": ("電圧 - ブレークダウン", "Voltage - Breakdown"),
            "mounting": ("取り付けタイプ", "Mounting Type"),
            "package": ("パッケージ/ケース", "Package / Case"),
        },
    },
    "connector": {
        "keywords": ["terminal block connector", "wire to board connector"],
        "prefer_through_hole": True,
        "parameter_aliases": {
            "pitch": ("ピッチ", "Pitch"),
            "positions": ("ポジション数", "Number of Positions"),
            "mounting": ("取り付けタイプ", "Mounting Type"),
            "package": ("パッケージ/ケース", "Package / Case"),
        },
    },
    # SiC MOSFET / IGBT power-switch role. Adds keywords + JP/EN parameter
    # aliases that DK API actually returns for SiC parts (V_DS, R_DS(on),
    # Q_g, package). Without this V2 falls back to LCSC-only discovery and
    # cannot extract V_DS / R_DS(on) for ranking.
    "sic_mosfet": {
        "keywords": [
            "SiC MOSFET 650V",
            "SiC MOSFET 1200V TO-220",
            "silicon carbide MOSFET TO-247",
            "SiC N-channel MOSFET",
        ],
        "prefer_through_hole": True,
        "parameter_aliases": {
            "vds": ("ドレイン-ソース電圧 (Vdss)", "Drain to Source Voltage (Vdss)",
                    "電圧 - ドレイン-ソース"),
            "rds_on": ("Rds On (最大) @ Id, Vgs", "Rds On (Max) @ Id, Vgs",
                       "オン抵抗 (最大)"),
            "qg": ("ゲート電荷 (Qg) (最大) @ Vgs", "Gate Charge (Qg) (Max) @ Vgs"),
            "id": ("電流 - 連続ドレイン (Id) @ 25°C", "Current - Continuous Drain (Id) @ 25°C"),
            "mounting": ("取り付けタイプ", "Mounting Type"),
            "package": ("パッケージ/ケース", "Package / Case"),
        },
    },
}


@dataclass(frozen=True)
class VendorAdapter:
    vendor_id: str
    exact_required: bool = True
    nrnd_markers: tuple[str, ...] = ()
    active_markers: tuple[str, ...] = ()
    no_match_markers: tuple[str, ...] = ()
    stock_patterns: tuple[str, ...] = ()
    price_patterns: tuple[str, ...] = ()


COMMON_NRND = (
    "not for new designs",
    "nrnd",
    "obsolete",
    "discontinued",
    "end of life",
    "eol",
    "last time buy",
    "新規設計向けに不適合",
    "廃止",
    "販売終了",
    "生産終了",
)

COMMON_ACTIVE = (
    "in stock",
    "available",
    "active",
    "在庫",
    "在庫あり",
    "購入可能",
    "カートに入れる",
    "add to cart",
)

COMMON_NO_MATCH = (
    "no results",
    "no result",
    "0 results",
    "検索結果なし",
    "検索結果はありません",
    "該当する商品はありません",
    "一致する商品はありません",
    "検索結果がありません",
    "見つかりません",
    "not found",
    "no matches",
)

COMMON_STOCK_PATTERNS = (
    r"(?:stock|inventory|availability|available|in stock)[^0-9]{0,40}([0-9][0-9,]*)",
    r"(?:在庫|在庫数|库存|可用数量)[^0-9]{0,40}([0-9][0-9,]*)",
    r"([0-9][0-9,]*)\s*(?:in stock|available|個在庫|点在庫|pcs|pieces)",
)

COMMON_PRICE_PATTERNS = (
    r"(?:JPY|¥|￥)\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
    r"(?:USD|\$)\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
    r"(?:EUR|€)\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
)

VENDOR_ADAPTERS: dict[str, VendorAdapter] = {
    "akizuki": VendorAdapter(
        "akizuki",
        active_markers=COMMON_ACTIVE + ("販売価格", "税込"),
        no_match_markers=COMMON_NO_MATCH + ("検索条件に一致する商品はありません",),
        stock_patterns=COMMON_STOCK_PATTERNS,
        price_patterns=COMMON_PRICE_PATTERNS,
    ),
    "marutsu": VendorAdapter(
        "marutsu",
        active_markers=COMMON_ACTIVE + ("通常価格", "税込"),
        no_match_markers=COMMON_NO_MATCH,
        stock_patterns=COMMON_STOCK_PATTERNS,
        price_patterns=COMMON_PRICE_PATTERNS,
    ),
    "digikey_jp": VendorAdapter(
        "digikey_jp",
        nrnd_markers=COMMON_NRND,
        active_markers=COMMON_ACTIVE + ("部品ステータス", "part status"),
        no_match_markers=COMMON_NO_MATCH,
        stock_patterns=COMMON_STOCK_PATTERNS,
        price_patterns=COMMON_PRICE_PATTERNS,
    ),
    "chip1stop": VendorAdapter(
        "chip1stop",
        nrnd_markers=COMMON_NRND,
        active_markers=COMMON_ACTIVE + ("カート", "単価"),
        no_match_markers=COMMON_NO_MATCH,
        stock_patterns=COMMON_STOCK_PATTERNS,
        price_patterns=COMMON_PRICE_PATTERNS,
    ),
    "mouser_jp": VendorAdapter(
        "mouser_jp",
        nrnd_markers=COMMON_NRND,
        active_markers=COMMON_ACTIVE + ("stock", "availability"),
        no_match_markers=COMMON_NO_MATCH,
        stock_patterns=COMMON_STOCK_PATTERNS,
        price_patterns=COMMON_PRICE_PATTERNS,
    ),
    "rs_jp": VendorAdapter(
        "rs_jp",
        nrnd_markers=COMMON_NRND,
        active_markers=COMMON_ACTIVE,
        no_match_markers=COMMON_NO_MATCH,
        stock_patterns=COMMON_STOCK_PATTERNS,
        price_patterns=COMMON_PRICE_PATTERNS,
    ),
    "digikey_us": VendorAdapter(
        "digikey_us",
        nrnd_markers=COMMON_NRND,
        active_markers=COMMON_ACTIVE + ("product status",),
        no_match_markers=COMMON_NO_MATCH,
        stock_patterns=COMMON_STOCK_PATTERNS,
        price_patterns=COMMON_PRICE_PATTERNS,
    ),
    "mouser_us": VendorAdapter(
        "mouser_us",
        nrnd_markers=COMMON_NRND,
        active_markers=COMMON_ACTIVE,
        no_match_markers=COMMON_NO_MATCH,
        stock_patterns=COMMON_STOCK_PATTERNS,
        price_patterns=COMMON_PRICE_PATTERNS,
    ),
    "lcsc": VendorAdapter(
        "lcsc",
        nrnd_markers=COMMON_NRND,
        active_markers=COMMON_ACTIVE + ("add to cart", "warehouse", "stock"),
        no_match_markers=COMMON_NO_MATCH,
        stock_patterns=COMMON_STOCK_PATTERNS,
        price_patterns=COMMON_PRICE_PATTERNS,
    ),
}


# ---------------------------------------------------------------------------
# API-only buyable_gate path
# ---------------------------------------------------------------------------
#
# As of 2026-05, V2 no longer scrapes vendor HTML for the buyable_gate. Vendors
# without a public API (akizuki, marutsu, chip1stop, rs_jp) are skipped at URL
# build time. Only vendor_ids in API_VENDOR_KIND are queried.
#
# DigiKey API: OAuth2 client_credentials, JP locale via X-DIGIKEY-Locale-* hdrs.
# Mouser API:  api key in env; locale tied to the account that issued the key.

DIGIKEY_TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
DIGIKEY_KEYWORD_URL = "https://api.digikey.com/products/v4/search/keyword"
DIGIKEY_MEDIA_URL_TMPL = "https://api.digikey.com/products/v4/search/{dk_id}/media"
MOUSER_PARTNUMBER_URL = "https://api.mouser.com/api/v1/search/partnumber"
API_HTTP_TIMEOUT = 25

API_VENDOR_KIND: dict[str, str] = {
    "digikey_jp": "digikey",
    "digikey_us": "digikey",
    "digikey_de": "digikey",
    "mouser_jp": "mouser",
    "mouser_us": "mouser",
    "mouser_de": "mouser",
    # LCSC: jlcsearch.tscircuit.com primary + wmsc.lcsc.com fallback.
    # Treated as a buyable lane equal to DK/Mouser; for JP it represents the
    # JLCPCB co-order workflow (PCB + parts on one DHL shipment).
    "lcsc": "lcsc",
}

# Vendor IDs that ship from inside Japan (DHL not required, no customs).
# Used to derive `local_jp_active` so shortlists can flag "LCSC-only" winners
# as a JLCPCB co-order lane rather than a true JP-domestic lane.
JP_LOCAL_VENDOR_IDS = {"digikey_jp", "mouser_jp", "akizuki", "marutsu", "chip1stop", "rs_jp"}

# Always-query buyable lanes — these represent distinct fulfilment paths the
# user might pick at order time:
#   DK_JP / Mouser_JP → JP-domestic direct ship (fast, JPY)
#   LCSC              → JLCPCB co-order on the same DHL shipment (cheap, ~5d)
# Each user trip-off (PCB-with-parts vs parts-only) is a different choice, so
# we never short_circuit between them: all three lanes go into vendor_results
# and the user decides at order time. Long-tail HTML scrapers (akizuki /
# marutsu / chip1stop / rs_jp) DO honor short_circuit because they're slow
# and lower-signal.
CORE_API_LANES = {
    "digikey_jp", "digikey_us", "digikey_de",
    "mouser_jp",  "mouser_us",  "mouser_de",
    "lcsc",
}

# Lane classification — written into every vendor_result so downstream skills
# (bom-readiness, component-preparing) can filter by physical fulfilment
# path without having to know individual vendor_ids. Three distinct paths:
#   jp_domestic         ships from inside Japan, JPY, no customs, fast
#   jlcpcb_consolidated ships from China alongside JLCPCB PCB on one DHL
#                       shipment (~5 days to JP, CNY pricing)
#   intl_direct         ships from outside Japan direct (DK US / Mouser US /
#                       DK DE) — slower + import duty
LANE_OF_VENDOR: dict[str, str] = {
    "digikey_jp": "jp_domestic",
    "mouser_jp": "jp_domestic",
    "akizuki": "jp_domestic",
    "marutsu": "jp_domestic",
    "chip1stop": "jp_domestic",
    "rs_jp": "jp_domestic",
    "lcsc": "jlcpcb_consolidated",
    "digikey_us": "intl_direct",
    "mouser_us": "intl_direct",
    "digikey_de": "intl_direct",
    "mouser_de": "intl_direct",
}


def _local_vendor_ids(locale_block: dict[str, Any] | None) -> set[str]:
    """Locale-driven local-vendor set; falls back to the JP constant so a
    stale locale_mapping.yaml keeps today's behavior."""
    ids = (locale_block or {}).get("local_vendor_ids")
    return {str(v) for v in ids} if ids else set(JP_LOCAL_VENDOR_IDS)


def _lane_of(vendor_id: str, locale_block: dict[str, Any] | None) -> str:
    lanes = (locale_block or {}).get("lanes") or {}
    return str(lanes.get(vendor_id) or LANE_OF_VENDOR.get(vendor_id, "unknown"))

# Display labels for human-readable summary. Short and locale-flagged so the
# 3-lane comparison fits on one line per vendor.
VENDOR_DISPLAY: dict[str, tuple[str, str]] = {
    # vendor_id -> (flag, short_label)
    "digikey_jp": ("🇯🇵", "DK_JP"),
    "mouser_jp":  ("🇯🇵", "Mouser_JP"),
    "akizuki":    ("🇯🇵", "秋月"),
    "marutsu":    ("🇯🇵", "マルツ"),
    "chip1stop":  ("🇯🇵", "Chip1Stop"),
    "rs_jp":      ("🇯🇵", "RS_JP"),
    "lcsc":       ("🇨🇳", "LCSC"),
    "digikey_us": ("🇺🇸", "DK_US"),
    "mouser_us":  ("🇺🇸", "Mouser_US"),
    "digikey_de": ("🇩🇪", "DK_DE"),
    "mouser_de":  ("🇩🇪", "Mouser_DE"),
}

_DIGIKEY_TOKEN_CACHE: dict[str, Any] = {}  # {access_token, expires_at}
_DIGIKEY_TOKEN_LOCK = threading.Lock()

# Worker 数对齐 throttle 瓶颈：DK 600ms / Mouser 2.2s 间距 by _dk_throttle.
# 每个 worker 在锁上排队，>2 个 worker 不会增加吞吐（throttle 是纯单线程瓶颈），
# 反而引入 context switch + 多 sub-agent 并行时跨进程锁竞争更激烈。
DEFAULT_PARALLEL = 2
MAX_PARALLEL = 4

# Longlist 上限——量化每天 1000 quota / API 在多次选品会话下的安全用量。
# 软上限 10：覆盖 90% commodity 件选择 (LDO/电容/电阻/普通 IC) — 充分。
# 硬上限 25：specialty 件 (特殊 SiC/隔离器) 才需要广覆盖；超过这个数
# discovery 已经在堆重复或低质量结果，应回 LLM 重新 filter。
LONGLIST_SOFT_LIMIT = 10
LONGLIST_HARD_LIMIT = 25


def _digikey_get_token() -> tuple[str | None, str | None]:
    """Thread-safe OAuth token getter. Multiple workers block on a single
    refresh instead of stampeding the auth endpoint."""
    with _DIGIKEY_TOKEN_LOCK:
        cached = _DIGIKEY_TOKEN_CACHE
        if cached.get("access_token") and cached.get("expires_at", 0) - time.time() > 60:
            return cached["access_token"], None
        cid = os.environ.get("DIGIKEY_CLIENT_ID", "").strip()
        csec = os.environ.get("DIGIKEY_CLIENT_SECRET", "").strip()
        if not (cid and csec):
            return None, "digikey_credentials_missing"
        data = urllib.parse.urlencode(
            {"client_id": cid, "client_secret": csec, "grant_type": "client_credentials"}
        ).encode()
        req = urllib.request.Request(
            DIGIKEY_TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=API_HTTP_TIMEOUT) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return None, f"digikey_token_http_{exc.code}"
        except Exception as exc:
            return None, f"digikey_token_error_{type(exc).__name__}"
        token = payload.get("access_token")
        if not token:
            return None, "digikey_token_missing_in_response"
        _DIGIKEY_TOKEN_CACHE["access_token"] = token
        _DIGIKEY_TOKEN_CACHE["expires_at"] = time.time() + int(payload.get("expires_in", 600))
        return token, None


_SINGLE_VENDOR_ALIASES = {
    "digikey": {"digikey_jp", "digikey_us", "digikey_de"},
    "mouser":  {"mouser_jp",  "mouser_us",  "mouser_de"},
    "dk":      {"digikey_jp", "digikey_us", "digikey_de"},
    "lcsc":    {"lcsc"},
}


def _resolve_only_vendors(args: argparse.Namespace) -> set[str] | None:
    """Map --single-vendor flag to a vendor_id whitelist. Returns None when no
    restriction. Recognized aliases: 'digikey', 'mouser', or a literal
    vendor_id like 'digikey_jp'."""
    sv = getattr(args, "single_vendor", None)
    if not sv:
        return None
    sv = str(sv).strip().lower()
    if sv in _SINGLE_VENDOR_ALIASES:
        return _SINGLE_VENDOR_ALIASES[sv]
    return {sv}


def parallel_map(
    func: Any,
    items: list[Any],
    *,
    workers: int,
    label: str = "tasks",
) -> list[Any]:
    """Run `func(item)` over `items` with bounded concurrency.

    Output order matches input order. Exceptions inside workers are wrapped
    into the per-item result via the func itself (workers should not raise).
    `workers <= 1` (or len(items) <= 1) falls back to a serial loop.
    """
    n = len(items)
    workers = max(1, min(int(workers or 1), MAX_PARALLEL))
    if n <= 1 or workers == 1:
        return [func(item) for item in items]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(func, items))


def _digikey_locale_headers(locale_block: dict[str, Any]) -> dict[str, str]:
    api_block = (locale_block or {}).get("digikey_api") or {}
    return {
        "X-DIGIKEY-Locale-Site": str(api_block.get("site", "US")),
        "X-DIGIKEY-Locale-Language": str(api_block.get("lang", "en")),
        "X-DIGIKEY-Locale-Currency": str(api_block.get("currency", "USD")),
    }


def _classify_digikey_product(
    product: dict[str, Any], target_norm: str
) -> tuple[str, dict[str, Any]]:
    """Return (status, fields) for a DigiKey API product entry.

    status ∈ {"active","nrnd","no_match"}.  Fields include stock/price/url/etc.
    """
    status_text = (product.get("ProductStatus") or {}).get("Status") or ""
    status_low = status_text.lower()
    nrnd = bool(
        product.get("EndOfLife")
        or product.get("Discontinued")
        or product.get("Ncnr")
        or any(t in status_low for t in ("nrnd", "obsolete", "discontinued", "end of life"))
        or any(t in status_text for t in ("新規設計向けに不適合", "廃止", "販売終了", "生産終了"))
    )
    stock_raw = product.get("QuantityAvailable")
    price_raw = product.get("UnitPrice")
    pv_list = product.get("ProductVariations") or []
    pv0 = pv_list[0] if pv_list else {}
    fields = {
        "stock": int(stock_raw) if isinstance(stock_raw, (int, float)) else None,
        "price": float(price_raw) if isinstance(price_raw, (int, float)) else None,
        "product_status": status_text,
        "dk_part_number": pv0.get("DigiKeyProductNumber"),
        "final_url": product.get("ProductUrl"),
        "manufacturer": (product.get("Manufacturer") or {}).get("Name"),
        "matched_mpn": product.get("ManufacturerProductNumber"),
        "datasheet_url": product.get("DatasheetUrl"),
    }
    # Extract key parameters from DigiKey API
    raw_params = {
        str(p.get("ParameterText") or ""): str(p.get("ValueText") or "")
        for p in product.get("Parameters", []) or []
        if isinstance(p, dict)
    }
    fields["raw_parameters"] = raw_params
    if nrnd:
        return "nrnd", fields
    is_active_status = (
        product.get("NormallyStocking")
        or status_low in ("active", "アクティブ")
        or "アクティブ" in status_text
    )
    has_stock = isinstance(stock_raw, (int, float)) and stock_raw > 0
    if is_active_status or has_stock:
        return "active", fields
    return "no_match", fields


def query_digikey_api_for_vendor(
    *,
    vendor_id: str,
    mpn: str,
    vendor_url: str,
    vendor_name: str,
    locale_block: dict[str, Any],
) -> dict[str, Any]:
    base = {
        "vendor_id": vendor_id,
        "name": vendor_name,
        "url": vendor_url,
        "fetched_at": now_iso(),
        "fetch_driver": "digikey_api",
    }
    token, err = _digikey_get_token()
    if not token:
        return {**base, "status": "fetch_error", "reason": err or "digikey_token_missing"}
    headers = {
        "Authorization": f"Bearer {token}",
        "X-DIGIKEY-Client-Id": os.environ.get("DIGIKEY_CLIENT_ID", ""),
        "Content-Type": "application/json",
        "Accept": "application/json",
        **_digikey_locale_headers(locale_block),
    }
    body = json.dumps({"Keywords": mpn, "Limit": 5}).encode()
    req = urllib.request.Request(DIGIKEY_KEYWORD_URL, data=body, headers=headers)
    try:
        with _v2_dk_urlopen(req, timeout=API_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return {**base, "status": "fetch_error", "reason": f"digikey_api_http_{exc.code}"}
    except Exception as exc:
        return {
            **base,
            "status": "fetch_error",
            "reason": f"digikey_api_error_{type(exc).__name__}",
            "error": str(exc)[:200],
        }
    products = data.get("Products") or []
    target = norm(mpn)
    chosen = next(
        (p for p in products if norm(p.get("ManufacturerProductNumber")) == target),
        None,
    )
    if chosen is None:
        # Suffix-tolerant match (e.g. AMC1311BDWVR ↔ AMC1311BDWV reel suffix)
        for p in products:
            cand = norm(p.get("ManufacturerProductNumber"))
            if cand and (target.startswith(cand) or cand.startswith(target)):
                chosen = p
                break
    if chosen is None:
        return {
            **base,
            "status": "no_match",
            "reason": "exact_mpn_not_found_in_api",
            "exact_mpn_seen": False,
            "candidates_returned": len(products),
        }
    status, fields = _classify_digikey_product(chosen, target)
    out = {
        **base,
        "status": status,
        "reason": "digikey_api_ok",
        "exact_mpn_seen": True,
        "currency": locale_block.get("currency"),
        **fields,
    }
    if not out.get("final_url"):
        out["final_url"] = vendor_url
    return out


def query_mouser_api_for_vendor(
    *,
    vendor_id: str,
    mpn: str,
    vendor_url: str,
    vendor_name: str,
    locale_block: dict[str, Any],
) -> dict[str, Any]:
    base = {
        "vendor_id": vendor_id,
        "name": vendor_name,
        "url": vendor_url,
        "fetched_at": now_iso(),
        "fetch_driver": "mouser_api",
    }
    key = os.environ.get("MOUSER_SEARCH_API_KEY", "").strip()
    if not key:
        return {**base, "status": "fetch_error", "reason": "mouser_key_missing"}
    body = json.dumps(
        {"SearchByPartRequest": {"mouserPartNumber": mpn, "partSearchOptions": "1"}}
    ).encode()
    req = urllib.request.Request(
        f"{MOUSER_PARTNUMBER_URL}?apiKey={urllib.parse.quote(key)}",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        # Mouser also goes through the shared throttle (2.2s spacing for
        # 30/min limit + 429/503 retry). Without this, parallel sub-agents
        # bypass throttle on Mouser and trigger HTTP 503.
        with _v2_dk_urlopen(req, timeout=API_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return {**base, "status": "fetch_error", "reason": f"mouser_api_http_{exc.code}"}
    except Exception as exc:
        return {
            **base,
            "status": "fetch_error",
            "reason": f"mouser_api_error_{type(exc).__name__}",
            "error": str(exc)[:200],
        }
    errors = data.get("Errors") or []
    if errors:
        return {
            **base,
            "status": "fetch_error",
            "reason": "mouser_api_returned_errors",
            "errors": errors[:3],
        }
    parts = (data.get("SearchResults") or {}).get("Parts") or []
    target = norm(mpn)
    chosen = next(
        (p for p in parts if norm(p.get("ManufacturerPartNumber")) == target),
        None,
    )
    if chosen is None:
        for p in parts:
            cand = norm(p.get("ManufacturerPartNumber"))
            if cand and (target.startswith(cand) or cand.startswith(target)):
                chosen = p
                break
    if chosen is None:
        return {
            **base,
            "status": "no_match",
            "reason": "exact_mpn_not_found_in_api",
            "exact_mpn_seen": False,
            "candidates_returned": len(parts),
        }
    stock_raw = chosen.get("AvailabilityInStock")
    if stock_raw is None:
        avail = str(chosen.get("Availability") or "")
        m = re.search(r"([0-9][0-9,]*)", avail)
        stock_raw = int(m.group(1).replace(",", "")) if m else None
    if isinstance(stock_raw, str):
        m = re.search(r"([0-9][0-9,]*)", stock_raw)
        stock_raw = int(m.group(1).replace(",", "")) if m else None
    price = None
    currency = None
    pb_list = chosen.get("PriceBreaks") or []
    if pb_list:
        m = re.search(r"([0-9][0-9,]*\.?[0-9]*)", str(pb_list[0].get("Price", "")))
        if m:
            try:
                price = float(m.group(1).replace(",", ""))
            except ValueError:
                price = None
        currency = pb_list[0].get("Currency")
    lifecycle = (chosen.get("LifecycleStatus") or "").lower()
    nrnd = any(t in lifecycle for t in ("nrnd", "obsolete", "discontinued", "end of life", "eol"))
    has_stock = isinstance(stock_raw, (int, float)) and stock_raw > 0
    if nrnd:
        cls = "nrnd"
    elif has_stock:
        cls = "active"
    else:
        cls = "no_match"
    return {
        **base,
        "status": cls,
        "reason": "mouser_api_ok",
        "exact_mpn_seen": True,
        "stock": int(stock_raw) if isinstance(stock_raw, (int, float)) else None,
        "price": price,
        "currency": currency or locale_block.get("currency"),
        "lifecycle": chosen.get("LifecycleStatus"),
        "matched_mpn": chosen.get("ManufacturerPartNumber"),
        "manufacturer": chosen.get("Manufacturer"),
        "final_url": chosen.get("ProductDetailUrl") or vendor_url,
        "datasheet_url": chosen.get("DataSheetUrl"),
    }


def query_digikey_media_eda_models(dk_part_number: str | None) -> dict[str, Any]:
    """Probe DigiKey /media for EDA Models entries (UltraLibrarian / SnapEDA).

    The API exposes whether CAD assets *exist* somewhere; it does not download
    them. Returns {"available": bool, "entries": [...], "reason": str?}.
    """
    if not dk_part_number:
        return {"available": False, "reason": "no_dk_part_number"}
    token, err = _digikey_get_token()
    if not token:
        return {"available": False, "reason": err or "no_token"}
    enc = urllib.parse.quote(str(dk_part_number), safe="")
    req = urllib.request.Request(
        DIGIKEY_MEDIA_URL_TMPL.format(dk_id=enc),
        headers={
            "Authorization": f"Bearer {token}",
            "X-DIGIKEY-Client-Id": os.environ.get("DIGIKEY_CLIENT_ID", ""),
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=API_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return {"available": False, "reason": f"media_http_{exc.code}"}
    except Exception as exc:
        return {"available": False, "reason": f"media_error_{type(exc).__name__}"}
    links = data.get("MediaLinks") or []
    eda = [m for m in links if (m.get("MediaType") or "").lower() == "eda models"]
    return {
        "available": bool(eda),
        "entries": [
            {"title": m.get("Title"), "url": m.get("Url")}
            for m in eda
        ],
        "checked_at": now_iso(),
    }


JLCSEARCH_URL_TMPL = "https://jlcsearch.tscircuit.com/api/search?q={q}&limit=5&full=true"
LCSC_WMSC_URL_TMPL = "https://wmsc.lcsc.com/ftps/wm/product/detail?productCode={c}"

# FX rate cache for LCSC CNY→JPY display. Source: frankfurter.app (ECB ref
# rates, free, no key, weekday refresh). 24h TTL is fine since CNY/JPY
# day-over-day movement is typically <0.3% — well within "estimate" precision
# we surface to the user. Fallback rate used when frankfurter is unreachable.
FXRATE_URL = "https://api.frankfurter.app/latest?from=CNY&to=JPY"
FXRATE_CACHE_FILE = "/tmp/fxrate_cny_jpy.json"
FXRATE_CACHE_TTL_S = 86400  # 24h
FXRATE_FALLBACK_JPY_PER_CNY = 21.0  # rough modern level; flagged as 'fallback'
_FXRATE_LOCK = threading.Lock()


def _get_cny_jpy_rate() -> tuple[float, str]:
    """Return (jpy_per_cny, source). source ∈ {cache, frankfurter, fallback}.

    Cache file is shared across processes. Never raises — always returns a
    usable rate; on hard failure returns the hard-coded fallback marked as
    such so the LCSC output can flag it visually.
    """
    with _FXRATE_LOCK:
        # 1) Try cache (24h fresh)
        try:
            with open(FXRATE_CACHE_FILE) as f:
                cached = json.load(f)
            if (
                isinstance(cached, dict)
                and isinstance(cached.get("rate"), (int, float))
                and cached.get("rate") > 0
                and time.time() - float(cached.get("fetched_at", 0)) < FXRATE_CACHE_TTL_S
            ):
                return float(cached["rate"]), "cache"
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            pass

        # 2) Fetch from frankfurter.app
        try:
            req = urllib.request.Request(FXRATE_URL, headers={"User-Agent": DEFAULT_USER_AGENT})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            rate = float((data.get("rates") or {}).get("JPY", 0))
            if rate > 0:
                try:
                    with open(FXRATE_CACHE_FILE, "w") as f:
                        json.dump({"rate": rate, "fetched_at": time.time(),
                                   "source": "frankfurter", "as_of": data.get("date")}, f)
                except OSError:
                    pass
                return rate, "frankfurter"
        except Exception:
            pass

        # 3) Hard fallback
        return FXRATE_FALLBACK_JPY_PER_CNY, "fallback"


def query_lcsc_api_for_vendor(
    *,
    vendor_id: str,
    mpn: str,
    vendor_url: str,
    vendor_name: str,
    locale_block: dict[str, Any],
) -> dict[str, Any]:
    """LCSC buyable_gate adapter. Tier 1: jlcsearch.tscircuit.com (community,
    MPN keyword). Tier 2: wmsc.lcsc.com (LCSC CDN, by C-code, fills missing
    stock). Used for JP locale to surface LCSC-via-JLCPCB buyability.

    Returns the same dict shape as DigiKey/Mouser adapters:
    status ∈ {active, no_match, fetch_error}.
    LCSC has no formal NRND flag, so stock=0 → no_match (never nrnd).
    """
    base = {
        "vendor_id": vendor_id,
        "name": vendor_name,
        "url": vendor_url,
        "fetched_at": now_iso(),
        "fetch_driver": "lcsc_api",
    }
    target = norm(mpn)
    chosen: dict[str, Any] | None = None
    try:
        url = JLCSEARCH_URL_TMPL.format(q=urllib.parse.quote(mpn))
        req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
        with _v2_dk_urlopen(req, timeout=API_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return {**base, "status": "fetch_error", "reason": f"lcsc_jlcsearch_http_{exc.code}"}
    except Exception as exc:
        return {
            **base,
            "status": "fetch_error",
            "reason": f"lcsc_jlcsearch_error_{type(exc).__name__}",
            "error": str(exc)[:200],
        }
    for c in data.get("components", []) or []:
        cand = norm(c.get("mfr") or c.get("mfr_part") or c.get("name"))
        if not cand:
            continue
        if cand == target or target.startswith(cand) or cand.startswith(target):
            chosen = c
            break

    if chosen is None:
        return {
            **base,
            "status": "no_match",
            "reason": "exact_mpn_not_found_in_lcsc",
            "exact_mpn_seen": False,
        }

    if not chosen.get("stock"):
        extra_block = chosen.get("extra")
        if isinstance(extra_block, str):
            try:
                extra_block = json.loads(extra_block)
            except json.JSONDecodeError:
                extra_block = {}
        if not isinstance(extra_block, dict):
            extra_block = {}
        lcsc_code = chosen.get("lcsc") or extra_block.get("number")
        if lcsc_code:
            try:
                req2 = urllib.request.Request(
                    LCSC_WMSC_URL_TMPL.format(c=urllib.parse.quote(str(lcsc_code))),
                    headers={"User-Agent": DEFAULT_USER_AGENT},
                )
                with _v2_dk_urlopen(req2, timeout=API_HTTP_TIMEOUT) as resp:
                    raw = json.loads(resp.read())
                wmsc = raw.get("result") if isinstance(raw, dict) else None
                if not isinstance(wmsc, dict):
                    wmsc = raw if isinstance(raw, dict) else {}
                if not chosen.get("stock"):
                    chosen["stock"] = wmsc.get("stockNumber")
            except Exception:
                pass  # Tier-2 is best-effort

    stock_raw = chosen.get("stock")
    if isinstance(stock_raw, str):
        m = re.search(r"([0-9][0-9,]*)", stock_raw)
        stock_int: int | None = int(m.group(1).replace(",", "")) if m else None
    elif isinstance(stock_raw, (int, float)):
        stock_int = int(stock_raw)
    else:
        stock_int = None

    price_raw = chosen.get("price")
    price = float(price_raw) if isinstance(price_raw, (int, float)) else None

    has_stock = stock_int is not None and stock_int > 0
    cls = "active" if has_stock else "no_match"

    extra = chosen.get("extra")
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except json.JSONDecodeError:
            extra = {}
    if not isinstance(extra, dict):
        extra = {}
    lcsc_id = chosen.get("lcsc") or extra.get("number")
    final_url = (
        f"https://www.lcsc.com/product-detail/{lcsc_id}.html"
        if lcsc_id
        else f"https://www.lcsc.com/products/search?keyword={urllib.parse.quote(mpn)}"
    )
    ds_block = extra.get("datasheet") if isinstance(extra.get("datasheet"), dict) else {}
    datasheet_url = ds_block.get("pdf") or chosen.get("datasheet")

    # CNY → JPY conversion for cross-lane comparability with DK_JP / Mouser_JP.
    # Gated by locale fx_display (default cny_jpy = today's JP behavior). When
    # populated the rate has a hard fallback, so output_format can rely on
    # `price_jpy_estimated` being present whenever `price` is. fx_source tells
    # the renderer whether to add a ⚠ marker. fx_display: none (native-CNY
    # locales) skips the frankfurter call entirely; keys stay present as None.
    price_jpy_estimated: float | None = None
    fx_rate: float | None = None
    fx_source: str | None = None
    fx_mode = str((locale_block or {}).get("fx_display", "cny_jpy"))
    if isinstance(price, (int, float)) and price > 0 and fx_mode == "cny_jpy":
        fx_rate, fx_source = _get_cny_jpy_rate()
        price_jpy_estimated = round(price * fx_rate, 2)

    # jlcsearch full=true attributes → raw_parameters, only for locales that
    # opt in (lcsc_attributes_as_parameters: "on") — feeds _extract_key_params
    # for LCSC-only candidates. Conservative normalizer: scalars kept, dict
    # values contribute their first scalar slot, anything else dropped.
    raw_parameters: dict[str, str] = {}
    if str((locale_block or {}).get("lcsc_attributes_as_parameters", "off")) == "on":
        attrs = extra.get("attributes")
        if isinstance(attrs, dict):
            for attr_key, attr_val in attrs.items():
                if isinstance(attr_val, (str, int, float)) and str(attr_val).strip():
                    raw_parameters[str(attr_key)] = str(attr_val)
                elif isinstance(attr_val, dict):
                    for slot in attr_val.values():
                        if isinstance(slot, (str, int, float)) and str(slot).strip():
                            raw_parameters[str(attr_key)] = str(slot)
                            break

    result = {
        **base,
        "status": cls,
        "reason": "lcsc_api_ok" if has_stock else "lcsc_api_no_stock",
        "exact_mpn_seen": True,
        "stock": stock_int,
        "price": price,
        "currency": "CNY",
        "price_jpy_estimated": price_jpy_estimated,
        "fx_rate": fx_rate,
        "fx_source": fx_source,
        "lcsc_id": lcsc_id,
        "matched_mpn": chosen.get("mfr") or chosen.get("mfr_part") or chosen.get("name"),
        "manufacturer": chosen.get("manufacturer") or chosen.get("brand"),
        "final_url": final_url,
        "datasheet_url": datasheet_url,
        "package": chosen.get("package") or extra.get("package"),
    }
    if raw_parameters:
        result["raw_parameters"] = raw_parameters
    return result


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def norm(text: str | None) -> str:
    return re.sub(r"[^A-Z0-9]+", "", (text or "").upper())


def parse_num(text: str | None) -> float | None:
    if not text:
        return None
    cleaned = text.replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data or {}
    except ImportError:
        return load_yaml_simple(path)


def load_yaml_simple(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    lines = [
        ln.rstrip()
        for ln in text.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]

    def scalar(value: str) -> Any:
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            return value[1:-1]
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            if not inner:
                return []
            parts = re.split(r",(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", inner)
            return [str(scalar(p.strip())) for p in parts]
        if value.isdigit():
            return int(value)
        return value

    def parse_list(idx: int, indent: int) -> tuple[list[Any], int]:
        out: list[Any] = []
        while idx < len(lines):
            line = lines[idx]
            cur = len(line) - len(line.lstrip(" "))
            if cur < indent:
                break
            stripped = line.strip()
            if cur != indent or not stripped.startswith("- "):
                break
            out.append(scalar(stripped[2:]))
            idx += 1
        return out, idx

    def parse_block(idx: int, indent: int) -> tuple[dict[str, Any], int]:
        out: dict[str, Any] = {}
        while idx < len(lines):
            line = lines[idx]
            cur = len(line) - len(line.lstrip(" "))
            if cur < indent:
                break
            if cur > indent:
                idx += 1
                continue
            stripped = line.strip()
            if stripped.startswith("- ") or ":" not in stripped:
                break
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip()
            if val:
                out[key] = scalar(val)
                idx += 1
                continue
            nxt = idx + 1
            if nxt < len(lines) and lines[nxt].strip().startswith("- "):
                out[key], idx = parse_list(nxt, cur + 2)
            else:
                out[key], idx = parse_block(nxt, cur + 2)
        return out, idx

    parsed, _ = parse_block(0, 0)
    return parsed


def _warn_caller_locale_mismatch(args: argparse.Namespace, locale_name: str) -> None:
    """Mis-invocation hint: a locale thin shell driving a different locale is
    almost always a USER.md §0 / --locale mistake. Warn, never fail."""
    caller = getattr(args, "caller_skill", "component-selecting-JP")
    if caller == "component-selecting-CN" and locale_name != "中国大陆":
        print(
            f"WARN caller-skill={caller} but resolved locale={locale_name}; "
            "check USER.md §0 or pass --locale 中国大陆",
            file=sys.stderr,
        )


def read_user_locale(user_md_path: Path) -> str | None:
    if not user_md_path.exists():
        return None
    text = user_md_path.read_text(encoding="utf-8")
    match = re.search(
        r"\|\s*\*?\*?所属地[^|]*\|\s*\*?\*?([^*|\n]+?)\*?\*?\s*\|",
        text,
    )
    if not match:
        return None
    value = match.group(1).strip().strip("*").strip()
    if not value or value == "[待填]":
        return None
    return value


def resolve_locale(label: str | None, mapping: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    locales = mapping.get("locales", {}) or {}
    if label:
        lower = label.lower()
        for canonical, block in locales.items():
            aliases = [str(a) for a in block.get("aliases", []) or []]
            all_names = [canonical] + aliases
            if label in all_names or lower in [n.lower() for n in all_names]:
                return canonical, block
    return "unknown", locales.get("unknown", {})


def build_vendor_urls(
    mpn: str,
    locale_block: dict[str, Any],
    *,
    only_vendors: set[str] | None = None,
    include_html_vendors: bool = False,
) -> list[dict[str, str]]:
    """Iterate locale vendors_priority and emit URL records.

    Vendors with an API adapter are included by default. Vendors without a
    structured API are included only when explicitly requested; they use
    Firecrawl + classify_html and are intentionally outside the fast path.

    `only_vendors` further filters the set — used by `--single-vendor` to skip
    rate-limited vendors when the user only needs one source for verification.
    """
    encoded = urllib.parse.quote_plus(mpn)
    vendors = locale_block.get("vendors", {}) or {}
    has_firecrawl = bool(FIRECRAWL_API_KEY)
    out: list[dict[str, str]] = []
    for vendor_id in locale_block.get("vendors_priority", []) or []:
        block = vendors.get(vendor_id, {}) or {}
        template = block.get("search_url")
        if not template:
            continue
        if vendor_id not in API_VENDOR_KIND and (not include_html_vendors or not has_firecrawl):
            continue
        if only_vendors is not None and vendor_id not in only_vendors:
            continue
        out.append(
            {
                "vendor_id": vendor_id,
                "name": block.get("name", vendor_id),
                "url": str(template).format(mpn=encoded),
            }
        )
    return out


def role_profile(role: str | None) -> dict[str, Any]:
    if not role:
        return {}
    key = role.lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "isolated_dc_dc": "isolated_dcdc",
        "isolated_dcdc_converter": "isolated_dcdc",
        "dc_dc_isolated": "isolated_dcdc",
        "iso_dcdc": "isolated_dcdc",
        "iso_dc_dc": "isolated_dcdc",
        "dcdc_iso": "isolated_dcdc",
        "isolation_amplifier": "iso_amp",
        "isolated_amplifier": "iso_amp",
        "terminal_block": "connector",
    }
    return ROLE_PROFILES.get(key) or ROLE_PROFILES.get(aliases.get(key, ""), {})


def first_param(params: dict[str, str], aliases: tuple[str, ...] | list[str]) -> str:
    for alias in aliases:
        if alias in params and params[alias]:
            return str(params[alias])
    low_map = {k.lower(): v for k, v in params.items()}
    for alias in aliases:
        value = low_map.get(str(alias).lower())
        if value:
            return str(value)
    return ""


def parse_engineering_number(text: str | None) -> float | None:
    if not text:
        return None
    raw = str(text).replace(",", "")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([munpkMUNPK]?)([VAW]?)", raw)
    if not match:
        return None
    value = float(match.group(1))
    prefix = match.group(2).lower()
    if prefix == "m":
        value *= 1e-3
    elif prefix == "u":
        value *= 1e-6
    elif prefix == "n":
        value *= 1e-9
    elif prefix == "k":
        value *= 1e3
    return value


def parse_isolation_volts(text: str | None) -> float:
    if not text:
        return 0.0
    raw = str(text).replace(",", "")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(k)?", raw, re.I)
    if not match:
        return 0.0
    value = float(match.group(1))
    if match.group(2) or "kv" in raw.lower():
        value *= 1000
    return value


def text_contains_voltage(text: str, target: str | None) -> bool:
    if not target:
        return True
    normalized = str(text).replace(" ", "").lower()
    wanted = str(target).replace(" ", "").lower()
    if wanted in normalized:
        return True
    number = parse_engineering_number(target)
    if number is None:
        return False
    patterns = {
        f"{number:g}v",
        f"{number:.1f}v",
        f"{int(number)}v" if float(number).is_integer() else f"{number:g}v",
    }
    return any(p.lower() in normalized for p in patterns)


def voltage_within_range(target: str | None, min_text: str, max_text: str) -> bool:
    target_num = parse_engineering_number(target)
    if target_num is None:
        return False
    min_num = parse_engineering_number(min_text)
    max_num = parse_engineering_number(max_text)
    if min_num is not None and max_num is not None:
        return min_num <= target_num <= max_num
    return text_contains_voltage(min_text, target) or text_contains_voltage(max_text, target)


def dk_product_id(product: dict[str, Any]) -> str | None:
    url = product.get("ProductUrl") or ""
    match = re.search(r"/(\d{6,})(?:\?|$)", url)
    if match:
        return match.group(1)
    pid = product.get("ProductId")
    if isinstance(pid, int) or (isinstance(pid, str) and pid.isdigit()):
        return str(pid)
    variations = product.get("ProductVariations") or []
    if variations and isinstance(variations[0], dict):
        return variations[0].get("DigiKeyProductNumber")
    return None


def product_status_text(product: dict[str, Any]) -> str:
    status = product.get("ProductStatus")
    if isinstance(status, dict):
        return str(status.get("Status") or "")
    return str(status or "")


def discover_digikey_api(
    *,
    keywords: list[str],
    role: str | None,
    locale_block: dict[str, Any],
    limit_per_keyword: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    notes: list[str] = []
    try:
        from fetch_datasheet_digikey import _get_digikey_token  # type: ignore
    except Exception as exc:
        return [], [f"digikey_import_failed:{type(exc).__name__}"]

    token = _get_digikey_token()
    client_id = os.environ.get("DIGIKEY_CLIENT_ID", "")
    if not token or not client_id:
        return [], ["digikey_api_unavailable:missing_token_or_client_id"]

    dk_api = locale_block.get("digikey_api") or {}
    headers = {
        "Content-Type": "application/json",
        "X-DIGIKEY-Client-Id": client_id,
        "Authorization": f"Bearer {token}",
        "X-DIGIKEY-Locale-Site": dk_api.get("site", "US"),
        "X-DIGIKEY-Locale-Language": dk_api.get("lang", "en"),
        "X-DIGIKEY-Locale-Currency": dk_api.get("currency", "USD"),
    }
    out: list[dict[str, Any]] = []
    for keyword in keywords:
        body = json.dumps({"Keywords": keyword, "Limit": limit_per_keyword}).encode()
        req = urllib.request.Request(
            "https://api.digikey.com/products/v4/search/keyword",
            data=body,
            headers=headers,
        )
        try:
            with _v2_dk_urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            # 429 = rate-limited even after retries; surface explicitly
            # so caller / LLM knows discovery is degraded, not "no parts".
            tag = "digikey_rate_limited" if exc.code in (429, 503) else "digikey_keyword_failed"
            notes.append(f"{tag}:{keyword}:HTTP{exc.code}")
            print(f"⚠ V2 discover_digikey_api: {tag} on '{keyword}' (HTTP {exc.code})",
                  file=sys.stderr)
            continue
        except Exception as exc:
            notes.append(f"digikey_keyword_failed:{keyword}:{type(exc).__name__}")
            print(f"⚠ V2 discover_digikey_api: {type(exc).__name__} on '{keyword}': {exc}",
                  file=sys.stderr)
            continue

        for product in data.get("Products", []) or []:
            mpn = product.get("ManufacturerProductNumber") or ""
            if not mpn:
                continue
            params = {
                str(p.get("ParameterText") or ""): str(p.get("ValueText") or "")
                for p in product.get("Parameters", []) or []
                if isinstance(p, dict)
            }
            profile = role_profile(role)
            aliases = profile.get("parameter_aliases", {}) if profile else {}
            package = first_param(params, aliases.get("package", ())) if aliases else ""
            mounting = first_param(params, aliases.get("mounting", ())) if aliases else ""
            row = {
                "mpn": mpn,
                "role": role,
                "source": "digikey_api_keyword",
                "source_keyword": keyword,
                "manufacturer": ((product.get("Manufacturer") or {}).get("Name") or ""),
                "product_status": product_status_text(product),
                "stock": product.get("QuantityAvailable") or 0,
                "price": product.get("UnitPrice"),
                "currency": headers["X-DIGIKEY-Locale-Currency"],
                "product_url": product.get("ProductUrl"),
                "datasheet_url": product.get("DatasheetUrl"),
                "dk_part_id": dk_product_id(product),
                "package": package,
                "package_hint": package,
                "distributor_package": package,
                "distributor_mounting": mounting,
                "key_parameters": {
                    "isolation": first_param(params, aliases.get("isolation", ())) if aliases else "",
                    "vout": first_param(params, aliases.get("vout", ())) if aliases else "",
                    "vin_min": first_param(params, aliases.get("vin_min", ())) if aliases else "",
                    "vin_max": first_param(params, aliases.get("vin_max", ())) if aliases else "",
                    "power": first_param(params, aliases.get("power", ())) if aliases else "",
                    "mounting": mounting,
                    "package": package,
                },
                "raw_parameters": params,
            }
            out.append(row)
    return out, notes


def discover_lcsc_api(keywords: list[str], role: str | None, limit_per_keyword: int) -> tuple[list[dict[str, Any]], list[str]]:
    notes: list[str] = []
    out: list[dict[str, Any]] = []
    for keyword in keywords:
        url = (
            "https://jlcsearch.tscircuit.com/api/search?"
            f"q={urllib.parse.quote(keyword)}&limit={limit_per_keyword}&full=true"
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
        except Exception as exc:
            notes.append(f"lcsc_keyword_failed:{keyword}:{type(exc).__name__}")
            continue
        for comp in data.get("components", []) or []:
            extra = comp.get("extra")
            if isinstance(extra, str):
                try:
                    extra = json.loads(extra)
                except json.JSONDecodeError:
                    extra = {}
            if not isinstance(extra, dict):
                extra = {}
            mpn = comp.get("mfr") or comp.get("mfr_part") or comp.get("name") or ""
            if not mpn:
                continue
            package = comp.get("package") or extra.get("package") or ""
            out.append(
                {
                    "mpn": mpn,
                    "role": role,
                    "source": "lcsc_jlcsearch",
                    "source_keyword": keyword,
                    "manufacturer": comp.get("manufacturer") or comp.get("brand") or "",
                    "stock": comp.get("stock"),
                    "price": comp.get("price"),
                    "currency": "CNY",
                    "lcsc_id": comp.get("lcsc") or extra.get("number"),
                    "package": package,
                    "package_hint": package,
                    "distributor_package": package,
                    "description": comp.get("description") or comp.get("desc") or "",
                }
            )
    return out, notes


# jlcsearch parametric list endpoints (`/<category>/list.json`, key-free).
# Deliberately partial: categories jlcsearch lacks (inductors / ferrite beads,
# both 404 upstream) fall through to the jlcparts shard lane instead.
JLCSEARCH_LIST_OF_ROLE: dict[str, str] = {
    "resistor": "resistors",
    "capacitor": "capacitors",
    "led": "leds",
    "diode": "diodes",
    "ldo": "ldos",
    "voltage_regulator": "voltage_regulators",
    "mosfet": "mosfets",
    "bjt": "bjt_transistors",
    "fuse": "fuses",
    "switch": "switches",
    "relay": "relays",
    "potentiometer": "potentiometers",
    "header": "headers",
    "mcu": "microcontrollers",
    "adc": "adcs",
    "dac": "dacs",
}

# Response fields that are metadata rather than parametrics — everything else
# scalar in a list.json row is folded into key_parameters.
_JLCSEARCH_LIST_BASE_KEYS = {
    "lcsc", "mfr", "description", "stock", "price1", "in_stock",
    "package", "attributes", "is_basic", "is_preferred",
}


def discover_lcsc_parametric(
    role: str | None,
    params: list[str],
    limit: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Parametric discovery via jlcsearch typed category endpoints.

    `params` are `key=value` pairs passed through verbatim as query params —
    jlcsearch expects normalized SI values (e.g. resistance=1000, package=0402).
    """
    notes: list[str] = []
    out: list[dict[str, Any]] = []
    category = JLCSEARCH_LIST_OF_ROLE.get(str(role or "").strip().lower())
    if not category:
        notes.append(f"lcsc_parametric_skipped:no_endpoint_for_role:{role}")
        return out, notes
    query: dict[str, str] = {}
    for pair in params or []:
        key, _, val = str(pair).partition("=")
        if key.strip() and val.strip():
            query[key.strip()] = val.strip()
    query["limit"] = str(max(1, int(limit)))
    url = f"https://jlcsearch.tscircuit.com/{category}/list.json?" + urllib.parse.urlencode(query)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        notes.append(f"lcsc_parametric_failed:{category}:{type(exc).__name__}")
        return out, notes
    rows = data.get(category) if isinstance(data, dict) else None
    if not isinstance(rows, list):
        notes.append(f"lcsc_parametric_bad_shape:{category}")
        return out, notes
    for comp in rows:
        if not isinstance(comp, dict):
            continue
        mpn = comp.get("mfr") or ""
        if not mpn:
            continue
        package = comp.get("package") or ""
        key_params = {
            k: v
            for k, v in comp.items()
            if k not in _JLCSEARCH_LIST_BASE_KEYS
            and isinstance(v, (str, int, float))
            and v != ""
        }
        out.append(
            {
                "mpn": mpn,
                "role": role,
                "source": "lcsc_parametric",
                "source_keyword": category,
                "manufacturer": "",
                "stock": comp.get("stock"),
                "price": comp.get("price1"),
                "currency": "CNY",
                "lcsc_id": comp.get("lcsc"),
                "package": package,
                "package_hint": package,
                "distributor_package": package,
                "description": comp.get("description") or "",
                "key_parameters": key_params,
                "is_basic": comp.get("is_basic"),
            }
        )
    return out, notes


def discover_local_history(role: str | None, keywords: list[str], limit: int = 50) -> tuple[list[dict[str, Any]], list[str]]:
    terms = [t.lower() for t in keywords if t]
    rows: list[dict[str, Any]] = []
    for path in (WORKSPACE_ROOT / "Projects").glob("*/datasheets/component_selecting/*.json"):
        if "_scratch" in path.parts:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        try:
            candidates = extract_candidates(data) if isinstance(data, (dict, list)) else []
        except ValueError:
            candidates = [data] if isinstance(data, dict) and data.get("mpn") else []
        for cand in candidates:
            hay = json.dumps(cand, ensure_ascii=False).lower()
            if terms and not any(term in hay for term in terms):
                continue
            vendor = cand.get("vendor") if isinstance(cand.get("vendor"), dict) else {}
            lcsc_global = cand.get("lcsc_global") if isinstance(cand.get("lcsc_global"), dict) else {}
            library = cand.get("library") if isinstance(cand.get("library"), dict) else {}
            package = (
                cand.get("package")
                or cand.get("package_hint")
                or lcsc_global.get("package")
                or cand.get("distributor_package")
            )
            library_status = cand.get("library_status")
            if not library_status:
                library_status = library.get("actual_status") or library.get("status")
            if library_status == "vendored":
                library_status = "vendored_complete"
            row = {
                "mpn": cand.get("mpn"),
                "role": cand.get("role") or role,
                "source": "local_history",
                "source_file": str(path.relative_to(WORKSPACE_ROOT)),
                "manufacturer": cand.get("manufacturer") or vendor.get("manufacturer"),
                "stock": vendor.get("stock") or lcsc_global.get("stock"),
                "price": vendor.get("price"),
                "currency": vendor.get("currency"),
                "product_url": vendor.get("url"),
                "package": package,
                "package_hint": package,
                "library_status": library_status,
                "package_consistency": cand.get("package_consistency"),
            }
            rows.append(row)
            if len(rows) >= limit:
                return rows, []
    return rows, []


def candidate_passes_discover_filters(candidate: dict[str, Any], args: argparse.Namespace) -> tuple[bool, str]:
    status = str(candidate.get("product_status") or candidate.get("status") or "")
    if status and not any(ok in status.lower() for ok in ("active", "アクティブ")):
        return False, "not_active"
    stock = candidate.get("stock")
    if isinstance(stock, (int, float)) and stock < args.min_stock:
        return False, "stock_below_min"
    params = candidate.get("key_parameters") or {}
    if args.vout and not text_contains_voltage(str(params.get("vout", "")), args.vout):
        return False, "vout_mismatch"
    if args.vout_pattern and args.vout_pattern not in str(params.get("vout", "")):
        return False, "vout_pattern_mismatch"
    if args.vin and not voltage_within_range(
        args.vin,
        str(params.get("vin_min", "")),
        str(params.get("vin_max", "")),
    ):
        return False, "vin_mismatch"
    if args.vin_pattern and args.vin_pattern not in (
        str(params.get("vin_min", "")) + " " + str(params.get("vin_max", ""))
    ):
        return False, "vin_pattern_mismatch"
    if args.min_iso_v and parse_isolation_volts(str(params.get("isolation", ""))) < args.min_iso_v:
        return False, "isolation_below_min"
    if args.min_power_w:
        power = parse_engineering_number(str(params.get("power", "")))
        if power is not None and power < args.min_power_w:
            return False, "power_below_min"
    return True, "pass"


def through_hole_score(candidate: dict[str, Any]) -> int:
    text = " ".join(
        str(candidate.get(k, ""))
        for k in ("distributor_mounting", "package", "package_hint", "description")
    ).lower()
    return 0 if any(t in text for t in ("through", "スルーホール", "tht", "sip", "dip", "to-220")) else 1


def dedupe_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = norm(row.get("mpn"))
        if not key:
            continue
        existing = seen.get(key)
        if existing is None:
            seen[key] = row
            continue
        sources = set(existing.get("discovery_sources", [existing.get("source")]))
        sources.add(row.get("source"))
        existing["discovery_sources"] = sorted(s for s in sources if s)
        for field in ("product_url", "datasheet_url", "dk_part_id", "lcsc_id", "package", "package_hint"):
            if not existing.get(field) and row.get(field):
                existing[field] = row[field]
    return list(seen.values())


def safe_mpn(mpn: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", mpn).strip("_") or "unknown"


def artifact_root(project_path: Path | None) -> Path | None:
    if not project_path:
        return None
    root = project_path / "_artifacts" / "component_selecting_version2"
    root.mkdir(parents=True, exist_ok=True)
    return root


def load_vendor_health(project_path: Path | None) -> dict[str, Any]:
    root = artifact_root(project_path)
    if not root:
        return {}
    path = root / "vendor_health.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_vendor_health(project_path: Path | None, health: dict[str, Any]) -> None:
    root = artifact_root(project_path)
    if not root:
        return
    path = root / "vendor_health.json"
    path.write_text(json.dumps(health, ensure_ascii=False, indent=2), encoding="utf-8")


def update_vendor_health(
    health: dict[str, Any], vendor_id: str, status: str, reason: str | None = None
) -> None:
    item = health.setdefault(vendor_id, {})
    item["last_status"] = status
    item["last_reason"] = reason
    item["updated_at"] = now_iso()
    if status == "fetch_error":
        item["consecutive_fetch_error"] = int(item.get("consecutive_fetch_error", 0)) + 1
    else:
        item["consecutive_fetch_error"] = 0


def vendor_cache_path(project_path: Path | None) -> Path | None:
    root = artifact_root(project_path)
    return (root / "vendor_cache.json") if root else None


def _load_vendor_cache_unlocked(project_path: Path | None) -> dict[str, Any]:
    path = vendor_cache_path(project_path)
    if not path or not path.exists():
        return {"schema": VENDOR_CACHE_SCHEMA, "entries": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema": VENDOR_CACHE_SCHEMA, "entries": {}}
    if data.get("schema") != VENDOR_CACHE_SCHEMA or not isinstance(data.get("entries"), dict):
        return {"schema": VENDOR_CACHE_SCHEMA, "entries": {}}
    return data


def _save_vendor_cache_unlocked(project_path: Path | None, cache: dict[str, Any]) -> None:
    path = vendor_cache_path(project_path)
    if not path:
        return
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def vendor_cache_key(
    *,
    vendor_id: str,
    mpn: str,
    locale_block: dict[str, Any],
) -> str:
    dk_api = (locale_block or {}).get("digikey_api") or {}
    locale_sig = "|".join(
        str(x or "")
        for x in (
            dk_api.get("site"),
            dk_api.get("lang"),
            dk_api.get("currency"),
            (locale_block or {}).get("currency"),
        )
    )
    return f"{vendor_id}|{locale_sig}|{norm(mpn)}"


def _copy_cached_vendor_result(entry: dict[str, Any], cache_status: str) -> dict[str, Any]:
    result = dict(entry.get("result") or {})
    result["cache_hit"] = True
    result["cache_status"] = cache_status
    result["cached_at"] = entry.get("stored_at_iso")
    result["cache_age_sec"] = round(time.time() - float(entry.get("stored_at", 0.0)), 1)
    return result


def get_cached_vendor_result(
    *,
    project_path: Path | None,
    vendor_id: str,
    mpn: str,
    locale_block: dict[str, Any],
    allow_stale: bool = False,
) -> dict[str, Any] | None:
    key = vendor_cache_key(vendor_id=vendor_id, mpn=mpn, locale_block=locale_block)
    max_age = VENDOR_CACHE_STALE_FALLBACK_SEC if allow_stale else VENDOR_CACHE_TTL_SEC
    with VENDOR_CACHE_LOCK:
        cache = _load_vendor_cache_unlocked(project_path)
        entry = (cache.get("entries") or {}).get(key)
    if not isinstance(entry, dict):
        return None
    try:
        age = time.time() - float(entry.get("stored_at", 0.0))
    except (TypeError, ValueError):
        return None
    if age < 0 or age > max_age:
        return None
    result = entry.get("result")
    if not isinstance(result, dict):
        return None
    return _copy_cached_vendor_result(
        entry,
        "stale_after_fetch_error" if allow_stale and age > VENDOR_CACHE_TTL_SEC else "fresh",
    )


def save_cached_vendor_result(
    *,
    project_path: Path | None,
    vendor_id: str,
    mpn: str,
    locale_block: dict[str, Any],
    result: dict[str, Any],
) -> None:
    if result.get("status") not in {"active", "no_match", "nrnd"}:
        return
    key = vendor_cache_key(vendor_id=vendor_id, mpn=mpn, locale_block=locale_block)
    clean = {
        k: v
        for k, v in result.items()
        if k not in {"cache_hit", "cache_status", "cached_at", "cache_age_sec", "live_error"}
    }
    entry = {
        "stored_at": time.time(),
        "stored_at_iso": now_iso(),
        "vendor_id": vendor_id,
        "mpn": mpn,
        "result": clean,
    }
    with VENDOR_CACHE_LOCK:
        cache = _load_vendor_cache_unlocked(project_path)
        entries = cache.setdefault("entries", {})
        entries[key] = entry
        _save_vendor_cache_unlocked(project_path, cache)


def _firecrawl_fetch(url: str) -> tuple[int | None, str, str | None]:
    """Fetch via Firecrawl API (handles JS rendering and anti-bot)."""
    if not FIRECRAWL_API_KEY:
        return None, "", "firecrawl_no_key"
    payload = json.dumps({
        "url": url,
        "formats": ["html"],
        "waitFor": 2000,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.firecrawl.dev/v1/scrape",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=FIRECRAWL_TIMEOUT_SEC) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="ignore"))
        if body.get("success"):
            html = body.get("data", {}).get("html", "")
            return 200, html, None
        return None, "", f"firecrawl_fail:{body.get('error','unknown')}"
    except Exception as exc:
        return None, "", f"firecrawl_{type(exc).__name__}"


def fetch_html(url: str) -> tuple[int | None, str, str | None]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,en-US;q=0.8,en;q=0.6,zh-CN;q=0.4",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SEC) as resp:
            raw = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.status, raw.decode(charset, errors="ignore"), None
    except urllib.error.HTTPError as exc:
        code = exc.code
        if code in (403, 429, 503):
            fc_status, fc_html, fc_err = _firecrawl_fetch(url)
            if fc_html:
                return fc_status, fc_html, None
        try:
            body = exc.read().decode("utf-8", errors="ignore")
        except Exception:
            body = ""
        return code, body, f"http_error_{code}"
    except Exception as exc:
        fc_status, fc_html, fc_err = _firecrawl_fetch(url)
        if fc_html:
            return fc_status, fc_html, None
        return None, "", type(exc).__name__


def fetch_html_playwright_current(url: str) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception as exc:
        return {
            "ok": False,
            "http_status": None,
            "html": "",
            "error": f"playwright_import_failed:{type(exc).__name__}",
        }

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=DEFAULT_USER_AGENT,
                locale="ja-JP",
                viewport={"width": 1366, "height": 900},
            )
            page = context.new_page()
            response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            html = page.content()
            final_url = page.url
            title = page.title()
            status = response.status if response else None
            context.close()
            browser.close()
            return {
                "ok": True,
                "http_status": status,
                "html": html,
                "final_url": final_url,
                "title": title,
                "error": None,
            }
    except Exception as exc:
        return {
            "ok": False,
            "http_status": None,
            "html": "",
            "error": f"playwright_fetch_failed:{type(exc).__name__}: {exc}",
        }


def fetch_html_playwright(url: str) -> tuple[int | None, str, str | None, dict[str, Any]]:
    result = fetch_html_playwright_current(url)
    if result.get("ok") or "playwright_import_failed" not in str(result.get("error")):
        meta = {
            "driver": "playwright",
            "final_url": result.get("final_url"),
            "title": result.get("title"),
        }
        return result.get("http_status"), result.get("html", ""), result.get("error"), meta

    if VENV_PYTHON.exists() and Path(sys.executable).resolve() != VENV_PYTHON.resolve():
        try:
            proc = subprocess.run(
                [
                    str(VENV_PYTHON),
                    str(Path(__file__).resolve()),
                    "--playwright-fetch-json",
                    url,
                ],
                cwd=str(WORKSPACE_ROOT),
                text=True,
                capture_output=True,
                timeout=45,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                nested = json.loads(proc.stdout)
                meta = {
                    "driver": "playwright",
                    "via": str(VENV_PYTHON),
                    "final_url": nested.get("final_url"),
                    "title": nested.get("title"),
                }
                return nested.get("http_status"), nested.get("html", ""), nested.get("error"), meta
            return (
                None,
                "",
                f"playwright_subprocess_failed:{proc.returncode}:{proc.stderr[-300:]}",
                {"driver": "playwright", "via": str(VENV_PYTHON)},
            )
        except Exception as exc:
            return (
                None,
                "",
                f"playwright_subprocess_exception:{type(exc).__name__}: {exc}",
                {"driver": "playwright", "via": str(VENV_PYTHON)},
            )

    return None, "", result.get("error"), {"driver": "playwright"}


def fetch_html_with_driver(url: str, driver: str) -> tuple[int | None, str, str | None, dict[str, Any]]:
    if driver == "urllib":
        status, html, error = fetch_html(url)
        return status, html, error, {"driver": "urllib"}
    if driver == "playwright":
        return fetch_html_playwright(url)

    status, html, error, meta = fetch_html_playwright(url)
    if html or not str(error or "").startswith("playwright_import_failed"):
        return status, html, error, meta
    status2, html2, error2 = fetch_html(url)
    return status2, html2, error2, {"driver": "urllib", "fallback_from": error}


def first_marker(text_low: str, markers: tuple[str, ...]) -> str | None:
    for marker in markers:
        if marker and marker.lower() in text_low:
            return marker
    return None


def first_pattern_number(text: str, patterns: tuple[str, ...]) -> float | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            value = parse_num(match.group(1))
            if value is not None:
                return value
    return None


def classify_html(
    *,
    vendor_id: str,
    mpn: str,
    html: str,
    http_status: int | None = 200,
    url: str | None = None,
) -> dict[str, Any]:
    adapter = VENDOR_ADAPTERS.get(vendor_id) or VendorAdapter(
        vendor_id,
        nrnd_markers=COMMON_NRND,
        active_markers=COMMON_ACTIVE,
        no_match_markers=COMMON_NO_MATCH,
        stock_patterns=COMMON_STOCK_PATTERNS,
        price_patterns=COMMON_PRICE_PATTERNS,
    )
    text_low = html.lower()
    normalized_html = norm(html)
    exact_seen = norm(mpn) in normalized_html if mpn else False
    nrnd_marker = first_marker(text_low, adapter.nrnd_markers + COMMON_NRND)
    no_match_marker = first_marker(text_low, adapter.no_match_markers + COMMON_NO_MATCH)
    active_marker = first_marker(text_low, adapter.active_markers + COMMON_ACTIVE)
    stock = first_pattern_number(html, adapter.stock_patterns + COMMON_STOCK_PATTERNS)
    price = first_pattern_number(html, adapter.price_patterns + COMMON_PRICE_PATTERNS)

    base = {
        "vendor_id": vendor_id,
        "url": url,
        "http_status": http_status,
        "exact_mpn_seen": exact_seen,
        "stock": int(stock) if stock is not None and stock == math.floor(stock) else stock,
        "price": price,
        "fetched_at": now_iso(),
    }

    if http_status is None:
        return {**base, "status": "fetch_error", "reason": "network_error"}
    if http_status >= 500:
        return {**base, "status": "fetch_error", "reason": f"http_{http_status}"}
    if http_status == 404:
        return {**base, "status": "no_match", "reason": "http_404"}
    if not html:
        return {**base, "status": "fetch_error", "reason": "empty_body"}

    if no_match_marker:
        return {
            **base,
            "status": "no_match",
            "reason": "no_match_marker",
            "marker": no_match_marker,
        }
    if exact_seen and (stock is not None or price is not None):
        if nrnd_marker:
            return {
                **base,
                "status": "nrnd",
                "reason": "nrnd_marker",
                "marker": nrnd_marker,
            }
        return {
            **base,
            "status": "active",
            "reason": "exact_mpn_with_stock_or_price",
            "marker": active_marker,
        }
    if exact_seen and active_marker:
        return {
            **base,
            "status": "fetch_error",
            "reason": "adapter_incomplete_fields",
            "marker": active_marker,
        }
    if exact_seen:
        return {
            **base,
            "status": "fetch_error",
            "reason": "exact_mpn_but_no_stock_or_price",
        }
    return {**base, "status": "no_match", "reason": "exact_mpn_not_found"}


def import_library_probe() -> Any | None:
    # V2 has its own library_probe.py with cache+retry. Import from V2 dir first;
    # fall back to V1's only if V2's copy was deleted.
    for candidate in (V2_SCRIPTS, OLD_SCRIPTS):
        if not candidate.exists():
            continue
        sys.path.insert(0, str(candidate))
        try:
            import library_probe  # type: ignore
            return library_probe
        except Exception:
            sys.path.remove(str(candidate))
            continue
    return None


def _dk_part_from_vendor_results(vendor_results: list[dict[str, Any]] | None) -> str | None:
    for v in vendor_results or []:
        if v.get("vendor_id", "").startswith("digikey") and v.get("dk_part_number"):
            return str(v["dk_part_number"])
    return None


def probe_library(
    candidate: dict[str, Any],
    locale_block: dict[str, Any],
    *,
    vendor_results: list[dict[str, Any]] | None = None,
    include_network_probes: bool = True,
    include_eda_models_probe: bool = False,
) -> dict[str, Any]:
    """Probe whether the part's KiCad library assets are obtainable.

    Default order (priority high → low) inside `library_probe.probe()`:
      1. lib_external/  (already vendored)            → vendored_complete
      2. KiCad official symbols+footprints           → standard_ready
      3. lib_cache/ mirrors                          → external_cache_exact
      4. LCSC dry-run via download_lcsc_lib.py       → lcsc_vendorable
      5. nothing found                               → unavailable

    Step 4 requires `include_network_probes=True`. DigiKey /media EDA model
    probing is metadata-only and off by default; component-preparing owns asset
    acquisition.
    """
    mpn = str(candidate.get("mpn", "")).strip()
    precomputed = candidate.get("library_status")
    dk_id = candidate.get("dk_part_id") or _dk_part_from_vendor_results(vendor_results)
    if precomputed:
        result = {
            "status": precomputed,
            "package_consistency": {
                "status": candidate.get("package_consistency", "unknown"),
                "source": "input_longlist",
            },
            "source": "input_longlist",
        }
    else:
        module = import_library_probe()
        if module is None:
            result = {
                "status": "unavailable",
                "package_consistency": {
                    "status": "unknown",
                    "reason": "library_probe_import_failed",
                },
                "source": "version2_fallback",
            }
        else:
            try:
                result = module.probe(
                    mpn,
                    package_hint=candidate.get("package_hint") or candidate.get("package"),
                    distributor_package=candidate.get("distributor_package"),
                    distributor_mounting=candidate.get("distributor_mounting"),
                    datasheet_package=candidate.get("datasheet_package"),
                    locale_block=locale_block,
                    dk_part_id=dk_id,
                    include_network_probes=include_network_probes,
                )
            except Exception as exc:
                result = {
                    "status": "unavailable",
                    "package_consistency": {"status": "unknown", "reason": type(exc).__name__},
                    "source": "library_probe_exception",
                }
    if include_eda_models_probe and dk_id:
        result["eda_models_index"] = query_digikey_media_eda_models(dk_id)
    return result


def user_soldering_text(user_md_path: Path) -> str:
    if not user_md_path.exists():
        return ""
    text = user_md_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "焊接能力" in line:
            return line
    return ""


def package_text(candidate: dict[str, Any], library: dict[str, Any]) -> str:
    fields = [
        candidate.get("package_hint"),
        candidate.get("package"),
        candidate.get("distributor_package"),
        candidate.get("datasheet_package"),
        candidate.get("lcsc_global", {}).get("package") if isinstance(candidate.get("lcsc_global"), dict) else None,
    ]
    evidence = library.get("decision_evidence") or []
    for item in evidence[:3]:
        if isinstance(item, dict):
            fields.extend([item.get("path"), item.get("match")])
    return " ".join(str(f) for f in fields if f)


def classify_package(pkg_text: str) -> str:
    low = pkg_text.lower()
    if any(t in low for t in ("bga", "qfn", "dfn", "lga", "wlcsp", "0201")):
        return "reflow_only"
    if "0402" in low or "qfp-0.5" in low or "0.4mm" in low:
        return "hand_solder_hard"
    if any(t in low for t in ("through", "tht", "dip", "sip", "to-220", "terminal", "connector", "0805", "1206", "1210")):
        return "hand_solder_easy"
    if any(t in low for t in ("soic", "sop", "tssop", "ssop", "sot-223", "sot223", "sot-23", "sot23", "0603")):
        return "hand_solder_ok"
    return "unknown"


def solderability_gate(candidate: dict[str, Any], library: dict[str, Any], user_md_path: Path) -> dict[str, Any]:
    line = user_soldering_text(user_md_path)
    if not line or ("[待填]" in line and not any(k in line for k in ("0603", "SOIC", "SOP", "TSSOP", "通孔"))):
        return {"status": "pending_user_input", "package_class": "unknown", "reason": "USER.md soldering capability missing"}
    pkg_text = package_text(candidate, library)
    pkg_class = classify_package(pkg_text)
    line_low = line.lower()
    has_advanced_tools = any(k in line for k in ("热风", "回流", "显微镜")) or any(
        k in line_low for k in ("hot air", "reflow", "microscope")
    )

    if pkg_class in {"hand_solder_easy", "hand_solder_ok"}:
        return {"status": "pass", "package_class": pkg_class, "reason": "compatible_with_USER_md"}
    if pkg_class == "hand_solder_hard":
        return {
            "status": "warn" if has_advanced_tools else "fail",
            "package_class": pkg_class,
            "reason": "requires_advanced_soldering",
        }
    if pkg_class == "reflow_only":
        return {"status": "fail", "package_class": pkg_class, "reason": "reflow_only_package"}
    return {"status": "warn", "package_class": "unknown", "reason": "package_unknown"}


def library_gate(library: dict[str, Any]) -> dict[str, Any]:
    status = library.get("status", "unavailable")
    pc = library.get("package_consistency") or {}
    pc_status = pc.get("status", "unknown")
    if status in GOOD_LIBRARY_STATUSES and pc_status != "fail":
        return {"status": "pass", "reason": status}
    return {"status": "fail", "reason": status, "package_consistency": pc_status}


def buyable_gate(
    vendor_results: list[dict[str, Any]],
    *,
    stock_threshold: int,
    fetched: bool,
    min_local_sources: int = 2,
) -> dict[str, Any]:
    if not fetched:
        return {"status": "pending_web_data", "reason": "run_with_--fetch-web"}
    active = [v for v in vendor_results if v.get("status") == "active"]
    nrnd = [v for v in vendor_results if v.get("status") == "nrnd"]
    if nrnd:
        return {
            "status": "fail",
            "reason": "nrnd_marker_seen",
            "active_count": len(active),
            "nrnd_count": len(nrnd),
        }
    if len(active) >= 2:
        return {"status": "pass", "reason": "two_or_more_local_sources", "active_count": len(active)}
    if len(active) == 1:
        stock = active[0].get("stock") or 0
        if isinstance(stock, (int, float)) and stock >= stock_threshold:
            # Single-source locales (gate_policy.min_local_sources: 1, e.g. an
            # LCSC-only lane) treat one healthy source as a real pass — there
            # is no second structured source to cross-check against.
            if min_local_sources <= 1:
                return {
                    "status": "pass",
                    "reason": "single_source_meets_locale_policy",
                    "active_count": 1,
                    "stock": stock,
                }
            return {
                "status": "warn_single_source",
                "reason": "one_local_source_above_stock_threshold",
                "active_count": 1,
                "stock": stock,
            }
        return {
            "status": "fail",
            "reason": "single_source_below_stock_threshold",
            "active_count": 1,
            "stock": stock,
        }
    if any(v.get("status") == "fetch_error" for v in vendor_results):
        return {"status": "fail", "reason": "unverifiable_fetch_error", "active_count": 0}
    return {"status": "fail", "reason": "no_active_local_sources", "active_count": 0}


def determine_verdict(buyable: dict[str, Any], library: dict[str, Any], solder: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if buyable["status"] == "pending_web_data":
        reasons.append("web data pending")
        return "pending_web_data", reasons
    if solder["status"] == "pending_user_input":
        reasons.append(solder["reason"])
        return "pending_user_input", reasons
    if buyable["status"] == "fail":
        reasons.append(f"buyable: {buyable.get('reason')}")
    if library["status"] == "fail":
        reasons.append(f"library: {library.get('reason')}")
    if solder["status"] == "fail":
        reasons.append(f"solderability: {solder.get('reason')}")
    if reasons:
        return "fail", reasons
    if buyable["status"] == "warn_single_source" or solder["status"] == "warn":
        if buyable["status"] == "warn_single_source":
            reasons.append("single local source")
        if solder["status"] == "warn":
            reasons.append(f"solderability warning: {solder.get('reason')}")
        return "warn_single_source", reasons
    return "pass", ["all hard gates passed"]


def extract_candidates(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        raw = data.get("longlist") or data.get("candidates") or data.get("parts")
        if raw is None and data.get("mpn"):
            raw = [data]
    else:
        raw = None
    if raw is None:
        raise ValueError("longlist JSON must contain a list or a 'longlist' array")
    out: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            out.append({"mpn": item})
        elif isinstance(item, dict) and item.get("mpn"):
            out.append(dict(item))
        else:
            raise ValueError(f"invalid candidate item: {item!r}")
    return out


def normalize_role(value: Any) -> str:
    return str(value or "").strip().lower()


def looks_like_refdes_role(value: Any) -> bool:
    return bool(REFDES_ROLE_RE.match(str(value or "").strip()))


def declared_role_from_input(data: Any, candidates: list[dict[str, Any]]) -> str:
    """Return the functional role declared by longlist JSON, if unambiguous."""
    if isinstance(data, dict):
        role = normalize_role(data.get("role"))
        if role:
            return role
        spec = data.get("spec")
        if isinstance(spec, dict):
            role = normalize_role(spec.get("role"))
            if role:
                return role
        expected = normalize_role(data.get("expected_role"))
        if expected and not looks_like_refdes_role(expected):
            return expected

    candidate_roles = {
        normalize_role(c.get("role") or c.get("expected_role"))
        for c in candidates
        if (
            normalize_role(c.get("role") or c.get("expected_role"))
            and not looks_like_refdes_role(c.get("role") or c.get("expected_role"))
        )
    }
    if len(candidate_roles) == 1:
        return next(iter(candidate_roles))
    return ""


def resolve_effective_role(
    args: argparse.Namespace,
    input_data: Any,
    candidates: list[dict[str, Any]],
) -> tuple[str, str | None]:
    """Resolve the semantic role used for profile/generic-passive logic.

    Historical runs sometimes passed schematic refdes (R16B/R3/U2) through
    --expected-role. When that happens, prefer the longlist's functional role.
    A deliberate non-refdes --expected-role (for example "component") still
    wins, so callers can force the original per-MPN library probe.
    """
    expected = normalize_role(args.expected_role)
    role_arg = normalize_role(getattr(args, "role", ""))
    declared = declared_role_from_input(input_data, candidates)

    if expected and not looks_like_refdes_role(expected):
        return expected, None

    fallback = role_arg if role_arg and not looks_like_refdes_role(role_arg) else declared
    if expected and fallback:
        return (
            fallback,
            f"--expected-role={args.expected_role!r} looks like refdes; using role={fallback!r}",
        )
    if expected:
        return expected, None
    if fallback:
        return fallback, None
    return "", None


def collect_vendor_results(
    *,
    mpn: str,
    vendor_urls: list[dict[str, str]],
    fetch_web: bool,
    locale_block: dict[str, Any],
    project_path: Path | None = None,
    short_circuit: bool = True,
    web_driver: str = "auto",
    use_vendor_cache: bool = True,
) -> list[dict[str, Any]]:
    """Query each vendor through its structured API adapter.

    API vendors (digikey_*, mouser_*, lcsc) are queried by default. Non-API
    vendors are present only when --include-html-vendors added them to
    vendor_urls; those are slower and lower-signal.

    `short_circuit` causes later vendors to be marked `skipped_after_pass` once
    two active sources are confirmed.
    """
    if not fetch_web:
        return [
            {
                "vendor_id": item["vendor_id"],
                "name": item["name"],
                "url": item["url"],
                "status": "pending",
                "reason": "fetch_web_disabled",
            }
            for item in vendor_urls
        ]

    results: list[dict[str, Any]] = []
    active_count = 0
    for item in vendor_urls:
        vendor_id = item["vendor_id"]
        kind = API_VENDOR_KIND.get(vendor_id)
        # Core lanes (DK_JP / Mouser_JP / LCSC) are always queried — each
        # represents a distinct fulfilment path the user picks between at
        # order time. Only long-tail HTML scrapers honor short_circuit.
        is_core_lane = vendor_id in CORE_API_LANES
        if short_circuit and active_count >= 2 and not is_core_lane:
            results.append(
                {
                    **item,
                    "status": "skipped_after_pass",
                    "reason": "two_active_sources_already_found",
                    "fetched_at": now_iso(),
                    "lane": _lane_of(vendor_id, locale_block),
                }
            )
            continue
        if kind:
            res = None
            if use_vendor_cache:
                res = get_cached_vendor_result(
                    project_path=project_path,
                    vendor_id=vendor_id,
                    mpn=mpn,
                    locale_block=locale_block,
                )
            if res is None:
                if kind == "digikey":
                    res = query_digikey_api_for_vendor(
                        vendor_id=vendor_id,
                        mpn=mpn,
                        vendor_url=item["url"],
                        vendor_name=item["name"],
                        locale_block=locale_block,
                    )
                elif kind == "mouser":
                    res = query_mouser_api_for_vendor(
                        vendor_id=vendor_id,
                        mpn=mpn,
                        vendor_url=item["url"],
                        vendor_name=item["name"],
                        locale_block=locale_block,
                    )
                elif kind == "lcsc":
                    res = query_lcsc_api_for_vendor(
                        vendor_id=vendor_id,
                        mpn=mpn,
                        vendor_url=item["url"],
                        vendor_name=item["name"],
                        locale_block=locale_block,
                    )
                else:
                    res = {**item, "status": "fetch_error", "reason": f"unknown_api_kind:{kind}"}
                if use_vendor_cache and res.get("status") == "fetch_error":
                    stale = get_cached_vendor_result(
                        project_path=project_path,
                        vendor_id=vendor_id,
                        mpn=mpn,
                        locale_block=locale_block,
                        allow_stale=True,
                    )
                    if stale is not None:
                        stale["live_error"] = {
                            "status": res.get("status"),
                            "reason": res.get("reason"),
                            "error": res.get("error"),
                        }
                        res = stale
                if use_vendor_cache and not res.get("cache_hit"):
                    save_cached_vendor_result(
                        project_path=project_path,
                        vendor_id=vendor_id,
                        mpn=mpn,
                        locale_block=locale_block,
                        result=res,
                    )
        elif FIRECRAWL_API_KEY:
            # Non-API vendor: scrape via Firecrawl + classify_html
            status_code, html, error = fetch_html(item["url"])
            res = classify_html(
                vendor_id=vendor_id,
                mpn=mpn,
                html=html or "",
                http_status=status_code,
                url=item["url"],
            )
            res["name"] = item["name"]
            if error:
                res["error"] = error
            res["fetch_driver"] = "firecrawl"
        else:
            res = {
                **item,
                "status": "skipped_no_api",
                "reason": "no_api_adapter_for_vendor",
                "fetched_at": now_iso(),
            }
        # Tag the lane so downstream consumers can filter without knowing
        # the vendor_id whitelist. Always present, even on fetch_error /
        # skipped_after_pass, so the field is a stable contract.
        res["lane"] = _lane_of(vendor_id, locale_block)
        results.append(res)
        if res.get("status") == "active":
            active_count += 1
    return results


def lowest_local_price(vendor_results: list[dict[str, Any]]) -> float | None:
    prices = [
        float(v["price"])
        for v in vendor_results
        if v.get("status") == "active" and isinstance(v.get("price"), (int, float))
    ]
    return min(prices) if prices else None


def best_stock(vendor_results: list[dict[str, Any]]) -> int | None:
    stocks = [
        int(v["stock"])
        for v in vendor_results
        if v.get("status") == "active" and isinstance(v.get("stock"), (int, float))
    ]
    return max(stocks) if stocks else None


# ---------------------------------------------------------------------------
# Generic derating (降额) evaluation
# ---------------------------------------------------------------------------
# Derating is a generic, project-agnostic electrical practice: never run a part
# at its Absolute-Maximum rating. The thresholds below are conventional
# industrial defaults (commercial/industrial reliability), NOT tuned to any
# project or any specific voltage:
#
#   voltage  : keep operating ≤ 80% of Abs-Max rating  (20% margin)
#   power    : keep operating ≤ 60% of Abs-Max rating  (mid of the common
#              50–70% band; conservative default for self-heating parts)
#   current  : keep operating ≤ 80% of Abs-Max rating  (20% margin)
#   temperature: keep ≥ 20 °C of headroom below the Abs-Max junction/operating
#              temperature (absolute headroom, not a ratio — ratios on °C are
#              meaningless)
#
# These are advisory warn-level checks. They never hard-fail a candidate (a
# derating call cannot override the buyable/library/solderability gates), and
# when the Abs-Max rating cannot be obtained the result is explicitly marked
# `unverified` rather than silently passed.
DERATING_VOLTAGE_MAX_FRAC = 0.80
DERATING_POWER_MAX_FRAC = 0.60
DERATING_CURRENT_MAX_FRAC = 0.80
DERATING_TEMP_MIN_HEADROOM_C = 20.0

# Which DK/Mouser parameter-text keys plausibly carry an Abs-Max rating for
# each operating-condition axis. Generic substrings (matched case-insensitively
# against the raw distributor parameter name), so this works across JP and EN
# parameter naming without binding to any role profile. "Max" filtering is
# applied on top so we prefer ceiling-type ratings over typical values.
_DERATING_ABSMAX_PARAM_HINTS: dict[str, tuple[str, ...]] = {
    "voltage": ("voltage", "vds", "vdss", "電圧", "drain to source",
                "drain-source", "reverse standoff", "breakdown"),
    "power": ("power", "watt", "電力", "ワット", "dissipation"),
    "current": ("current", "電流", "drain current", "id ", "(id)", "io ", "output current"),
    "temperature": ("operating temperature", "junction temperature",
                    "動作温度", "接合部温度", "tj", "temperature - operating"),
}


def _coerce_operating_conditions(candidate: dict[str, Any]) -> dict[str, float]:
    """Pull caller-supplied operating conditions off a candidate dict.

    Operating V/I/P/T do not flow into this script through argparse — there is
    no role-spec carrier for them. The only place they can ride in is the
    per-candidate dict from a longlist JSON, where the LLM may attach an
    `operating` block, e.g.

        {"mpn": "...", "operating": {"voltage": 24, "current": 0.5,
                                     "power": 1.2, "temperature": 85}}

    All keys optional; values are numbers in base SI units (V / A / W / °C).
    Anything unparseable is dropped. Returns {} when nothing usable is present.
    """
    op = candidate.get("operating")
    if not isinstance(op, dict):
        return {}
    out: dict[str, float] = {}
    for axis in ("voltage", "current", "power", "temperature"):
        val = op.get(axis)
        if isinstance(val, (int, float)):
            out[axis] = float(val)
        elif isinstance(val, str):
            num = parse_engineering_number(val)
            if num is not None:
                out[axis] = num
    return out


def _absmax_from_params(raw_params: dict[str, str], axis: str) -> tuple[float | None, str | None]:
    """Best-effort Abs-Max rating for one axis from distributor parameters.

    Returns (value_in_base_SI, source_param_name) or (None, None) when no
    plausible parameter is present. Prefers parameter names that contain a
    'max' marker; for temperature, parses the upper bound of a range like
    '-40°C ~ 125°C (TJ)'.
    """
    hints = _DERATING_ABSMAX_PARAM_HINTS.get(axis, ())
    best: tuple[float | None, str | None] = (None, None)
    best_is_max = False
    for name, value in raw_params.items():
        nl = str(name).lower()
        if not any(h in nl for h in hints):
            continue
        if axis == "temperature":
            nums = re.findall(r"-?[0-9]+(?:\.[0-9]+)?", str(value))
            if not nums:
                continue
            parsed = max(float(n) for n in nums)
        else:
            parsed = parse_engineering_number(value)
            if parsed is None:
                continue
        is_max = ("max" in nl) or ("最大" in str(name))
        # Prefer an explicit-max parameter; among same kind keep the larger
        # (the true ceiling), which avoids picking a 'min' or typical field.
        if best[0] is None or (is_max and not best_is_max) or (
            is_max == best_is_max and parsed > (best[0] or 0)
        ):
            best = (parsed, str(name))
            best_is_max = is_max
    return best


def derating_check(
    candidate: dict[str, Any],
    vendor_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Generic derating evaluation. Consumes the candidate's optional
    `operating` block + Abs-Max ratings recovered from distributor parameters.

    Graceful degradation: when operating conditions are absent OR no Abs-Max
    rating can be found for an axis, that axis is reported as
    `unverified` with an explicit reason — never silently passed.

    Returns a dict:
        {
          "status": "ok" | "warn" | "unverified" | "not_applicable",
          "axes": { "<axis>": {status, operating, abs_max, used_pct,
                               threshold_pct?, headroom_c?, source?, reason?} },
          "warnings": [str, ...],
        }
    `status` is the worst axis state (warn > unverified > ok). Advisory only.
    """
    operating = _coerce_operating_conditions(candidate)

    # Find the richest raw_parameters block among vendor results (DK API).
    raw_params: dict[str, str] = {}
    for v in vendor_results:
        rp = v.get("raw_parameters")
        if isinstance(rp, dict) and len(rp) > len(raw_params):
            raw_params = rp

    if not operating:
        return {
            "status": "unverified",
            "axes": {},
            "warnings": [
                "derating: unverified (no operating conditions supplied; "
                "attach an `operating` block {voltage/current/power/temperature} "
                "to the longlist candidate to enable derating)"
            ],
        }

    axes: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []

    frac_thresholds = {
        "voltage": DERATING_VOLTAGE_MAX_FRAC,
        "current": DERATING_CURRENT_MAX_FRAC,
        "power": DERATING_POWER_MAX_FRAC,
    }

    for axis, op_val in operating.items():
        abs_max, source = (
            _absmax_from_params(raw_params, axis) if raw_params else (None, None)
        )
        if abs_max is None:
            axes[axis] = {
                "status": "unverified",
                "operating": op_val,
                "abs_max": None,
                "reason": "Abs Max unavailable",
            }
            warnings.append(f"derating[{axis}]: unverified (Abs Max unavailable)")
            continue

        if axis == "temperature":
            headroom = abs_max - op_val
            ok = headroom >= DERATING_TEMP_MIN_HEADROOM_C
            axes[axis] = {
                "status": "ok" if ok else "warn",
                "operating": op_val,
                "abs_max": abs_max,
                "headroom_c": round(headroom, 2),
                "min_headroom_c": DERATING_TEMP_MIN_HEADROOM_C,
                "source": source,
            }
            if not ok:
                warnings.append(
                    f"derating[temperature]: operating {op_val:g}°C leaves only "
                    f"{headroom:g}°C headroom below Abs-Max {abs_max:g}°C "
                    f"(< {DERATING_TEMP_MIN_HEADROOM_C:g}°C)"
                )
        else:
            thr = frac_thresholds[axis]
            used = (op_val / abs_max) if abs_max else None
            ok = used is not None and used <= thr
            axes[axis] = {
                "status": "ok" if ok else "warn",
                "operating": op_val,
                "abs_max": abs_max,
                "used_pct": round(used * 100, 1) if used is not None else None,
                "threshold_pct": round(thr * 100, 1),
                "source": source,
            }
            if not ok and used is not None:
                warnings.append(
                    f"derating[{axis}]: operating {op_val:g} is {used * 100:.0f}% "
                    f"of Abs-Max {abs_max:g} (> {thr * 100:.0f}% threshold)"
                )

    axis_states = {a["status"] for a in axes.values()}
    if "warn" in axis_states:
        status = "warn"
    elif "unverified" in axis_states or not axes:
        status = "unverified"
    else:
        status = "ok"

    return {"status": status, "axes": axes, "warnings": warnings}


_GENERIC_PARAM_KEYWORDS = (
    # JP keywords (DigiKey JP returns these)
    "電圧", "電流", "電力", "ワット", "絶縁", "出力", "入力", "周波数",
    "効率", "リップル", "温度", "ピッチ", "ポジション", "チャンネル",
    "取り付けタイプ", "パッケージ", "ケース",
    # EN keywords (DigiKey en-US returns these)
    "voltage", "current", "power", "watt", "isolation", "output", "input",
    "frequency", "efficiency", "ripple", "temperature", "pitch", "position",
    "channel", "mounting", "package", "case", "rds", "vds", "qg",
)


def _extract_key_params(vendor_results: list[dict[str, Any]], role: str | None) -> dict[str, str]:
    """Extract key electrical parameters from DigiKey API raw_parameters.

    Two-tier strategy:
      1. If role is registered in ROLE_PROFILES → use curated aliases (clean,
         predictable keys like 'isolation' / 'vout').
      2. If role is unknown / unregistered → fall back to a generic keyword
         filter so the LLM still gets *something* useful instead of {}.
         Keys are kept as-is from DK (Japanese or English).
    """
    profile = role_profile(role)
    aliases = profile.get("parameter_aliases", {}) if profile else {}

    # Find a vendor result with raw_parameters (DigiKey API, or LCSC when the
    # locale opts into lcsc_attributes_as_parameters)
    raw_params: dict[str, str] = {}
    raw_params_vendor = ""
    for v in vendor_results:
        rp = v.get("raw_parameters")
        if isinstance(rp, dict) and rp:
            raw_params = rp
            raw_params_vendor = str(v.get("vendor_id") or "")
            break
    if not raw_params:
        return {}

    if aliases:
        out = {
            key: first_param(raw_params, tuples)
            for key, tuples in aliases.items()
            if first_param(raw_params, tuples)
        }
        out["_role_recognized"] = "true"
        return out

    # Generic fallback: any param whose key contains a recognised keyword.
    # Cap at 16 entries to avoid bloating the JSON for parts with 60+ params.
    # LCSC-sourced params get passive-term keywords on top — the DK-driven JP
    # fallback output stays unchanged.
    keywords = _GENERIC_PARAM_KEYWORDS
    if raw_params_vendor == "lcsc":
        keywords = keywords + ("resistance", "capacitance", "inductance", "impedance", "tolerance", "esr")
    generic: dict[str, str] = {}
    for k, val in raw_params.items():
        if not val:
            continue
        kl = str(k).lower()
        if any(kw.lower() in kl for kw in keywords):
            generic[str(k)] = str(val)
            if len(generic) >= 16:
                break
    if generic:
        generic["_role_recognized"] = "false_fallback"
    return generic


def evaluate_candidate(
    candidate: dict[str, Any],
    *,
    locale_name: str,
    locale_block: dict[str, Any],
    project_path: Path | None,
    expected_role: str | None,
    fetch_web: bool,
    short_circuit: bool,
    long_term_supply: bool,
    web_driver: str,
    library_network_probes: bool = True,
    only_vendors: set[str] | None = None,
    include_html_vendors: bool = False,
    use_vendor_cache: bool = True,
    probe_eda_models: bool = False,
) -> dict[str, Any]:
    mpn = str(candidate["mpn"]).strip()
    vendor_urls = build_vendor_urls(
        mpn,
        locale_block,
        only_vendors=only_vendors,
        include_html_vendors=include_html_vendors,
    )
    vendor_results = collect_vendor_results(
        mpn=mpn,
        vendor_urls=vendor_urls,
        fetch_web=fetch_web,
        locale_block=locale_block,
        project_path=project_path,
        short_circuit=short_circuit,
        web_driver=web_driver,
        use_vendor_cache=use_vendor_cache,
    )
    stock_block = locale_block.get("stock_threshold", {}) or {}
    threshold_key = "long_term" if long_term_supply else "short_term"
    threshold = int(stock_block.get(threshold_key, 10))
    gate_policy = locale_block.get("gate_policy") or {}
    buyable = buyable_gate(
        vendor_results,
        stock_threshold=threshold,
        fetched=fetch_web,
        min_local_sources=int(gate_policy.get("min_local_sources", 2)),
    )

    # Speed: skip the library network probe (LCSC dry-run + UL session, ~5s)
    # whenever buyable already says fail. The final verdict will be fail
    # anyway, and library status would not change that. Local cache scan is
    # cheap (<1s) so we still run it for visibility, but with network probes
    # disabled.
    skip_library_network = buyable.get("status") == "fail"

    # Enrich candidate with package info from DigiKey API raw_parameters
    for v in vendor_results:
        rp = v.get("raw_parameters", {})
        if isinstance(rp, dict) and rp:
            if not candidate.get("package_hint"):
                candidate["package_hint"] = rp.get("Package / Case") or rp.get("パッケージ/ケース") or ""
            if not candidate.get("distributor_package"):
                candidate["distributor_package"] = candidate.get("package_hint")
            if not candidate.get("distributor_mounting"):
                candidate["distributor_mounting"] = rp.get("Mounting Type") or rp.get("取り付けタイプ") or ""
            break
    # Fallback: LCSC carries package as a plain field, not raw_parameters —
    # without this, LCSC-only candidates hit solderability package_unknown.
    if not candidate.get("package_hint"):
        for v in vendor_results:
            if v.get("package"):
                candidate["package_hint"] = str(v["package"])
                if not candidate.get("distributor_package"):
                    candidate["distributor_package"] = candidate["package_hint"]
                break

    # Reuse Phase 1 library probe if present (saves a redundant scan).
    # Phase 1 ran without vendor_results (because vendor calls hadn't happened
    # yet). If buyable found a vendor, re-probe with vendor_results so the
    # library_probe can use distributor/datasheet package consistency checks.
    # Exception: passive_generic is a deliberate Phase-1 short-circuit for
    # generic-passive roles — never re-probe, always keep the synthetic pass.
    cached_library = candidate.pop("_phase1_library", None)
    candidate.pop("_phase1_library_gate", None)
    is_passive_generic_cached = (
        isinstance(cached_library, dict)
        and cached_library.get("status") == "passive_generic"
    )
    if cached_library is not None and (not vendor_results or is_passive_generic_cached):
        library = cached_library
    else:
        library = probe_library(
            candidate,
            locale_block,
            vendor_results=vendor_results,
            include_network_probes=library_network_probes and not skip_library_network,
            include_eda_models_probe=probe_eda_models,
        )
        if skip_library_network and library_network_probes:
            library["library_network_skipped"] = "buyable_already_failed"
    solder = solderability_gate(candidate, library, USER_MD)
    lib_gate = library_gate(library)
    verdict, reasons = determine_verdict(buyable, lib_gate, solder)

    # Extract key parameters from DigiKey API vendor result
    key_params = _extract_key_params(vendor_results, expected_role)

    # Generic derating evaluation (advisory). Consumes any caller-supplied
    # `operating` block on the candidate + Abs-Max ratings from distributor
    # parameters. Never affects verdict; degrades to `unverified` on missing
    # data instead of silently passing.
    derating = derating_check(candidate, vendor_results)

    # Lane indicators: distinguish "stocked in Japan" from "only via JLCPCB
    # co-order on LCSC". Purely informational — buyable_gate already counts
    # LCSC as buyable. User-facing output uses these flags to hint that an
    # LCSC-only winner means a 5-day DHL lane via JLCPCB.
    local_ids = _local_vendor_ids(locale_block)
    local_jp_active = any(
        v.get("status") == "active" and v.get("vendor_id") in local_ids
        for v in vendor_results
    )
    lcsc_active = any(
        v.get("status") == "active" and v.get("vendor_id") == "lcsc"
        for v in vendor_results
    )
    lcsc_only_active = lcsc_active and not local_jp_active

    # Collect product detail URLs from API vendor results
    product_urls = {
        v["vendor_id"]: v["final_url"]
        for v in vendor_results
        if v.get("final_url") and v.get("status") in ("active", "nrnd")
    }

    # Collect datasheet URLs (vendor → URL) so Phase 2.5 bulk_fetch can
    # download datasheets without re-querying the DK keyword search API.
    datasheet_urls = {
        v["vendor_id"]: v["datasheet_url"]
        for v in vendor_results
        if v.get("datasheet_url") and v.get("status") in ("active", "nrnd")
    }

    result = {
        "mpn": mpn,
        "expected_role": expected_role or candidate.get("role"),
        "locale": locale_name,
        "currency": locale_block.get("currency", "USD"),
        "evaluated_at": now_iso(),
        "verdict": verdict,
        "reason": "; ".join(reasons),
        "input": candidate,
        "vendor_urls": vendor_urls,
        "vendor_results": vendor_results,
        "product_urls": product_urls,
        "datasheet_urls": datasheet_urls,
        "buyable_gate": buyable,
        "library": library,
        "library_gate": lib_gate,
        "solderability_gate": solder,
        "key_parameters": key_params,
        "derating": derating,
        "local_price": lowest_local_price(vendor_results),
        "local_stock": best_stock(vendor_results),
        "local_jp_active": local_jp_active,
        "lcsc_only_active": lcsc_only_active,
        "llm_review": {
            "status": "not_run",
            "instruction": "LLM may only block or request rerun; it must not override hard fail to pass.",
        },
    }
    # JP-lane diagnostic flags only make sense where a "domestic vs LCSC
    # co-order" distinction exists; single-lane locales suppress them.
    display_cfg = locale_block.get("display") or {}
    if str(display_cfg.get("emit_lane_flags", "jp")) != "jp":
        del result["local_jp_active"]
        del result["lcsc_only_active"]
    # Locales without a lifecycle data source label every result honestly
    # instead of pretending an NRND check happened.
    if str(gate_policy.get("lifecycle_policy", "api")) == "unverified":
        result["lifecycle"] = "unverified"
    return result


def add_price_tags(results: list[dict[str, Any]]) -> None:
    usable_prices = [
        r["local_price"]
        for r in results
        if r.get("verdict") in {"pass", "warn_single_source"}
        and isinstance(r.get("local_price"), (int, float))
    ]
    median = statistics.median(usable_prices) if usable_prices else None
    for result in results:
        price = result.get("local_price")
        if median is None or not isinstance(price, (int, float)):
            result["price_tag"] = "unknown"
            continue
        ratio = price / median if median else 1.0
        if ratio <= 0.75:
            result["price_tag"] = "cheap"
        elif ratio <= 1.4:
            result["price_tag"] = "normal"
        elif ratio <= 2.5:
            result["price_tag"] = "premium"
        else:
            result["price_tag"] = "outlier"


def rank_results(results: list[dict[str, Any]]) -> None:
    library_score = {
        "vendored_complete": 0,
        "standard_ready": 1,
        "external_cache_exact": 2,
        "lcsc_vendorable": 3,
        "browser_vendorable": 4,
        "external_cache_compatible": 8,
        "unavailable": 9,
    }
    price_score = {"cheap": 0, "normal": 1, "premium": 2, "outlier": 3, "unknown": 4}

    def key(item: dict[str, Any]) -> tuple[Any, ...]:
        active_count = sum(1 for v in item.get("vendor_results", []) if v.get("status") == "active")
        return (
            VERDICT_ORDER.get(item.get("verdict"), 9),
            price_score.get(item.get("price_tag"), 4),
            -active_count,
            library_score.get(item.get("library", {}).get("status"), 9),
            item.get("local_price") if isinstance(item.get("local_price"), (int, float)) else 10**12,
            item.get("mpn", ""),
        )

    results.sort(key=key)
    for idx, item in enumerate(results, start=1):
        item["rank"] = idx


def summary_lines(payload: dict[str, Any]) -> list[str]:
    if "longlist" in payload and "results" not in payload:
        lines = [
            "Component Selecting Version 2 discover summary",
            f"role={payload.get('role')} locale={payload.get('locale')} count={payload.get('count')}",
            f"keywords={', '.join(payload.get('keywords', []))}",
        ]
        for item in payload.get("longlist", [])[:12]:
            params = item.get("key_parameters") or {}
            key_bits = " ".join(
                str(v)
                for v in (
                    params.get("vin_min"),
                    params.get("vout"),
                    params.get("isolation"),
                    params.get("power"),
                )
                if v
            )
            if not key_bits and params:
                # Passive/parametric rows carry category-typed keys instead of
                # the fixed power-IC quartet — show the first few generically.
                key_bits = " ".join(f"{k}={v}" for k, v in list(params.items())[:3])
            lines.append(
                f"{item.get('discover_rank')}. {item.get('mpn')} "
                f"source={item.get('source')} stock={item.get('stock')} "
                f"price={item.get('price')} pkg={item.get('package') or item.get('package_hint')} "
                f"params={key_bits}"
            )
        if payload.get("source_notes"):
            lines.append("notes=" + "; ".join(payload["source_notes"][:5]))
        if payload.get("output"):
            lines.append(f"json={payload['output']}")
        # Discover output is a LONGLIST, not a verified pick — say so on the
        # stream the LLM actually reads (next_command in JSON is not enough).
        lines.append(
            "next: discover 产物只是 longlist —— 跑 --longlist <json> --fetch-web "
            "过 buyable/solderability 验证后才是选品结果"
        )
        return lines

    return _render_evaluation_summary(payload)


# ---------------------------------------------------------------------------
# Hard-coded human-readable summary renderer
# ---------------------------------------------------------------------------
# Output contract:
#   stdout `--summary` = user-ready text, hard-coded per-lane rendering. The
#                        consuming LLM should be able to copy this verbatim
#                        to the user without re-formatting JSON.
#   `--output <path>`  = stable machine contract. Downstream skills (bom-readiness,
#                        component-preparing) MUST read JSON, never grep
#                        stdout — formatting is allowed to evolve, JSON keys
#                        are not.

_VERDICT_GLYPH = {
    "pass": "✓",
    "warn_single_source": "⚠",
    "warn": "⚠",
    "pending_web_data": "…",
    "pending_user_input": "…",
    "fail": "✗",
}

_LIBRARY_GLYPH = {
    # ✅ = library 能稳定拿到（已在 lib / KiCad std / 同源 verify-only 跑通）
    # ❌ = 全部来源都没有
    # 只列 library_probe 实际会返回的 status；旧别名（kicad_standard /
    # kicad_partial / digikey_eda_models）已删，避免 stdout ✅ 跟 GOOD set fail
    # 不一致的隐 bug。
    "vendored_complete": "✅",
    "passive_generic": "✅",
    "standard_ready": "✅",
    "external_cache_exact": "✅",
    "external_cache_compatible": "❌",  # 同 package 占位 ≠ library；pinout 不属此 MPN
    "lcsc_vendorable": "✅",            # easyeda2kicad dry-run 跑通；preparing 走同一调用
    "missing": "❌",
    "unavailable": "❌",
    "fail": "❌",
}


def _fmt_price(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    if abs(value) >= 100:
        return f"¥{value:,.0f}"
    return f"¥{value:,.2f}"


def _fmt_int(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{int(value):,}"
    return "—"


def _compact_int(n: Any) -> str:
    if not isinstance(n, (int, float)):
        return "—"
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    if n >= 1_000:
        return f"{n / 1000:.1f}k"
    return f"{n:,}"


# Renderer fallback = today's JP literals. Locale yaml `display` blocks
# override per key; any yaml failure falls back here so the JP summary can
# never regress to a blank table.
_JP_DISPLAY_DEFAULTS: dict[str, Any] = {
    "lane_order": ["digikey_jp", "mouser_jp", "lcsc"],
    "lane_labels": {
        "digikey_jp": "🇯🇵 DK_JP (¥/在库)",
        "mouser_jp": "🇯🇵 Mouser_JP",
        "lcsc": "🇨🇳 LCSC (CNY ≈ JPY / 在库)",
    },
    "emit_lane_flags": "jp",
    "lcsc_only_note": "⚠ {mpn}：本土仓未收录，仅 LCSC 有现货 — 走 JLCPCB 拼单（DHL ~5d 到日本）",
    "footer_note": "注：购买 URL / datasheet / fail 候选完整数据 → JSON。LCSC lane 走 JLCPCB 拼单（PCB+元件同 DHL）。",
}


def _display_config(locale_label: str | None) -> dict[str, Any]:
    cfg = dict(_JP_DISPLAY_DEFAULTS)
    try:
        mapping = load_yaml(LOCALE_MAPPING)
        _, block = resolve_locale(locale_label, mapping)
        for key, val in (block.get("display") or {}).items():
            cfg[key] = val
    except Exception:
        pass
    return cfg


def _lane_cell(v: dict[str, Any] | None) -> str:
    """One markdown-table cell for a single lane. Hide all non-active states
    (no_match / fetch_error / skipped / pending) — user doesn't care which
    lanes failed, only what succeeded."""
    if v is None:
        return "—"
    status = v.get("status", "")
    if status not in ("active", "nrnd"):
        return "—"
    price = v.get("price")
    stock = v.get("stock")
    nrnd_marker = " ⚠NRND" if status == "nrnd" else ""
    if v.get("vendor_id") == "lcsc" and isinstance(v.get("price_jpy_estimated"), (int, float)):
        jpy_est = v["price_jpy_estimated"]
        fx_warn = " ⚠fx" if v.get("fx_source") == "fallback" else ""
        # < ¥10 JPY → 1 decimal so user can compare e.g. ¥0.85 vs ¥4.9 vs ¥12.
        jpy_text = f"{jpy_est:.1f}" if jpy_est < 10 else f"{jpy_est:.0f}"
        return (
            f"¥{price:.2f} CNY (≈¥{jpy_text} JPY, {_compact_int(stock)})"
            f"{nrnd_marker}{fx_warn}"
        )
    if v.get("vendor_id") == "lcsc" and v.get("currency") == "CNY" and isinstance(price, (int, float)):
        # fx_display: none locales — native CNY with adaptive decimals (generic
        # ¥{:,.0f} would round ¥0.04 to ¥0; passives price in fractions of a fen).
        cny_text = f"{price:.2f}" if price >= 0.1 else f"{price:.4f}"
        return f"¥{cny_text} CNY ({_compact_int(stock)}){nrnd_marker}"
    p = f"¥{price:,.0f}" if isinstance(price, (int, float)) else "—"
    if isinstance(stock, (int, float)) and int(stock) == 0:
        return f"{p} (缺货){nrnd_marker}"
    return f"{p} ({_compact_int(stock)}){nrnd_marker}"


def _candidate_manufacturer(item: dict[str, Any]) -> str:
    for v in item.get("vendor_results", []) or []:
        m = v.get("manufacturer")
        if m:
            return str(m)
    return ""


def _candidate_package(item: dict[str, Any]) -> str:
    kp = item.get("key_parameters", {}) or {}
    pkg = kp.get("package")
    if pkg:
        return str(pkg)
    inp = item.get("input", {}) or {}
    return str(inp.get("package_hint") or inp.get("package") or "")


def _spec_pairs(item: dict[str, Any]) -> list[str]:
    """Render key_parameters as ordered key=value pairs, dropping noise that's
    already shown in the header line (package / mounting) and internal flags
    (any key starting with `_`)."""
    kp = item.get("key_parameters", {}) or {}
    skip = {"package", "mounting"}
    bits: list[str] = []
    for k, val in kp.items():
        if k in skip or not val or str(k).startswith("_"):
            continue
        bits.append(f"{k}={val}")
    return bits


def _render_evaluation_summary(payload: dict[str, Any]) -> list[str]:
    """Render a markdown-table summary of successful candidates.

    Display rules (driven by user feedback 2026-05-07):
      - Hide candidates with verdict=fail entirely. The user only wants to see
        actionable buy options. (Failed candidates remain in JSON for audit.)
      - Hide URLs (purchase + datasheet) from stdout — they clutter the table
        view. URLs stay in JSON output and the user can ask explicitly.
      - One row per candidate. Per-lane status shown as a single cell with
        price + stock; missing lanes render as "—" (no_match / fetch_error
        suppressed).
    """
    locale = payload.get("locale", "?")
    currency = payload.get("currency", "?")
    lines: list[str] = [
        f"=== Component Selecting / locale={locale} currency={currency} ===",
    ]

    # FX footer — same rate across all LCSC candidates in this run.
    fx_rate: float | None = None
    fx_source: str | None = None
    for item in payload.get("results", []) or []:
        for v in item.get("vendor_results", []) or []:
            if v.get("vendor_id") == "lcsc" and v.get("fx_rate"):
                fx_rate = v["fx_rate"]
                fx_source = v.get("fx_source")
                break
        if fx_rate:
            break
    if fx_rate:
        flag = " ⚠ fallback" if fx_source == "fallback" else ""
        lines.append(f"fx: {fx_rate:.3f} JPY/CNY ({fx_source}{flag})")
    lines.append("")

    # Filter out fail candidates — user wants only actionable buy options.
    visible = [
        it for it in (payload.get("results", []) or [])
        if str(it.get("verdict") or "") != "fail"
    ][:12]

    display_cfg = _display_config(payload.get("locale"))
    lane_order = [str(x) for x in (display_cfg.get("lane_order") or _JP_DISPLAY_DEFAULTS["lane_order"])]
    lane_labels = display_cfg.get("lane_labels") or {}

    if not visible:
        lines.append("✗ 没有任何候选通过 verdict —— 请回 longlist 重 spec 或考虑替代型号。")
        lines.append("（fail 候选完整数据见 JSON。）")
    else:
        # Markdown table — lane columns driven by locale display config
        header_cells = [str(lane_labels.get(l, l)) for l in lane_order]
        lines.append(
            "| # | MPN | 厂家 · spec · 封装 | " + " | ".join(header_cells) + " | lib |"
        )
        lines.append("|---|---|---|" + "---|" * len(lane_order) + ":-:|")
        for idx, item in enumerate(visible, 1):
            verdict = str(item.get("verdict") or "")
            warn = " ⚠" if verdict == "warn_single_source" else ""
            mpn = (item.get("mpn") or "?") + warn

            manufacturer = _candidate_manufacturer(item)
            package = _candidate_package(item)
            spec_bits = _spec_pairs(item)
            spec_str = " ".join(spec_bits[:3])  # top-3 most important specs
            desc = " · ".join(b for b in (manufacturer, spec_str, package) if b)

            lane_cells = []
            for lane_id in lane_order:
                v = next(
                    (vr for vr in item.get("vendor_results", []) or []
                     if vr.get("vendor_id") == lane_id),
                    None,
                )
                lane_cells.append(_lane_cell(v))

            lib_status = (item.get("library", {}) or {}).get("status", "")
            lib_glyph = _LIBRARY_GLYPH.get(lib_status, "·")

            lines.append(
                f"| {idx} | {mpn} | {desc} | " + " | ".join(lane_cells) + f" | {lib_glyph} |"
            )

        # Annotate any candidate with lcsc_only_active to clarify lane choice.
        if str(display_cfg.get("emit_lane_flags", "jp")) == "jp":
            lcsc_only = [it.get("mpn") for it in visible if it.get("lcsc_only_active")]
            if lcsc_only:
                lines.append("")
                note_tmpl = str(display_cfg.get("lcsc_only_note") or _JP_DISPLAY_DEFAULTS["lcsc_only_note"])
                for mpn in lcsc_only:
                    lines.append(note_tmpl.format(mpn=mpn))

        # Derating annotations (advisory). Only surface non-ok states so the
        # table stays clean. `warn` = operating point exceeds the generic
        # derating threshold; `unverified` = couldn't obtain Abs-Max or no
        # operating conditions supplied.
        derate_lines: list[str] = []
        for it in visible:
            d = it.get("derating") or {}
            st = d.get("status")
            if st in ("warn", "unverified") and d.get("warnings"):
                for w in d["warnings"]:
                    glyph = "⚠" if st == "warn" else "ℹ"
                    derate_lines.append(f"{glyph} {it.get('mpn')}: {w}")
        if derate_lines:
            lines.append("")
            lines.append("降额 (derating)：")
            lines.extend("  " + dl for dl in derate_lines)

    lines.append("")
    if display_cfg.get("lifecycle_note"):
        lines.append(str(display_cfg["lifecycle_note"]))
    lines.append(str(display_cfg.get("footer_note") or _JP_DISPLAY_DEFAULTS["footer_note"]))
    if payload.get("output"):
        lines.append(f"json: {payload['output']}")
    lines.append(
        "llm_review: only block / rerun if top pick contradicts project constraints; "
        "do not override hard gates."
    )
    return lines


def run_evaluation(args: argparse.Namespace) -> int:
    mapping = load_yaml(LOCALE_MAPPING)
    locale_label = args.locale or read_user_locale(USER_MD)
    locale_name, locale_block = resolve_locale(locale_label, mapping)
    _warn_caller_locale_mismatch(args, locale_name)
    if locale_name == "unknown" and not args.allow_unknown_locale:
        payload = {
            "status": "pending_user_input",
            "reason": "USER.md §0 locale missing or unsupported",
            "evaluated_at": now_iso(),
        }
        write_payload(payload, args)
        return 3

    input_data: Any = None
    if args.mpn:
        candidates = [{"mpn": args.mpn}]
    else:
        input_data = json.loads(Path(args.longlist).read_text(encoding="utf-8"))
        candidates = extract_candidates(input_data)

    # ── Phase 0: longlist size enforcement ────────────────────────────────
    # 软上限 10：覆盖 90% commodity 件选择已足够；超过让 LLM 复审是否真需要广覆盖。
    # 硬上限 25：再多 discovery 已经堆重复/低质量结果，且会撑爆 daily quota。
    longlist_warning: str | None = None
    if len(candidates) > LONGLIST_HARD_LIMIT:
        payload = {
            "schema": f"{args.caller_skill}/v1",
            "status": "fail_too_many_candidates",
            "reason": (
                f"longlist 有 {len(candidates)} 个候选，超过硬上限 {LONGLIST_HARD_LIMIT}。"
                f"Discovery 阶段返回过多候选通常意味着 LLM 没有按 spec 严格 filter——"
                f"请回 LLM 复审 longlist，挑出最相关的 ≤{LONGLIST_HARD_LIMIT} 个再喂给 V2。"
            ),
            "candidates_count": len(candidates),
            "hard_limit": LONGLIST_HARD_LIMIT,
            "evaluated_at": now_iso(),
        }
        write_payload(payload, args)
        return 3
    if len(candidates) > LONGLIST_SOFT_LIMIT:
        longlist_warning = (
            f"longlist {len(candidates)} 个候选超过软上限 {LONGLIST_SOFT_LIMIT}——"
            f"会用更多 daily API quota，但仍在 {LONGLIST_HARD_LIMIT} 硬上限内允许执行"
        )
        print(f"⚠ {longlist_warning}", file=sys.stderr)

    project_path = Path(args.project_path) if args.project_path else None

    effective_role, role_note = resolve_effective_role(args, input_data, candidates)
    if role_note:
        print(f"ℹ role resolution: {role_note}", file=sys.stderr)

    # ── Phase 1: LIBRARY PROBE (offline, fast) ────────────────────────────
    # library_probe 走本地索引（lib_external + lib_cache + KiCad 标准库）。
    # library 状态只影响最终 verdict；vendor API 仍照常查，保证价格/库存可见。
    #
    # 例外：generic-passive role（capacitor / resistor / ferrite_bead /
    # inductor_smd / inductor_th）整段跳过——KiCad std footprint 已覆盖所有
    # 标准尺寸，per-MPN library 验证无意义；候选直接走 vendor API。
    role_lower = effective_role
    is_passive_generic = role_lower in PASSIVE_GENERIC_ROLES

    def _probe_one(cand: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        lib = probe_library(
            cand, locale_block,
            include_network_probes=not args.no_library_network,
            include_eda_models_probe=args.probe_eda_models,
        )
        return cand, lib, library_gate(lib)

    survivors: list[dict[str, Any]] = []

    if is_passive_generic:
        # Synthetic pass — 不扫盘、不打 DK /media，所有候选直接进 Phase 2
        for cand in candidates:
            synth_lib = {
                "status": "passive_generic",
                "source": "role_whitelist",
                "reason": (
                    f"role={role_lower} is generic-passive; KiCad std footprint "
                    f"covers all standard sizes (0402/0603/0805/1206/...)"
                ),
            }
            cand["_phase1_library"] = synth_lib
            cand["_phase1_library_gate"] = {
                "status": "pass",
                "reason": "passive_generic_skip",
            }
            survivors.append(cand)
        print(
            f"ℹ Phase 1 library_probe: 跳过（role={role_lower} 是通用被动件，"
            f"{len(candidates)} 个候选自动放行）",
            file=sys.stderr,
        )
    else:
        # Phase 1 reports library state but does NOT gate the vendor API call.
        # Rationale: in early-stage selection the user often needs to see "library
        # not yet vendored, but what does the price look like?" — quota saving
        # at the cost of price visibility is the wrong trade. Library state still
        # contributes to the final verdict via library_gate, but vendor signals
        # always reach the user. (Quota math: longlist hard-cap 25 × 3 lanes =
        # 75 calls / run; DK/Mouser daily quota 1000 each — comfortable margin.)
        library_phase = parallel_map(_probe_one, candidates, workers=args.parallel, label="library_probe")
        for cand, lib, lg in library_phase:
            cand["_phase1_library"] = lib  # cached for evaluate_candidate to reuse
            cand["_phase1_library_gate"] = lg
            survivors.append(cand)
        n_lib_pass = sum(1 for c in survivors
                         if (c.get("_phase1_library_gate") or {}).get("status") != "fail")
        n_lib_fail = len(survivors) - n_lib_pass
        if n_lib_fail:
            print(
                f"ℹ Phase 1 library_probe: {n_lib_pass}/{len(candidates)} library 命中；"
                f"{n_lib_fail} 个 library 缺失（vendor API 仍照常查，library 状态只影响 verdict）",
                file=sys.stderr,
            )

    # ── Phase 2: VENDOR API (everyone — library no longer gates this) ─────
    def _eval_one(candidate: dict[str, Any]) -> dict[str, Any]:
        return evaluate_candidate(
            candidate,
            locale_name=locale_name,
            locale_block=locale_block,
            project_path=project_path,
            expected_role=effective_role or args.expected_role,
            fetch_web=args.fetch_web,
            short_circuit=not args.no_short_circuit,
            long_term_supply=args.long_term_supply,
            web_driver=args.web_driver,
            library_network_probes=not args.no_library_network,
            only_vendors=_resolve_only_vendors(args),
            include_html_vendors=args.include_html_vendors,
            use_vendor_cache=not args.no_vendor_cache,
            probe_eda_models=args.probe_eda_models,
        )

    results = parallel_map(_eval_one, survivors, workers=args.parallel, label="candidates")
    add_price_tags(results)
    rank_results(results)
    payload = {
        "schema": f"{args.caller_skill}/v1",
        "status": "complete",
        "locale": locale_name,
        "currency": locale_block.get("currency", "USD"),
        "fetch_web": args.fetch_web,
        "web_driver": args.web_driver,
        "include_html_vendors": args.include_html_vendors,
        "vendor_cache": not args.no_vendor_cache,
        "probe_eda_models": args.probe_eda_models,
        "short_circuit": not args.no_short_circuit,
        "evaluated_at": now_iso(),
        "results": results,
    }
    write_payload(payload, args)
    verdicts = {r.get("verdict") for r in results}
    if verdicts & {"pass", "warn_single_source"}:
        return 0
    if verdicts & {"pending_web_data", "pending_user_input"}:
        return 3
    return 2


def run_discover(args: argparse.Namespace) -> int:
    mapping = load_yaml(LOCALE_MAPPING)
    locale_label = args.locale or read_user_locale(USER_MD)
    locale_name, locale_block = resolve_locale(locale_label, mapping)
    _warn_caller_locale_mismatch(args, locale_name)
    if locale_name == "unknown" and not args.allow_unknown_locale:
        payload = {
            "schema": f"{args.caller_skill}/discover-v1",
            "status": "pending_user_input",
            "reason": "USER.md §0 locale missing or unsupported",
            "evaluated_at": now_iso(),
        }
        write_payload(payload, args)
        return 3

    role = args.role or args.expected_role
    profile = role_profile(role)
    keywords = list(args.keywords or [])
    if not keywords:
        keywords = list(profile.get("keywords", []))
    if args.query:
        keywords.append(args.query)

    # `all` resolves to the locale's discover_sources so key-free locales
    # never even attempt DigiKey; explicit --discover-source overrides.
    if args.discover_source == "all":
        sources = {
            str(s) for s in (locale_block.get("discover_sources") or ["local", "digikey", "lcsc"])
        }
    else:
        sources = {args.discover_source.replace("-", "_")}

    # Keywords gate only keyword-consuming sources; parametric / shard lanes
    # run off --param / role mapping and need none.
    keyword_sources = sources & {"local", "digikey", "lcsc"}
    if not keywords and keyword_sources == sources:
        payload = {
            "schema": f"{args.caller_skill}/discover-v1",
            "status": "pending_user_input",
            "reason": "discover requires --keywords, --query, or a known --role profile",
            "evaluated_at": now_iso(),
        }
        write_payload(payload, args)
        return 3
    if not keywords:
        sources = sources - keyword_sources

    notes: list[str] = []
    raw_rows: list[dict[str, Any]] = []
    if "local" in sources:
        rows, source_notes = discover_local_history(role, keywords)
        raw_rows.extend(rows)
        notes.extend(source_notes)
    if "digikey" in sources:
        rows, source_notes = discover_digikey_api(
            keywords=keywords,
            role=role,
            locale_block=locale_block,
            limit_per_keyword=args.discover_limit_per_keyword,
        )
        raw_rows.extend(rows)
        notes.extend(source_notes)
    if "lcsc" in sources:
        rows, source_notes = discover_lcsc_api(keywords, role, args.discover_limit_per_keyword)
        raw_rows.extend(rows)
        notes.extend(source_notes)
    if "lcsc_parametric" in sources:
        rows, source_notes = discover_lcsc_parametric(role, args.param, args.discover_limit)
        raw_rows.extend(rows)
        notes.extend(source_notes)
    if "jlcparts" in sources:
        # Offline shard lane for categories jlcsearch lacks. Lazy import +
        # blanket degrade: this lane must never break a discover run.
        try:
            import jlcparts_shard

            rows, source_notes = jlcparts_shard.discover_rows(
                role=role,
                query=args.query,
                limit=args.discover_limit,
            )
        except Exception as exc:
            rows, source_notes = [], [f"jlcparts_failed:{type(exc).__name__}"]
        raw_rows.extend(rows)
        notes.extend(source_notes)

    deduped = dedupe_candidates(raw_rows)
    filtered: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in deduped:
        ok, reason = candidate_passes_discover_filters(row, args)
        if ok:
            filtered.append(row)
        else:
            rejected.append({"mpn": row.get("mpn"), "reason": reason, "source": row.get("source")})

    drop_library_fail = not args.keep_library_fail
    if args.library_check:
        def _probe_one(row: dict[str, Any]) -> dict[str, Any]:
            lib = probe_library(
                row,
                locale_block,
                include_network_probes=not args.no_library_network,
                include_eda_models_probe=args.probe_eda_models,
            )
            return {"row": row, "lib": lib}

        probe_results = parallel_map(
            _probe_one, filtered, workers=args.parallel, label="library_probes"
        )
        for entry in probe_results:
            row = entry["row"]
            lib = entry["lib"]
            row["library_status"] = lib.get("status")
            row["package_consistency"] = (lib.get("package_consistency") or {}).get("status", "unknown")
            if drop_library_fail and library_gate(lib).get("status") == "fail":
                row["_drop_by_library"] = True
        if drop_library_fail:
            rejected.extend(
                {
                    "mpn": row.get("mpn"),
                    "reason": f"library_{row.get('library_status')}",
                    "source": row.get("source"),
                }
                for row in filtered
                if row.get("_drop_by_library")
            )
            filtered = [row for row in filtered if not row.get("_drop_by_library")]

    prefer_th = args.prefer_through_hole or bool(profile.get("prefer_through_hole"))
    filtered.sort(
        key=lambda row: (
            through_hole_score(row) if prefer_th else 0,
            row.get("price") if isinstance(row.get("price"), (int, float)) else 10**12,
            -(row.get("stock") if isinstance(row.get("stock"), (int, float)) else 0),
            str(row.get("mpn", "")),
        )
    )
    filtered = filtered[: args.discover_limit]
    for idx, row in enumerate(filtered, start=1):
        row["discover_rank"] = idx
        row.pop("_drop_by_library", None)

    payload = {
        "schema": f"{args.caller_skill}/discover-v1",
        "status": "complete" if filtered else "no_candidates",
        "role": role,
        "locale": locale_name,
        "currency": locale_block.get("currency", "USD"),
        "keywords": keywords,
        "filters": {
            "vin": args.vin,
            "vout": args.vout,
            "vin_pattern": args.vin_pattern,
            "vout_pattern": args.vout_pattern,
            "min_iso_v": args.min_iso_v,
            "min_power_w": args.min_power_w,
            "min_stock": args.min_stock,
            "prefer_through_hole": prefer_th,
            "library_check": args.library_check,
            "drop_library_fail": drop_library_fail,
            "library_network_probes": not args.no_library_network,
            "probe_eda_models": args.probe_eda_models,
        },
        "source_notes": notes,
        "rejected_count": len(rejected),
        "rejected_sample": rejected[:20],
        "longlist": filtered,
        "count": len(filtered),
        "evaluated_at": now_iso(),
        "next_command": (
            f"python3 .claude/skills/{args.caller_skill}/scripts/"
            "component_select.py --longlist <this_json> "
            "--project-path Projects/<name> --expected-role <role> --fetch-web --summary"
        ),
    }
    write_payload(payload, args)
    return 0 if filtered else 2


def write_payload(payload: dict[str, Any], args: argparse.Namespace) -> None:
    output = getattr(args, "output", None)
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload["output"] = str(path)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if getattr(args, "summary", False):
        print("\n".join(summary_lines(payload)))
    elif not output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def run_classify_html(args: argparse.Namespace) -> int:
    html = Path(args.html_file).read_text(encoding="utf-8", errors="ignore")
    result = classify_html(
        vendor_id=args.vendor,
        mpn=args.mpn,
        html=html,
        http_status=args.http_status,
        url=args.url,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") in {"active", "nrnd", "no_match"} else 2


def run_self_check() -> int:
    failures: list[str] = []
    warnings: list[str] = []
    mapping: dict[str, Any] = {}
    if not LOCALE_MAPPING.exists():
        failures.append(f"missing {LOCALE_MAPPING}")
    try:
        mapping = load_yaml(LOCALE_MAPPING)
        if not mapping.get("locales"):
            failures.append("locale_mapping has no locales")
    except Exception as exc:
        failures.append(f"locale_mapping parse failed: {exc}")
    if import_library_probe() is None:
        failures.append("library_probe import failed")
    pw_current = "available" if fetch_html_playwright_current("about:blank").get("ok") else "unavailable"
    if pw_current == "unavailable" and not VENV_PYTHON.exists():
        warnings.append("playwright unavailable; library download workflow (UL session) will not work")

    # API adapter wiring: digikey_jp/mouser_jp/lcsc must be in API_VENDOR_KIND
    for required in ("digikey_jp", "mouser_jp", "lcsc"):
        if required not in API_VENDOR_KIND:
            failures.append(f"{required} not in API_VENDOR_KIND")
    # Credentials presence (warn-only — self-check stays passable in CI)
    dk_ready = bool(os.environ.get("DIGIKEY_CLIENT_ID") and os.environ.get("DIGIKEY_CLIENT_SECRET"))
    mouser_ready = bool(os.environ.get("MOUSER_SEARCH_API_KEY"))
    if not dk_ready:
        warnings.append("DIGIKEY_CLIENT_ID/SECRET not set; DigiKey API will return fetch_error")
    if not mouser_ready:
        warnings.append("MOUSER_SEARCH_API_KEY not set; Mouser API will return fetch_error")

    # classify_html still used by --classify-html offline tool; smoke-test it.
    active_html = "LM1117T-3.3/NOPB Product Status Active In Stock 743 JPY ¥120"
    active = classify_html(vendor_id="digikey_jp", mpn="LM1117T-3.3/NOPB", html=active_html)
    if active.get("status") != "active":
        failures.append(f"active classifier failed: {active}")
    nrnd_html = "ABC123 Not For New Designs In Stock 1 $3.20"
    nrnd = classify_html(vendor_id="digikey_us", mpn="ABC123", html=nrnd_html)
    if nrnd.get("status") != "nrnd":
        failures.append(f"nrnd classifier failed: {nrnd}")
    no_match_html = "No results found for your search"
    no_match = classify_html(vendor_id="mouser_us", mpn="NOPE123", html=no_match_html)
    if no_match.get("status") != "no_match":
        failures.append(f"no_match classifier failed: {no_match}")

    # Role resolver regression checks: refdes input should not defeat
    # generic-passive short-circuit, but explicit "component" remains a force-probe.
    role, note = resolve_effective_role(
        argparse.Namespace(expected_role="R16B", role=None),
        {"role": "capacitor"},
        [{"mpn": "C0805C104K5RACTU"}],
    )
    if role != "capacitor" or not note:
        failures.append(f"refdes role resolver failed: role={role!r}, note={note!r}")
    role, note = resolve_effective_role(
        argparse.Namespace(expected_role="component", role=None),
        {"role": "capacitor"},
        [{"mpn": "C0805C104K5RACTU"}],
    )
    if role != "component" or note:
        failures.append(f"force-probe role resolver failed: role={role!r}, note={note!r}")

    if mapping.get("locales"):
        _, jp_block = resolve_locale("日本", mapping)
        default_vendors = [v["vendor_id"] for v in build_vendor_urls("C0805C104K5RACTU", jp_block)]
        if default_vendors != ["digikey_jp", "mouser_jp", "lcsc"]:
            failures.append(f"default JP vendors not API-only core lanes: {default_vendors}")
        # JP yaml keys must reproduce the engine defaults — catches yaml drift
        # that would silently change JP behavior.
        jp_gate = jp_block.get("gate_policy") or {}
        if int(jp_gate.get("min_local_sources", 2)) != 2:
            failures.append(f"JP gate_policy.min_local_sources drifted: {jp_gate}")
        jp_display = jp_block.get("display") or {}
        if [str(x) for x in jp_display.get("lane_order") or []] not in ([], ["digikey_jp", "mouser_jp", "lcsc"]):
            failures.append(f"JP display.lane_order drifted: {jp_display.get('lane_order')}")
        if str(jp_block.get("fx_display", "cny_jpy")) != "cny_jpy":
            failures.append(f"JP fx_display drifted: {jp_block.get('fx_display')}")
        # CN locale (component-selecting-CN thin shell) wiring
        cn_name, cn_block = resolve_locale("中国大陆", mapping)
        if cn_name != "中国大陆":
            failures.append(f"CN locale unresolvable: {cn_name}")
        else:
            cn_vendors = [v["vendor_id"] for v in build_vendor_urls("0402WGF1001TCE", cn_block)]
            if cn_vendors != ["lcsc"]:
                failures.append(f"default CN vendors not [lcsc]: {cn_vendors}")
            cn_gate = cn_block.get("gate_policy") or {}
            if int(cn_gate.get("min_local_sources", 2)) != 1:
                failures.append(f"CN gate_policy.min_local_sources != 1: {cn_gate}")
            if str(cn_gate.get("lifecycle_policy", "api")) != "unverified":
                failures.append(f"CN lifecycle_policy != unverified: {cn_gate}")
            if str(cn_block.get("fx_display", "cny_jpy")) != "none":
                failures.append(f"CN fx_display != none: {cn_block.get('fx_display')}")

    if failures:
        for failure in failures:
            print(f"FAIL {failure}")
        return 2
    for warn in warnings:
        print(f"WARN {warn}")
    print(
        f"PASS component-selecting-JP self-check "
        f"(api_kinds={sorted(API_VENDOR_KIND.values())}, digikey_creds={dk_ready}, "
        f"mouser_creds={mouser_ready}, playwright={pw_current})"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Component Selecting Version 2 deterministic pipeline")
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--mpn")
    src.add_argument("--longlist")
    src.add_argument("--discover", action="store_true")
    parser.add_argument("--project-path")
    parser.add_argument("--expected-role")
    parser.add_argument("--role", help="Discovery/evaluation role profile, e.g. isolated_dcdc, ldo, iso_amp")
    parser.add_argument("--locale")
    parser.add_argument(
        "--caller-skill",
        default="component-selecting-JP",
        help="skill identity for schema strings / next_command (thin locale "
        "shells like component-selecting-CN pass their own name)",
    )
    parser.add_argument("--allow-unknown-locale", action="store_true")
    parser.add_argument("--fetch-web", action="store_true")
    parser.add_argument("--web-driver", choices=["auto", "playwright", "urllib"], default="auto")
    parser.add_argument(
        "--include-html-vendors",
        action="store_true",
        help="Opt into slow non-API vendor scraping (Akizuki/Marutsu/etc.) via Firecrawl.",
    )
    parser.add_argument("--no-short-circuit", action="store_true")
    parser.add_argument("--long-term-supply", action="store_true")
    parser.add_argument(
        "--parallel",
        type=int,
        default=DEFAULT_PARALLEL,
        help=f"Concurrent workers for API + library probes (default {DEFAULT_PARALLEL}, max {MAX_PARALLEL}).",
    )
    parser.add_argument(
        "--single-vendor",
        default=None,
        help=(
            "Restrict buyable_gate to one vendor (alias: digikey, mouser, or a "
            "literal vendor_id like digikey_jp). Halves API call count and dodges "
            "Mouser free-tier 30/min rate limit; verdict caps at warn_single_source."
        ),
    )
    parser.add_argument("--output")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument(
        "--no-vendor-cache",
        action="store_true",
        help="Disable local DK/Mouser/LCSC result cache.",
    )
    parser.add_argument(
        "--probe-eda-models",
        action="store_true",
        help="Also query DigiKey /media for EDA model metadata (off by default).",
    )

    parser.add_argument("--query", help="Extra free-text discovery query")
    parser.add_argument("--keywords", action="append", default=[], help="Discovery keyword; repeatable")
    parser.add_argument(
        "--discover-source",
        choices=["all", "local", "digikey", "lcsc", "lcsc-parametric", "jlcparts"],
        default="all",
    )
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="parametric filter for --discover-source lcsc-parametric, "
        "normalized SI values (e.g. --param resistance=1000 --param package=0402); repeatable",
    )
    parser.add_argument("--discover-limit-per-keyword", type=int, default=30)
    parser.add_argument("--discover-limit", type=int, default=12)
    parser.add_argument("--vin")
    parser.add_argument("--vout")
    parser.add_argument("--vin-pattern", default="")
    parser.add_argument("--vout-pattern", default="")
    parser.add_argument("--min-iso-v", type=float, default=0)
    parser.add_argument("--min-power-w", type=float, default=0)
    parser.add_argument("--min-stock", type=int, default=0)
    parser.add_argument("--prefer-through-hole", action="store_true")
    parser.add_argument("--library-check", action="store_true")
    parser.add_argument(
        "--no-library-network",
        action="store_true",
        help="Disable LCSC dry-run + UL session probes; only check local libs",
    )
    # Discover defaults to dropping library-fail rows. Use --keep-library-fail
    # to surface them anyway (e.g. for diagnostics).
    parser.add_argument(
        "--drop-library-fail",
        action="store_true",
        help=argparse.SUPPRESS,  # legacy; default behavior now, kept for compat
    )
    parser.add_argument(
        "--keep-library-fail",
        action="store_true",
        help="Override discover default: include candidates with no obtainable library",
    )

    parser.add_argument("--classify-html", action="store_true")
    parser.add_argument("--vendor")
    parser.add_argument("--html-file")
    parser.add_argument("--http-status", type=int, default=200)
    parser.add_argument("--url")

    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--playwright-fetch-json", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.playwright_fetch_json:
        result = fetch_html_playwright_current(args.playwright_fetch_json)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 2
    if args.self_check:
        return run_self_check()
    if args.discover:
        return run_discover(args)
    if args.classify_html:
        if not args.vendor or not args.mpn or not args.html_file:
            parser.error("--classify-html requires --vendor, --mpn, and --html-file")
        return run_classify_html(args)
    if not args.mpn and not args.longlist:
        parser.error("provide --mpn, --longlist, --discover, --classify-html, or --self-check")
    return run_evaluation(args)


if __name__ == "__main__":
    raise SystemExit(main())
