---
name: draw-schematic
description: >-
  KiCad schematic generation pipeline expert (Phase 3). ALWAYS
  invoke this skill when generating or regenerating .kicad_sch from a
  frozen BOM + topology, or rendering schematic PDF (画原理图 / 重新生成 sch /
  生成 PDF / regenerate after BOM change). Do not write .kicad_sch by string
  concatenation, skip the .bom_readiness.json sentinel gate, or hand-add
  PWR_FLAG. Use this skill first. Owns generation only; deep review (sch
  analyzer / SPICE / datasheet cross-ref) belongs to check-schematic.
---

# Draw Schematic — KiCad 原理图生成 pipeline

"BOM + 拓扑 → circuit-synth .py → .kicad_sch + PDF + ERC clean" 自动化流水线。
**只生成**；深度审图归 `check-schematic`。

## 前置依赖：component-preparing 的 BOM sentinel

`pipeline.py` Phase 0 检 `<project>/datasheets/.bom_readiness.json`（由 component-preparing 写）：
- 不存在 / mtime 不对 / `all_pass=false` → fail-fast，回 `component-preparing`
- 通过 → 继续

理由：library/stock/datasheet 是选品阶段的事；画到一半发现 footprint 没了等于全返工。

## 何时用 / 不用

| 用 | 不用 |
|---|---|
| BOM + 拓扑已冻结，要生成 sch | BOM 没冻结 → `circuit-design` |
| 改了 BOM/拓扑要重生 | 审图 / SPICE / rule-id triage → `check-schematic` |
| 验证生成的 sch 跟项目 CLAUDE.md 一致 | 选元件 / 查库存 → `component-selecting-JP` |
|  | 画图前 BOM gate → `component-preparing` |
|  | PCB 阶段 → `draw-pcb` / `release`（出货 umbrella） |

## 边界：generation-time correctness vs design review

| 项 | 归属 | 理由 |
|---|---|---|
| L1 ERC `total_errors == 0` | 本 skill | 结构错的 sch 不可用 |
| L2 PDF + 视觉 | 本 skill | 确认生成没崩 |
| L3 拓扑 vs CLAUDE.md | 本 skill | "画的是不是用户要的" |
| L4 footprint 全可用 | 本 skill | PCB 阶段 "Footprint not found" 必须早堵 |
| sch analyzer 深度 / rule_id / findings[] | **check-schematic** | review 不是生成对错 |
| SPICE 子电路仿真 | **check-schematic** | 数值验证 |
| Datasheet pin-level 交叉验证 | **check-schematic** | 设计层 |

## 设计原则：硬编码优先

LLM 不稳定。所有 critical 步骤（pipeline 顺序 / ERC 阈值 / 坐标变换 / Y 轴翻转）写死在脚本，LLM 只调脚本不做决策。90% 任务 = `python pipeline.py <project.py>`。

## 死规定：passive value 填电学量不填 MPN

R/C/L/LED/通用 D/J/SW 的 `Component(value=...)` 必须是电学量或角色名（`1M`、`100nF`、`LED_GREEN`、`HV_INPUT`），**不许**是 distributor SKU。U/复杂 D 允许 MPN。理由：板上 silk 印 `1M` 焊接时一眼能认，印 `TNPW12061M00BEEA` 没人认得。详细表 + Pipeline gate 正则 → `references/passive_value_rule.md`（draw-pcb silk audit 共用）。

## 工作流

### 一键（推荐）

```bash
# 跑前先 cd 到 KICAD 工作区根（或 export KICAD_ROOT=...）
"$KICAD_ROOT/.venv/bin/python" \
  ".claude/skills/draw-schematic/scripts/pipeline.py" \
  /path/to/project.py
```

输出 JSON dict（`ok`、`sch_path`、`pdf_path`、`l1_total_errors`、`lib_id_count` 等）。Claude 读 dict 判成败。

### 阶段总览（分步排查用）

```
0. BOM sentinel check    → 不过 fail-fast
1. 读 CLAUDE.md 提 BOM + 拓扑
2. 元件库对齐（消费 sentinel + lib_external）
3. 写/改 <project>.py（circuit-synth DSL）
4. pipeline.py（generate → fix_labels → add_hier_labels → ERC → PDF → L3）
5. Claude Read PDF 做 L2 视觉 + L3 拓扑
   ↓ 生成成功后
6. 切给 check-schematic（深度 review + SPICE）
```

**默认单图**（一个 `@circuit` 里写所有元件）。`fix_labels.py` 用 kicad-sch-api
精确 pin 坐标重写 label，已能压住上游 bug #2 的 label collision，元件数量不再是
拆图门槛。检查工具（analyzer / detect_rc_filters / SPICE）也都是为单图优化的——
hierarchical 模式下 net 会被分片成 `name` 与 `/uuid/name` 两份，detector 失效。

**仅在以下情况** 才考虑拆 hierarchical（每片 ≤15 元件）：
- 单图 ≥38 元件且实测 net 错合并（fix_labels 仍漏修）
- 业务上需要做 IP 复用 / 子板分文件交付

pipeline 两种模式都支持（自动跑 fix_labels；hierarchical 时自动补 hier_label）。
详细 Bug 历史 → `references/known-bugs.md` #2/#3/#6。

每个 Stage 详细（写 .py 模式、ERC JSON 解析、L1–L4 验证脚本、L4-bis/tris 路径、PWR_FLAG 自动注入）→ `references/pipeline_stages.md`。

环境检查（KiCad CLI / venv / 工具栈版本）→ `references/preflight.md`（pipeline.py 内部已 fail-fast）。

## 脚本清单（`scripts/` 下）

`pipeline.py`（一键）/ `verify_footprints.py`（L4 + fuzzy 自动修）/ `fix_labels.py`（修 pin label + 删 power 符号 + auto-NC 未映射 pin）/ `add_hier_labels.py`（hierarchical 跨表连通修复）/ `add_pwr_flags.py`（自动加 PWR_FLAG）/ `verify_topology.py`（L3 比对）。每个 `--help` 看用法。

## 输出格式

完成后给用户的报告模板（L1/L2/L3 状态行 + 下一步建议 + 禁止报告事项）→ `references/output_template.md`。

## references / examples

- `references/passive_value_rule.md` — value 字段死规定（draw-pcb 共用）
- `references/pipeline_stages.md` — Stage 1–5 + L4-bis/tris 详细
- `references/preflight.md` — KiCad CLI / venv / 工具栈
- `references/output_template.md` — 完成后给用户的报告格式
- `references/known-bugs.md` — circuit-synth + easyeda2kicad bug + workaround

## 触发关键词

中：画原理图 / 生成 sch / 重新生成 / 改 BOM 重画 / 看 PDF / 验证连接
EN: draw schematic, regenerate after BOM change, render PDF, run schematic pipeline

## 红线

- ❌ 只看 ERC 数字不看 PDF 就报完成
- ❌ 工具回 `success: true` 直接相信
- ❌ **只 grep 一种 ERC 错误**（必须 `total_errors == 0`）
- ❌ 自己手动加 PWR_FLAG（pipeline 自动跑）
- ❌ LLM 凭记忆猜 MPN / 不查就改 .py value
- ❌ 字符串拼接 .kicad_sch（用 circuit-synth + ksa）
