---
name: check-schematic
description: >-
  Schematic review gate (Phase 3.5) — sch-only deep check expert.
  ALWAYS invoke this skill when auditing an existing .kicad_sch, running
  ERC triage, sch analyzer (structure + signal detection + rule IDs),
  subcircuit SPICE simulation (regulator/divider/RC/LC/opamp/crystal), or
  datasheet pin-level cross-check. Do not declare a schematic 'verified'
  without running this skill's analyzer + SPICE pipeline. Use this skill
  first. From-scratch generation belongs to draw-schematic; cross-domain
  checks (EMC / thermal / sch↔pcb / parasitic SPICE) belong to check-pcb.
  Triggers: 检查原理图 / schematic review / ERC / SPICE 仿真 / subcircuit
  simulation / divider ratio / opamp gain / crystal load cap / filter
  cutoff / regulator FB voltage / 原理图通过没.
---

# Check Schematic Skill

Phase 3.5 原理图检查 gate。draw-schematic 画完 → 本 skill 跑全套 sch 单侧检查 → 通过后才进 Phase 4 画 PCB。

## Related Skills

| Skill | Purpose |
|-------|---------|
| `draw-schematic` | 上游：画出 .kicad_sch（本 skill 的输入）|
| `check-pcb` | 下游：跨域检查（EMC / thermal / cross-ref / parasitic SPICE）|
| `circuit-design` | 上游：拓扑确定 + 子电路对齐；PDF 参考设计提取也归 `circuit-design/references/pdf-schematic-extraction.md` |

## What This Skill Does（单侧能跑通的部分）

| 检查类别 | 入口 | 输入 |
|---|---|---|
| Schematic structural + signal analysis | `analyze_schematic.py` | `.kicad_sch` |
| ERC（KiCad 内置）| KiCad CLI 或 analyzer 解读 | `.kicad_sch` |
| Power tree、regulator、bridge、protection 等子电路检测 | `analyze_schematic.py` 的 `findings[]` | sch 单侧 |
| SPICE 仿真验证（rc/lc/divider/opamp/crystal/feedback/decoupling）| `simulate_subcircuits.py` | `analysis.json` |
| RC 抗混叠 / 分压 AA cap / 差分 fc sweep | `aa_filter_sim.py` | `analysis.json` |
| Monte Carlo 容差分析 | `simulate_subcircuits.py --monte-carlo` | sch 单侧 |
| Behavioral opamp 模型生成 | `spice_model_generator.py` | datasheet |
| Datasheet 交叉验证（pin / 电气参数）| analyzer + datasheets/ | sch + datasheets/ |

**不在本 skill 范围**（属于 check-pcb，因为要 pcb.json）：EMC pre-compliance、thermal hotspot、sch↔pcb cross_analysis、parasitic-aware SPICE。

## Workflow（标准三步）

### Step 1 — 跑 sch analyzer
```bash
python3 <skill-path>/scripts/analyze_schematic.py design.kicad_sch \
  --analysis-dir analysis/
```
产出 `analysis/<run_id>/schematic.json`，包含 components / nets / ic_pin_analysis / findings[]（含 rule_id、detector、severity）。

### Step 2 — 跑 SPICE 仿真（有 simulator 就跑）
```bash
which ngspice ltspice xyce  # 检测
python3 <skill-path>/scripts/simulate_subcircuits.py \
  analysis/<run_id>/schematic.json \
  --output analysis/<run_id>/spice.json
```
没有任何 simulator 装就跳过，并在报告里写明"SPICE skipped"。**不要把缺仿真器当作错误。**

**信号链 fc 验证**（同步跑）：
```bash
python3 <skill-path>/scripts/aa_filter_sim.py \
  analysis/<run_id>/schematic.json \
  --output analysis/<run_id>/aa_filter_sim.json
```
补 `simulate_subcircuits.py` 漏的项：voltage-divider 自带的 AA cap、hierarchical 子图被切碎的 net、差分 R-R-C 桥（OUTP/OUTN 经 R 后并 C_diff）。出 `AAF-SE` / `AAF-DIFF` finding + `fc_analytic_hz` vs `fc_simulated_hz` 对比；±5% 内 pass。CLAUDE.md 写 fc 目标的项目，把 finding 里的 fc 跟设计 spec 比一遍。

### Step 3 — 交叉验证 + 写报告
- 看 `findings[]`，按 `rule_id` 分类：CRITICAL / HIGH 必须解决，MEDIUM 评估
- 看 SPICE `sim_report.json`：pass / warn / fail / skip 四态
- 数值对比：把 finding 里声称的关键参数（fc、Vout、gain、FB pin 电压）跟仿真数对一遍
- 有 `datasheets/` 时用 pin-level 验证；无则在报告里写明 verification gap（不要写"verified"）

详细方法论见：
- `references/schematic-analysis.md` — 分析方法
- `references/datasheet-verification.md` — datasheet 交叉验证
- `references/output-schema.md` — analyzer JSON 结构
- `references/report-generation.md` — 报告写法
- `references/simulation-models.md` — SPICE 模型精度参考
- `references/config-reference.md` — `.kicad-happy.json` 配置 / suppression 字段（analyzer 自动读，project_config.py 实现）
- `references/file-formats.md` — KiCad 各文件格式逐字段参考（手工解析 .kicad_sch / .kicad_pcb 时查）

## Output JSON 字段速查（最容易踩坑）

| 想要 | 路径 |
|---|---|
| 某 net 上的 pin | `nets[<name>].pins[].component / .pin_number / .pin_name / .pin_type` |
| Unnamed-net 显示名 | `nets[<name>].display_name`（`Ref.PinName` 提示）|
| IC pin 映射 | `ic_pin_analysis[]`（**list**，每项有 `.reference` 和 `.pins[]`）|
| 子电路检测 | `findings[]` flat list，按 `detector` 字段筛选；**不要**读 `subcircuits[]` |

完整 schema：`python3 <skill-path>/scripts/analyze_schematic.py --schema`。

## SPICE 不跑哪些

不仿真：comparator（无 hysteresis 模型）、open-loop opamp、active oscillator、复杂 transistor 拓扑。仿真覆盖详见 `references/simulation-models.md`。

## 收尾总结（每次 check 跑完必发，给用户看）

**任何 check-schematic 会话结束前**——不管跑了几步、有没有 finding、用户有没有问——
Claude 都必须给用户一份"本次检查清单"。让用户一眼能看出"哪些被看过了、哪些没看、为什么没看"。

格式（块状，终端友好）：

```
─── check-schematic 本次跑了 ───
✓ ERC                       <X> errors / <Y> warnings
✓ analyze_schematic         findings: BLOCKER <a> / CRITICAL <b> / HIGH <c> / MEDIUM <d>
✓ simulate_subcircuits      pass <p> / warn <w> / fail <f> / skip <s>
✓ aa_filter_sim             AAF-SE <n> / AAF-DIFF <m>（fc ±5% pass <k>）
⏭ Monte Carlo               skipped: 用户未要求
⏭ datasheet pin-level       skipped: datasheets/ 目录缺 <ref-list>
⏭ behavioral opamp model    skipped: 没拿到 datasheet
─── verdict ───
<pass / fail / pending>  next: <进 draw-pcb / 退回 draw-schematic 改 X>
```

- 跑了的标 `✓`，跳过的标 `⏭` 并写 1 行**原因**（"没装 ngspice" / "没 datasheet" / "用户没要求"）
- finding 计数按严重度展开，不要只给总数
- SPICE 四态（pass/warn/fail/skip）都要列，skip 写明原因
- verdict 必给三选一：`pass` / `fail` / `pending`，附 1 行下一步

**这不是可选的报告**——是 hand-off 的一部分。漏发 = 用户不知道你看了啥，等于没 check。

## Hand-off

- ✅ 全部 CRITICAL / HIGH 解决，SPICE pass / 合理 skip → 通过 → 进 Phase 4 用 `draw-pcb`
- ❌ 有 BLOCKER → 退回 `draw-schematic` 改 .py，重跑流水线

check-pcb 跑 EMC / thermal / cross 时会复用本 skill 产出的 `analysis/<run_id>/schematic.json`，所以**不要清掉 analysis/ 目录**。

## 红线

- ❌ 不要绕过 ERC（DRC 等价物） — KiCad CLI 报错就是真错
- ❌ `datasheets/` 不存在时不要写"verified" / "per datasheet"
- ❌ SPICE skip ≠ 合规；要在报告里写明 skip 原因
- ❌ `findings[]` 是 flat list，不要按 v1.3 之前的 keyed dict 读
- ❌ **不发收尾总结直接收尾 = 没 check**：每次 check-schematic 跑完必须给用户一份"本次跑了 / 跳了"的清单（见上面"收尾总结"块），不管用户问没问
