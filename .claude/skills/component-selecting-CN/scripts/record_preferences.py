#!/usr/bin/env python3
"""Persist 4-axis user preferences asked by component-selecting-CN at session start.

Phase 2 LLM 用 AskUserQuestion 问完 4 轴（渠道 / 品牌 / 价格 vs 库存 / 黑名单）后
**立刻**调本脚本把答案落盘到
    Projects/<name>/_artifacts/component_selecting/user_preferences.json

下游 release skill 通过 load_preferences.py 读这份文件，按用户渠道偏好渲染
ORDER_GUIDE。如果 release 阶段读不到，会再次 AskUser 4 轴并调本脚本回写。

文件格式契约与 JP 版一致（schema_version=v1），只有渠道 / 品牌选项集不同。

用法:
    record_preferences.py --project <name> \\
        --channel {cn_domestic_fast|lcsc_jlcpcb|auto_cheapest} \\
        --brand   {domestic_first|international|any} \\
        --price-vs-stock {tight|balanced|stable_first} \\
        [--blacklist MPN1,MPN2,...]

不要写其他字段；schema_version=v1 锁定结构。
"""
from __future__ import annotations
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[4]

CHANNEL_CHOICES = ("cn_domestic_fast", "lcsc_jlcpcb", "auto_cheapest")
BRAND_CHOICES = ("domestic_first", "international", "any")
PRICE_VS_STOCK_CHOICES = ("tight", "balanced", "stable_first")


def prefs_path(project: str) -> Path:
    if not project.strip() or "/" in project or "\\" in project or ".." in project:
        raise SystemExit(f"❌ --project must be a bare directory name, got {project!r}")
    return (WORKSPACE_ROOT / "Projects" / project
            / "_artifacts" / "component_selecting"
            / "user_preferences.json")


def write_preferences(
    project: str, channel: str, brand: str,
    price_vs_stock: str, blacklist_mpns: list[str],
) -> Path:
    if channel not in CHANNEL_CHOICES:
        raise SystemExit(f"❌ channel must be one of {CHANNEL_CHOICES}, got {channel!r}")
    if brand not in BRAND_CHOICES:
        raise SystemExit(f"❌ brand must be one of {BRAND_CHOICES}, got {brand!r}")
    if price_vs_stock not in PRICE_VS_STOCK_CHOICES:
        raise SystemExit(f"❌ price_vs_stock must be one of {PRICE_VS_STOCK_CHOICES}, "
                         f"got {price_vs_stock!r}")

    payload = {
        "schema_version": "v1",
        "asked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "channel": channel,
        "brand": brand,
        "price_vs_stock": price_vs_stock,
        "blacklist_mpns": list(blacklist_mpns),
    }
    out = prefs_path(project)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True,
                    help="Projects/<name> 项目目录名")
    ap.add_argument("--channel", required=True, choices=CHANNEL_CHOICES)
    ap.add_argument("--brand", required=True, choices=BRAND_CHOICES)
    ap.add_argument("--price-vs-stock", required=True, choices=PRICE_VS_STOCK_CHOICES,
                    dest="price_vs_stock")
    ap.add_argument("--blacklist", default="",
                    help="逗号分隔 MPN 列表（可空）")
    args = ap.parse_args()

    blacklist = [m.strip() for m in args.blacklist.split(",") if m.strip()]
    out = write_preferences(
        project=args.project,
        channel=args.channel,
        brand=args.brand,
        price_vs_stock=args.price_vs_stock,
        blacklist_mpns=blacklist,
    )
    print(f"✅ saved 4-axis preferences → {out}")


if __name__ == "__main__":
    main()
