---
name: circuit-design
description: >-
  Phase 1 circuit topology and anchor-component discussion expert.
  ALWAYS invoke this skill when the user wants to discuss circuit structure,
  pick a topology, choose anchor parts, or extend a project with a new
  subcircuit (project must already exist; if not, project-init first).
  Do not propose topologies, anchor parts, or subcircuits directly without
  consulting this skill first. Use this skill first. Strict component
  selection / verdict / library vendoring / BOM lock belong to
  component-selecting + component-preparing (post-freeze). Triggers: 帮我
  设计电路 / 怎么搭这个回路 / 选什么拓扑 / 拓扑选哪个 / circuit design /
  topology selection / 讨论电路 / 回路怎么搭 / 前级怎么做 / 隔离怎么选 /
  在 X 下加一个 Y 子电路 / extend project with subcircuit.
---

# circuit-design — 回路结构讨论 skill

## 核心理念（**最重要**）

**这个 skill 只做一件事**：跟用户**对齐回路结构是否合理**。不是选品 gate / 参数验算 gate / BOM 锁定 gate——那些在回路冻结后由 `component-selecting` + `component-preparing` 做（legacy 已有的详细参数直接继承，不重新展开）。

**两阶段**：本 skill = **讨论轻**（可快查 active + 库存辅助，不落严格三件套）；回路冻结后 = **最终严**（component-selecting 对非通用件做 longlist + library + verdict 落盘）。

**skill 不是必走路径**：已确定的步骤跳过，不回头补。

## 何时用 / 不用

| 用 | 不用 |
|---|---|
| project-init 完成进 Phase 1，要讨论拓扑/锚点/子电路 | 拓扑 + 锚点件全锁定 → 直接进 `component-selecting` |
| 用户拓扑不确定，要 2-3 候选对比 | PCB layout → `draw-pcb` / `release` |
| 已有项目要加子电路 | 单 MPN 查询 → `component-selecting-JP --mpn` |
|  | BOM 冻结进 gate → `component-preparing` |
|  | 软件 / 控制算法 → 不属于本工作区 |

## 入场两件事（不可跳过）

**第一件事 — 问"哪些已经定了"**：

> "你这次想讨论什么？拓扑 / 锚点件 / 周边件 / 全部？哪些已经定下来了？"

已锁定的部分（拓扑 / 锚点件 / 电源域 / 差分链）**接受不重做**。
"全部都定了，只剩 X 件没选" → **退出本 skill，让用户直接进 component-selecting**。

**第二件事 — 探测已建子电路**：项目里有 `.kicad_sch` 时**强制**跑 `analyze_schematic.py` 拿 detection JSON，把已建的 LDO/分压/RC/opamp/差分对等列给用户当"已锁定输入"。详细决策树 + 反例 → `references/subcircuit_detection.md`。

## 工作流（Step 0–3 + 落盘）

> 这里的 Step 0–3 是**本 skill 内部的讨论子步骤**，跟工作区流水线的 Phase 编号**无关**（本 skill 整体 = 工作区 Phase 1 一格）。

```
入场 → Step 0 扫盲（仅触及 USER.md [待填] 项时）
     → Step 1 拓扑（仅未锁定时）
     → Step 2 锚点件（仅未锁定时；可调 component-selecting 快速查询）
     → Step 3 周边件（仅未锁定时）
     → 落盘收口（§3 拓扑 + §5 spec 三态冻结表 + 初期可行性评估）
     → 退出，告知用户去 component-selecting
     ↺ 选品可合法退回本 skill：结构变=重开 / 松安规 spec=bless / 纯换等效件=不回
```

Step 详细 + 落盘收口 + 回环回退 → `references/discussion_workflow.md`；落盘格式（§3 ASCII + §5 spec 三态表 + 各章继承）→ `references/claude_md_template.md`。

## 关键规则

1. 不重做用户已锁定的部分
2. CLAUDE.md 写到"未来 AI 能明白结构"即可
3. 选品仅作辅助查询，严格审查留到回路冻结后
4. 选取舍给 ≥2 候选 + trade-off，不直接给"答案"
5. 解释带原理，不"业界一般这样"
6. 触及测试/焊接/采购时跟 USER.md cross-check
7. Step 0 扫盲只问相关 [待填] 项
8. 不硬编码 locale / vendor 名（读 USER.md §0）
9. §5 BOM 表 ID 用 snake_case 功能名，禁 R1 / U2 / C1 电气编号
10. 落盘前给 §5 spec 三态完整性 + 一个【初期可行性评估】（标「初期」，非验证，真验证在 3.5/4.5）
11. 选品退回本 skill 合法：只看回路结构动没动
12. 讨论期设计知识搜索按需触发（locale 无关，仍 ≥2 候选）

规则 1-12 的 ✅/❌ 详例 + 反例 → `references/discussion_rules.md`（9-12 的落盘 / 回退 / 搜索机制另见 discussion_workflow.md + design_search.md）。

## 跟其他 skill 的边界

回路冻结后 → `component-selecting`（严格选品）→ `component-preparing`（资产 + BOM gate，**不再回本 skill**）→ `draw-schematic`。完整「不做什么 / 谁做」对照表 → `references/skill_boundaries.md`。

## 概念示意图（讨论辅助，可选）

用户说"画一下 / 看图" → 调 `picture` skill（`--models nano` = Nano Banana 2）出**概念示意图**（统一信息图风：HV 暖 / LV 冷 / 隔离带灰；**≠ 准确电路图**，准确图归 draw-schematic / draw-pcb）。风格 + 模板 + 调用协议 + 反模式 → `references/illustration_prompts.md`。

## 反模式

10 条具体反例 → `references/antipatterns.md`。

## references

> 正文各处就近的 `→` 指路即按场景路由；下表是全量目录，分三组。

**讨论流程**
- `references/discussion_workflow.md` — Step 0–3 详细 + 落盘收口 + 回环回退 + USER.md 字段表
- `references/discussion_rules.md` — 讨论规则 1-12 详例 + 反例
- `references/subcircuit_detection.md` — 入场探测 .kicad_sch 决策树 + 16 类 detector

**落盘 / 边界 / 铁律**
- `references/claude_md_template.md` — 落盘 §3 / §5 模板
- `references/skill_boundaries.md` — 「这个 skill 不做什么 / 谁做」完整对照表
- `references/electrical_invariants.md` — 工作区跨项目电气铁律
- `references/antipatterns.md` — 10 条反模式

**辅助（搜索 / 出图 / 读外部图）**
- `references/design_search.md` — 讨论期设计知识搜索（按需触发 / 搜什么 / 工具 / URL 规矩）
- `references/illustration_prompts.md` — 概念示意图风格 + prompt 模板（走 picture skill）
- `references/pdf-schematic-extraction.md` — 读懂 PDF / 外部原理图（开发板 / eval board / 参考设计 / app note），提取元件 / 网表 / 子电路（到此为止；生成 sch 归 draw-schematic）
