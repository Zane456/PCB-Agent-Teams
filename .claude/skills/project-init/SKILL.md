---
name: project-init
description: >-
  Phase 0 PCB project skeleton expert. ALWAYS invoke this skill
  when the user wants to create / init / scaffold a new PCB project and
  Projects/<name>/ does not yet exist. Do not create project directories
  or project-level CLAUDE.md by hand. Use this skill first. Generates 5
  standard subdirs (datasheets / kicad / reference_designs / layout /
  docs) + project .gitignore + project CLAUDE.md with 9 required sections.
  Also generates STATUS.md (live dashboard for phase progress + artifact
  index + change log + rollback rules) so CLAUDE.md stays static (compass)
  while STATUS.md tracks live state. Gate: skip if Projects/<name>/CLAUDE.md
  already exists (then route to circuit-design for topology discussion).
  Triggers: 新建项目 / 初始化项目 / 开新板子 / 我要做 X 传感器 / create
  new project / init project / scaffold / open new pcb project.
---

# project-init — PCB 项目骨架生成

## 哲学

每个 PCB 项目都需要一致的目录结构 + 两份顶层文件：
- **`CLAUDE.md`**：项目级**决策快照**（compass，static）。不跟工作区 CLAUDE.md 混 — 工作区写通用规则，项目写电路细节。
- **`STATUS.md`**：项目级 **live dashboard**。phase 进度表 + artifact 索引 + change log（append-only，含回退）+ blocker 列表。CLAUDE.md 保持静态，STATUS.md 跟着进度走。

手动 `mkdir` + 复制两份模板很容易漏文件夹或忘填章节。这个 skill 一行命令搞定。

## 何时用

**触发**：
- 用户说"我要做 X 传感器"、"新建一个 Y 项目"、"开个新板子"
- 工作区没对应 `Projects/<name>/` 目录时
- Phase 0（circuit-design 之前）— 必须先有目录结构才能开始填 BOM

**不触发**：
- 项目已存在（这时直接读 `Projects/<name>/CLAUDE.md`）
- 用户只是要审已有项目（sch 用 `check-schematic` / pcb 用 `check-pcb`）
- 单纯讨论电路概念（不落到具体项目）

## 工作流

```
用户说"我要做 X 传感器板"
   ↓
Claude 调用 project-init：
   python .claude/skills/project-init/scripts/init_project.py <name> [--goal "..."]
   ↓
生成：
   Projects/<name>/
   ├── CLAUDE.md          ← 决策快照模板（9 个章节占位）— static compass
   ├── STATUS.md          ← live dashboard（phase 表 / artifact 索引 / change log / 回退 cheat sheet）
   ├── .gitignore         ← 项目局部垃圾文件 / scratch 过滤
   ├── datasheets/        ← component-preparing 写 evidence + sentinel + 采购 CSV
   ├── kicad/             ← .py + .kicad_sch + .kicad_pcb 全在这
   ├── reference_designs/ ← 参考板的 PDF/sch
   ├── layout/            ← 手画的布局草图
   └── docs/              ← bom.md + 项目文档（release umbrella 会读这里）
   ↓
然后：用 circuit-design 跟用户讨论电路
   ↓
circuit-design 结果填进 Projects/<name>/CLAUDE.md
   ↓
每个 phase 转场，AI 把 STATUS.md 对应 Phase 行 ⛔→🟡→✅ 改一下，append change log
```

## 命令行用法

```bash
".venv/bin/python" \
  ".claude/skills/project-init/scripts/init_project.py" \
  <project_name> --goal "一句话目标"
```

参数：
- `<project_name>`：必填，用小写下划线（如 `voltage_sensor_400v`）
- `--goal "..."`：可选，一句话项目目标。没填会留 `(待 circuit-design 后填写)` 占位
- `--projects-root <path>`：可选，默认 `PCB-Agent-Teams/Projects`

退出码：
- `0`：创建成功
- `2`：项目已存在 / 名称非法
- `1`：模板缺失（不应发生）

## 项目命名规范

- 全小写 + 下划线：`voltage_sensor_400v`、`buck_5v_3a`、`isos_dab_module`
- **不要**用空格、大写、连字符：~~`Voltage Sensor`~~ ~~`Buck-5V`~~
- 描述清楚功能 + 关键参数（电压/电流/精度），方便扫一眼知道是啥

## 两份模板

### `CLAUDE.md`（决策快照，9 章节）

1. **项目目标**（1-2 句）
2. **技术参数表**（输入/输出/精度/隔离/采样率）
3. **完整原理图（v0.X）**：ASCII 拓扑（draw-schematic L3 会对照）
4. **接口定义**：每个连接器/排针的 pin 含义
5. **BOM v0.X**：编号 / 数量 / MPN / 封装 / 备注
6. **关键参数验算**：分压比、滤波 Fc、功耗、CPU 占用
7. **安规布局 checklist**：PCB 阶段对照
8. **设计哲学**：关键设计取舍
9. **未决问题**

模板顶部有 `> **通用规则**见 ../../CLAUDE.md` 提示，避免在项目文件里重写通用知识。

### `STATUS.md`（live dashboard）

| 块 | 内容 |
|---|---|
| 阶段进度表 | Phase 0~5 × 状态（⛔/🟡/✅/⚠/🔄）× 完成日期 × 关键 artifact 路径 |
| 当前 blocker | 等谁回复 / 等什么 artifact |
| artifact 索引 | 已产出文件 link 清单 |
| Change log | append-only，含 Phase 回退条目 |
| 图例 + 回退 cheat sheet | 给后续 AI / 用户对照 |

**约定**：每个下游 skill 转场（phase 完成 / 进入 / 回退）由 AI 主动改 STATUS.md 对应行 + append change log。skill 自身不直接写 STATUS.md，避免硬耦合。

**回退原则**：append-only，不删历史。Phase 回退 = 旧行保留 + append 🔄 redoing 行 + 下游所有 ✅ 改 ⚠ stale。

## 不重复造轮子

- 不写 git 仓库初始化（工作区不强制 git）
- 不创建 `kicad/<name>.py` 占位（draw-schematic 会创建）
- 不下载 datasheet（component-preparing 会下）
- 不调任何外部 API
- 会创建项目级 `.gitignore`，屏蔽 `.DS_Store`、`__pycache__/`、KiCad `.history/`、`datasheets/component_selecting/_scratch/`、`_pending_*.json`、`datasheets/_archive/`

## 下一步建议（脚本输出会提示）

```
Phase 1   → circuit-design             讨论电路结构
Phase 2   → component-selecting-JP     出 shortlist
Phase 2.5 → component-preparing        落资产 + 写 BOM sentinel + 采购 CSV
Phase 3   → draw-schematic             生成 .kicad_sch
Phase 3.5 → check-schematic            sch 检查 + SPICE
Phase 4   → draw-pcb                   生成 .kicad_pcb
Phase 4.5 → check-pcb                  pcb 检查 + EMC + thermal + 跨域
Phase 5   → release                    Gerber + 文档 + vendor + 下单包（umbrella）
```

## 触发关键词

中文：新建项目 / 开新板子 / 新项目 / 做一块 X 板 / 创建项目骨架
English: start new project / create new pcb / new sensor board / new board project / init project skeleton
