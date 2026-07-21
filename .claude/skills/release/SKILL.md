---
name: release
description: >-
  Release / fab-package aggregator expert (Phase 5). ALWAYS invoke
  this skill after check-pcb gate passes when the user wants to package a
  board for fab/distributors: Gerber/Drill/CPL export, production BOM,
  document PDFs (HDD / CE / Design Review / ICD / Manufacturing), vendor
  routing (JLCPCB / PCBWay / DigiKey / Mouser / LCSC), distributor upload
  CSV, ORDER_GUIDE.md, release_<ts>.zip. Do not assemble fab packages by
  copying files manually, redo upstream checks, recompute the BOM, or
  rewrite document originals. Use this skill first. Gate: check-pcb verdict
  pass + .bom_readiness.json all_pass + .kicad_pcb mtime ≤ sentinel
  verified_at. Triggers: 下单 / 打包 / release / 出货包 / vendor 覆盖率 /
  JLCPCB / PCBWay / ORDER_GUIDE / 导 Gerber / export gerbers / 生产 BOM /
  CPL / fab 输出 / 导制造文件 / manufacturing package / distributor CSV /
  turnkey 还是自买.
---

# release — 出货聚合器

## 哲学

**只聚合 + 转格式 + 打包，不重做上游**。check-pcb gate 通过后用户要下单时入场，把分散在
四五个产物目录里的东西（DRC/EMC JSON、采购 BOM CSV、KiCad 工程）转成 fab 厂 / distributor
能直接吃的格式，再打成一个 zip 给用户。

不做的事（重要）：
- 不重做 check（gate 已 pass，结果直接读 `analysis/<run_id>/*.json`）
- 不重新算 BOM（component-preparing 写的 sentinel + 采购 CSV 直接搬过去）
- 不重画 Gerber（调 `kicad-cli pcb export gerbers/drill`，参数从模板读）
- 不写文档原文（HDD/CE/ICD 等模板 + 上游 JSON 槽位填空，不做技术判断）

> 历史职责合并：原 `kidoc` / `kicad`（fab export 部分）/ `fab` 三个 skill 的脚本与
> references 已搬到本 skill `scripts/` 与 `references/` 下作为模板和数据；本 skill
> 自身不实现新的检查/分析逻辑。

## 不做的事

- ❌ 不重做 check（gate 已 pass）
- ❌ 不重新算 BOM（component-preparing 已写好 sentinel + CSV）
- ❌ 不重画 Gerber（如已存在 release/<ts>/pcb_fab/，可 `--skip-fab-export` 复用）
- ❌ 不画原理图、不审 PCB

## 何时用 / 不用

**触发**：
- check-pcb gate pass 后，用户说"准备下单"、"打包"、"出货"
- 需要 distributor 上传格式 CSV
- 需要决定 JLCPCB vs PCBWay vs DigiKey vs Mouser vs LCSC 哪条 path 最划算
- 需要生成 HDD / CE / Design Review / ICD / Manufacturing 文档 PDF

**不触发**：
- 还在画 PCB（用 `draw-pcb`）
- 还在审 PCB（用 `check-pcb`）
- 单 MPN 临时查库存 / 比价（用 `component-selecting-JP --mpn`）
- 还在选品 / 落资产 / BOM 验收（用 `component-selecting-JP` / `component-preparing`）

## 入口

### Phase 0：4 轴渠道偏好（必读）

调 build_release.py 之前先确认 `Projects/<name>/_artifacts/component_selecting/user_preferences.json`
是否存在。

- **存在** → 直接跑 build_release.py
- **不存在**（老项目 / 没经过新版 component-selecting-JP）→ AskUserQuestion 4 轴
  （渠道 / 品牌 / 价格 vs 库存 / 黑名单），然后调
  `.claude/skills/component-selecting-JP/scripts/record_preferences.py --project <name> ...`
  写盘，再 build_release.py

build_release.py 读不到这份文件会 fail-fast，要求回到上面流程；不要 `--force`
绕过。理由：ORDER_GUIDE 推荐路径**完全由用户渠道偏好驱动**（lcsc_jlcpcb → Path A，
jp_domestic_fast → Path C-1/C-2，auto_cheapest → 落给 coverage_scan 算法），
没有偏好 release 不知道该把哪条 path 标 ★。

### 主入口

```bash
# 全量 release（首次或 BOM 大改）
".venv/bin/python" \
  .claude/skills/release/scripts/build_release.py Projects/<name>

# 原地修订（小改后只重导 fab artifact，不重做 ORDER_GUIDE / coverage / zip）
".venv/bin/python" \
  .claude/skills/release/scripts/build_release.py Projects/<name> --reuse <release_id>
```

**`--reuse` 何时用**：.kicad_pcb 微调（换 1–2 个值 / 修个丝印 / 调一根线）后，
原 release/<ts>/ 框架还有效。`--reuse` 从 `release/<id>/pcb_fab/fab_manifest.json#commands`
照搬原 kicad-cli 命令重跑 → Gerber / drill / pos / CPL 用的标志（含
`--subtract-soldermask` / `--check-zones` 等关键 flag）跟首次完全一致，杜绝手写漏 flag
导致 B_Silkscreen 变空 / Mask 几何错位的隐性 bug。BOM / vendor / 渠道偏好变了 → 走全量 release，不要 reuse。

完整 flag 列表（`--skip-fab-export` / `--dry-run` / `--force`）+ 单工序入口（只导 Gerber / 只生成某种文档 PDF）+ 输出目录结构 + Gate 行为 + BOM 复核协议 + Vendor 路由表 + 文档类型表 → `references/release_internals.md`。

## 红线

- ❌ 不要用 release 做检查 —— 那是 check-schematic / check-pcb 的事
- ❌ 不要在 gate fail 时强制 `--force` 跑 —— gate 是有原因 fail 的
- ❌ 不要手改 `release/<ts>/` 里的文件 —— 重跑 release 是无副作用的；要原地修订请走 `--reuse <release_id>`（见入口节）
- ❌ 不要绕过 `--reuse` 模式手写 kicad-cli 命令重导 Gerber —— manifest 里记录的 `--subtract-soldermask` / `--check-zones` 等标志漏一个会让 Silkscreen / Mask 静默错位
- ❌ **采购 BOM ≠ 生产 BOM/CPL**：采购 BOM (procurement/bom_*.csv) distributor 用；生产 BOM/CPL (pcb_fab/assembly/*) fab 厂贴片用
- ❌ **CPL 必须含 THT**：positions.csv 默认全员（SMD + THT），跟 assembly_bom 行数对齐。**漏 THT = fab 收到半成品**（电容/连接器/SW/iso DC-DC 全没坐标）。SMT-only fab 需要纯 SMD 时再显式加 `--smd-only`，且必须给用户确认 — 不做静默过滤
- ❌ 不要直接给 fab 厂用户工程目录里的源文件，永远走 export_gerbers 出来的 pcb_fab/
- ❌ **fab-bound artifact 必须双轨审查**：release 跑完不能凭 user 一句 "OK" 当 final——必须再走一轮独立审查（独立 sub-agent）。理由：silkscreen 漏检 / Mask 几何错位 / drill report 缺失这类问题，主线 LLM 自查盲区高，物理板印错就是整批板报废

## references

- `references/release_internals.md` — 完整命令 / 输出树 / vendor 决策 / 文档类型 / 子工序协议
- `references/distributor_csv_formats.md` — 各家 BOM 上传列名规范
- `references/jlcpcb.md` — JLCPCB 工艺百科（原 fab skill）
- `references/pcbway.md` — PCBWay 工艺百科（原 fab skill）
- `references/kidoc/` — 原 kidoc skill references（文档结构 / 渲染选项 / 各报告类型模板）
