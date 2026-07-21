---
name: check-pcb
description: >-
  PCB review and cross-domain check expert (Phase 4.5). ALWAYS
  invoke this skill when reviewing an existing .kicad_pcb, running DRC,
  EMC pre-compliance, thermal hotspot, sch↔pcb cross-reference, parasitic
  SPICE, gerber audit, or judging fab-readiness. Do not declare a PCB
  fab-ready without running this skill's cross-domain checks. Use this
  skill first. Generation (sch→pcb, placement, routing) belongs to draw-pcb.
  Triggers: 检查 PCB / PCB review / ready to fab / DRC / DFM / EMC / EMI /
  FCC / CISPR / thermal hotspot / ground plane / decoupling check / diff
  pair skew / gerber check / cross-reference sch pcb / 准备打样.
---

# Check PCB Skill

PCB 检查 gate +**跨域系统级检查的统一归口**。draw-pcb 画完 → 本 skill 跑全套（含跨 sch+pcb 的 EMC / thermal / cross / parasitic SPICE）→ 通过后进 `release` umbrella（文档 + Gerber + vendor + 下单包）。

## Related Skills

| Skill | Purpose |
|-------|---------|
| `draw-pcb` | 上游：画出 .kicad_pcb |
| `check-schematic` | 上游产出 `schematic.json`（EMC / thermal / cross / parasitic 都要消费）|
| `release` | 下游 umbrella：吃本 skill 通过状态后做 Gerber 导出 + 文档生成 + vendor 决策 + 下单包 |

## What This Skill Does

| 检查类别 | 入口 | 输入 | 性质 |
|---|---|---|---|
| DRC | KiCad CLI 或 pcb analyzer | `.kicad_pcb` | pcb 单侧 |
| PCB 几何 / 信号 / 布局 | `analyze_pcb.py --full` | `.kicad_pcb` | pcb 单侧 |
| 丝印 ↔ 设计 spec 一致性（SK-002） | `analyze_pcb.py`（自动）| `.kicad_pcb` + 项目 `CLAUDE.md` §技术参数表 | pcb 单侧 |
| Gerber & drill | `analyze_gerbers.py` | `gerbers/` | pcb 单侧 |
| sch↔pcb 一致性 | `cross_analysis.py` | schematic.json + pcb.json | **跨域** |
| Thermal hotspot | `analyze_thermal.py` | schematic.json + pcb.json | **跨域** |
| EMC pre-compliance（18 类 44 规则）| `analyze_emc.py` | schematic.json + pcb.json | **跨域** |
| Parasitic-aware SPICE | `extract_parasitics.py` + check-schematic 的 `simulate_subcircuits.py --parasitics` | schematic.json + pcb.json | **跨域** |
| Lifecycle / temperature audit | `lifecycle_audit.py` | sch BOM + pcb | 可跨域 |
| Diff 对比（前后两版）| `diff_analysis.py` | 两个 run | 可选 |
| What-if 参数扫 | `what_if.py` | analyzer JSON | 可选 |

**必读 floor（按任务量）**：单项检查（只 DRC / 只 EMC）→ `run_steps.md` 对应 Step；全套 check → + `emc_severity.md`；写 design review → + `report-generation.md`。

## Workflow（建议顺序，按需跳过）

**开跑前先打 roster**：`python3 scripts/roster_steps.py <project_root>`（相对本 skill 目录）—— 机械判定 9 步 READY / NO-DATA / TOOL-GATED。**数据有无以脚本为准**（模型不许凭印象判"没数据"）；用户要不要跑（scope）仍由模型裁剪。收尾总结的 ⏭ 原因直接引用 roster 打印的理由，或写「用户未要求」。

```
Step 0 schematic.json 在不在（缺 → 先跑 check-schematic 的 analyze_schematic.py 现造，非可跳步）
   → Step 1 analyze_pcb.py --full
   → Step 2 cross_analysis.py（sch↔pcb）
   → Step 3 analyze_gerbers.py（如已导 fab）
   → Step 4 analyze_thermal.py
   → Step 5 analyze_emc.py（总是跑，44 规则，--market eu/us/...）
   → Step 6 extract_parasitics.py + check-schematic SPICE（可选）
   → Step 7 lifecycle_audit.py（联网 + MPN 时）
   → Step 8 写 design review（report-generation.md checklist）
```

每个 Step 完整命令 + flag 说明 → `references/run_steps.md`。

**每跑完一个 Step 打印一行**「Step N ✓ <产物 / 关键计数>」——不打印不算跑过。

> **Step 3 Gerber 审查的时序**：如要在过 release umbrella 之前先验 Gerber，单跑
> `release/scripts/export_gerbers.py <project>/kicad/<name>.kicad_pcb` 标准导出，
> 再回 Step 3 跑 `analyze_gerbers.py <gerbers/>`。验过后正式 release 加
> `--skip-fab-export` 复用即可。

## 跨域检查为何归本 skill

时间线决定归属：跨 sch+pcb 的检查只有在 PCB 阶段才有数据可吃。check-schematic 阶段跑不了 EMC（没 pcb 几何）、跑不了 thermal（没 pcb 铜面积）、跑不了 cross（没另一侧）。所以跨域检查不应"再单建一个 skill"，应统一归 PCB 阶段的检查 gate。

## Output JSON 字段速查

`pcb.footprints[].x/.y` / `pcb.zones[].net`（int ID）/ `pcb.tracks[]` / `findings[]` (按 `rule_id` 筛) / `summary.emc_risk_score` / `per_net_scores[]`。完整 schema + `--schema` 命令 → `references/json_schema_cheat.md`。

## EMC 严重度处置

CRITICAL → fab 前必修；HIGH → 强烈建议；MEDIUM → 看上下文；LOW/INFO → 参考。EMC 不是合规预测器，覆盖 ~70% 常见错，过没过看实验室。详细 → `references/emc_severity.md`。

## 收尾总结（每次 check 跑完必发，给用户看）

**任何 check-pcb 会话结束前**——不管跑了多少步、有没有 finding、用户有没有问——Claude
都必须在最后给用户一份"本次检查清单"。让用户一眼能看出"哪些被看过了、哪些没看、为什么没看"。

格式（块状，终端友好）：

```
─── check-pcb 本次跑了 ───
✓ DRC                       <X> violations / <Y> unconnected（已 triage <Z>）
✓ analyze_pcb --full        findings: BLOCKER <a> / CRITICAL <b> / HIGH <c> / MEDIUM <d>
✓ cross_analysis            sch↔pcb: <一致 / N 处不一致>
✓ analyze_emc --market eu   findings 总 <n>，CRITICAL <m>
⏭ analyze_thermal           skipped: <原因>
⏭ extract_parasitics+SPICE  skipped: <原因>
⏭ analyze_gerbers           skipped: gerber 还没导出（先跑 release/scripts/export_gerbers.py）
⏭ lifecycle_audit           skipped: 没联网 / 没 MPN
─── verdict ───
<pass / fail / pending>  next: <进 release / 退回 draw-pcb 调 X>
```

- 跑了的标 `✓`，跳过的标 `⏭` 并写 1 行**原因**（"没装 ngspice" / "没 datasheet" / "用户没要求"）
- finding 计数按严重度展开，不要只给总数
- verdict 必给三选一：`pass` / `fail` / `pending`，附 1 行下一步
- 如果是部分跑（用户只要 EMC），仍然列全 Step 0–8 与 DRC，没跑的写 `⏭ 用户未要求`

## Hand-off

- ✅ 全 CRITICAL/HIGH 解决，DRC clean，cross 一致 → 进 Phase 5 `release`（umbrella：文档 + Gerber + vendor + 下单包）
- ❌ 有 BLOCKER → 退回 `draw-pcb` 调布局 / 改走线 / 加 stitching via

## references

- `run_steps.md` — Step 0–8 完整命令
- `json_schema_cheat.md` — analyzer JSON 字段速查
- `emc_severity.md` — 严重度处置
- `pcb-layout-analysis.md` — pcb 分析方法
- `emc-methodology.md` — EMC 思路
- `pcb-emc-rules.md` — 44 条规则全文
- `emc-standards.md` — FCC / CISPR / 汽车 / 军规 标准对照
- `output-schema.md` — analyzer JSON 完整 schema
- `report-generation.md` — design review 报告模板
- `datasheet-verification.md` / `diff-analysis.md` / `gerber-parsing.md` / `manual-*.md` / `net-tracing.md` / `standards-compliance.md` / `what-if.md` — 专项

## 红线

- ❌ 不要绕过 DRC / 不要"通过=零 finding"——是"零 BLOCKER + 已 triage"
- ❌ `analyze_pcb.py` **必须** `--full`，否则 EMC / thermal 缺 per-track 数据
- ❌ EMC 不能写"will pass FCC"——只有实验室能给结论
- ❌ 没跑跨域三项（EMC / thermal / cross）的 review 不算 complete
- ❌ `datasheets/` 缺失时不要写"verified"
- ❌ **SK-002 silk_spec_drift finding 必须 triage**：丝印里出现 CLAUDE.md §技术参数表 没有的 number+unit token，说明设计 spec 翻案后丝印没跟着改 → 物理板会印错字。Heuristic 有可能 false-positive（版本号 / 不该入 spec 的数），但**每条都必须当面 verdict**：要么改丝印，要么把值补进 CLAUDE.md，**不能默认忽略**
- ❌ **不发收尾总结直接收尾 = 没 check**：每次 check-pcb 跑完必须给用户一份"本次跑了 / 跳了"的清单（见上面"收尾总结"块），不管用户问没问
