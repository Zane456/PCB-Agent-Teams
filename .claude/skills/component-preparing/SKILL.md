---
name: component-preparing
description: >-
  Phase 2.5 expert — applicability review + shortlist
  confirmation + asset acquisition + BOM gate. ALWAYS invoke this skill
  after component-selecting outputs shortlist JSON: deep-review each top
  pick datasheet against the circuit role (5-dimension check) before user
  confirms, fetch datasheets, vendor library into lib_external/, write
  per-MPN evidence JSON, run verify_vendoring.py + check_readiness.py to
  emit .bom_readiness.json sentinel + procurement BOM CSV (draw-schematic
  entry gate). Do not download datasheets, write to lib_external/, skip
  the applicability review, or skip the BOM sentinel gate by hand. Use
  this skill first. Does not re-evaluate shortlist verdict
  (component-selecting owns that) and does not draw schematics. Triggers:
  落资产 / 把 shortlist 落下来 / 适用性审查 / applicability review / 抓
  datasheet / vendor library / Phase 2.5 / BOM 验货 / verify BOM /
  library readiness / 生成订货 CSV / pre-sch-render check.
---

# component-preparing — 选品确认 + 资产获取 + BOM gate

## 核心理念

**单一职责**：把 component-selecting 已经 verdict=pass 的 MPN **落到磁盘并出 BOM 验收 sentinel**，下游 draw-schematic 凭 sentinel 入场。

- 不重选、不二评：verdict 来自 component-selecting，本 skill 只**适用性审查 + 确认 + 抓取 + 验收**（⓪ 审查不重跑参数 API，只对 datasheet vs role 做深度核对）
- 不查库存、不算价格：API 已在 component-selecting 阶段查过
- 不动 .py 电路结构 / .kicad_sch：那是 draw-schematic 的事（唯一例外见 ⑥——BOM 全过后自动把已验证 MPN/Datasheet 属性补回 .py `Component()`，纯属性补全、不改结构）

> 历史职责拆分：原 `bom-readiness` skill 已合并入本 skill（2026-05）；
> 原因——shortlist 落资产时 BOM 数据本就齐了，没必要等到画完 sch 再回头校。

## 共享元件库 lib_external/

写入 / 读取 / 命名空间 / 清理 / 冲突解决 → **见 `lib_external/CONVENTIONS.md`**（single source of truth）。

## 入场判断

| 用户进来时的状态 | 进 / 不进 | 处理 |
|---|---|---|
| 拿着 `_artifacts/component_selecting/<role>_shortlist.json` | ✅ 进 | 跑全工作流 ⓪–⑥ |
| **同族换值**（MPN 内某段变化、supplier+series+pkg 不变）| ✅ 进（family-swap 模式）| 跑 `scripts/swap_family_mpn.py --old-mpn X --new-mpn Y --apply`（自动跨文件改 .py / .kicad_sch / .kicad_pcb / .net / lcsc_mapping，stamp shortlist `_v5_revision`，clean orphan PDF，重跑 sentinel）|
| 单 MPN 跨族 swap（不同系列 / pkg / 供应商）| ✅ 进（单件模式）| 单点抓 datasheet → 必跑 clean_orphan_datasheets.py --apply → 后续步骤 |
| 准备 draw-schematic 但 sentinel 不存在 / stale | ✅ 进（gate 模式）| 跳过抓取，直接跑 ⑥ check_readiness.py 重写 sentinel |
| 拓扑 / 锚点件还没定 | ❌ 退出 → `circuit-design` | — |
| shortlist 还没出来 | ❌ 退出 → `component-selecting-JP` | — |
| 单 MPN 临时查库存 / 比价 | ❌ 退出 → `component-selecting-JP --mpn` | — |

## 入场必读

- `Projects/<name>/CLAUDE.md §5` — 锚点件功能 spec，是 ⓪ 适用性审查的 ground truth
- `USER.md §0` — locale 决定抓取链路（日本 → DK dkjp + UL/SnapEDA fallback；中国 → LCSC，待建）
- `lib_external/CONVENTIONS.md` — vendoring 写入规则
- `references/vendoring_field_notes.md` — Cloudflare / dkjp session / DK DOM 速记

## 工作流概览（7 步）

```
读 shortlist JSON
   ↓
对每个 role，从 top-1 起：
   ⓪ 适用性审查（pre-confirm gate，详见下节）
       ├─ pass / pass_with_concerns → 带报告进下一步
       ├─ fail (swap)               → 取 shortlist[+1]，⓪ 重跑
       └─ fail (回 selecting)       → spec 错位严重，user 自跳 component-selecting
   ↓
跟 user 逐 role 确认 top pick（基于 ⓪ 报告拍板）
   ├─ accept → scripts/accept_shortlist.py 把 shortlist → per-MPN evidence
   │            （写到 datasheets/component_selecting/<safe_mpn>.json；
   │             ⓪ 报告由 LLM 补写到 evidence `applicability` 字段）
   └─ reject → 退回 component-selecting 重出 shortlist
   ↓
对每个确认的 MPN：
   ① datasheet 批量抓取（scripts/bulk_fetch_datasheets.py，原 sourcing 迁入）
       → **强制接** clean_orphan_datasheets.py --apply
       （⓪ swap 留下的孤儿 PDF 也在这里被清掉）
   ② library 分类（按 evidence library.status 7 类）
   ③ vendoring（按 USER.md §0 locale 路由）
   ④ evidence JSON 补齐（datasheet.path / library.vendored_*）
   ⑤ verify_vendoring.py 自检（强制收尾，**不允许跳**）
   ↓
全 MPN 落地后：
   ⑥ BOM gate（写 .bom_readiness.json sentinel + 采购 BOM CSV）
       → scripts/check_readiness.py
   ↓
告知用户："资产 + sentinel + 采购 CSV 已就绪，可进 draw-schematic"
```

⓪ 详见下节；①–⑤ 抓取 / vendoring / locale 路由 → `references/vendoring_field_notes.md`；⑥ BOM gate 全流程 + fidelity 三检 + sentinel 协议 → `references/bom_gate.md`。

## ⓪ 适用性审查（pre-confirm gate）

user 拍 top pick 前，先用 datasheet 把候选跟项目 §5 spec 对齐：per role 从 top-1 抓 datasheet（先不写 evidence）→ LLM 按 5 维度核查（每条带 datasheet 页码 / §5 行号支撑句）→ markdown 报告交 user 三选一（accept / swap_next / back_to_selecting）。**拿不到项目 §5 → 不进 ⓪。**

进 ⓪ 前读 `references/applicability_review.md`：gate 条件 + per-role 流程 + 5 维度细表 + verdict 三档 + evidence `applicability` schema + 风险接受规则 + 反例。

## ⑥ BOM gate（核心硬门槛）

全 MPN 落地后跑 `check_readiness.py`：fidelity 四检（A.MPN 一致性 / B.封装类一致性 / C.占位符伪装 / D.pin 数一致性）任一 fail → `all_pass=False` → draw-schematic 入场被挡。`all_pass` 后默认调 `inject_mpn_props.py` 把已验证 MPN/Datasheet/Manufacturer 补回 .py `Component()`（幂等、只补属性不改结构，`--no-inject-mpn` 关闭）。

跑 ⑥ 前读 `references/bom_gate.md`：四检血泪案 + 注入细节 + sentinel 字段/失效条件 + 审查模式 + evidence contract + 命令行用法。

## 反模式（最高频 3 条；全集 → `references/anti-patterns.md`）

- ❌ **重评 shortlist verdict / 跳过 user 确认**：verdict 来自 component-selecting，top pick 必须 user 拍板
- ❌ **跳过 ⓪ 适用性审查直接抓全 BOM**：spec 错位的料 vendoring 完才发现 → 浪费 quota + 误导 user 拍板；⓪ 报告必须先到 user 桌上
- ❌ **同族换值用 bare sed / Edit cross-file**：必须走 `swap_family_mpn.py`，否则漏改连带字段 → sentinel 静默放行 → 物理板按错值贴片

> 其余（支撑句要求 / 手拷 lib / 近似 footprint / 跳 verify / role 裸编号 等）展开在 `references/anti-patterns.md`，动手前对照自检。

## 边界（不做这些）

讨论拓扑 → `circuit-design`；longlist + verdict + buyable_gate → `component-selecting-JP`；
临时查库存 / 比价 → `component-selecting-JP --mpn`；.py → .kicad_sch → `draw-schematic`；
Gerber + 生产 BOM/CPL + 文档 + vendor 决策 + BOM 复核 → `release`（出货 umbrella）。

## references

- `references/applicability_review.md` — ⓪ 5 维度核查清单 + verdict 三档 + evidence `applicability` schema + 风险接受规则
- `references/vendoring_field_notes.md` — ①–⑤ 实战速记（Cloudflare / dkjp / DK DOM）
- `references/bom_gate.md` — ⑥ check_readiness.py 全流程 + fidelity 四检 + sentinel 协议
- `references/bom_lifecycle.md` — 三类 BOM 文件生命周期（采购 vs 生产 vs CPL）
- `references/anti-patterns.md` — 反模式全集（越界 / vendoring / 命名 / family-swap）
