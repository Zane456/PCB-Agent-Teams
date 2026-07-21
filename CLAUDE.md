# PCB-Agent-Teams 工作区 — 指南针

> 纯路由文件。**不承担 domain 知识 / 工程铁律**——那些归 SKILL.md 或项目 CLAUDE.md。

## FIRST-RUN GATE

`USER.md` 缺失 = 新 clone 未配置 → 先跑 `setup` skill，**别开工**（不 init / 不选品 / 不画图）。
`USER.md` 存在 → 忽略本节，正常路由。（no USER.md → run `setup` first; otherwise ignore this section.）

## 目录速览

```text
PCB-Agent-Teams/
├── CLAUDE.md             ← 本文件
├── USER.md               ← 在手硬件、所属地、能力、偏好（必读；不入 git，从 USER.md.example 复制）
├── lib_external/         ← 共享元件库（CONVENTIONS.md）
├── lib_cache/sources/    ← 外部库只读 cache（pre-filter 池，不被项目引用）
├── .venv/                ← Python 3.12（禁 3.13/3.14）
├── .env                  ← 分销商 API key（不入 git）
├── .claude/
│   ├── references/       ← 工作区级元协议（按需读）
│   │   └── protocols.md  ← USER 维护 / 计划先行 / sub-agent 分工 / 监控 / Phase 编号
│   └── skills/           ← 10 个正式 skill + `setup`（首次配置引导，配完自动失效）
└── Projects/<name>/      ← 项目骨架（用 project-init 生成）
```

## 资源路由

| 场景 | 去哪 |
| --- | --- |
| 用户在手硬件 / 焊接能力 / 所属地 / 偏好 | `USER.md` |
| 跨项目电气铁律（电源域、差分链等） | `.claude/skills/circuit-design/references/electrical_invariants.md` |
| 日本选品规则、JP 替代库表 | `.claude/skills/component-selecting-JP/references/jp_vendor_priority.md` |
| 中国选品规则、国产替代表（零 key 链路） | `.claude/skills/component-selecting-CN/references/cn_vendor_priority.md` |
| API rate limit / DK / Mouser | `.claude/skills/component-selecting-JP/references/api_rate_limits.md` |
| BOM 三类文件生命周期 | `.claude/skills/component-preparing/references/bom_lifecycle.md` |
| 工作区元协议（计划先行 / sub-agent 分工 / 监控） | `.claude/references/protocols.md` |
| 项目专属设计意图、BOM、参数（**static** compass） | `Projects/<name>/CLAUDE.md` |
| 项目 **live 进度** / artifact 索引 / change log / 回退记录 | `Projects/<name>/STATUS.md` |
| 项目骨架模板（CLAUDE.md 9 章节 + STATUS.md dashboard） | `.claude/skills/project-init/templates/` |
| 共享库写入规则 | `lib_external/CONVENTIONS.md` |

## 运行环境：只认 Claude Code

skill 全部在 `.claude/skills/<name>/SKILL.md`，靠 Claude Code 自动发现 + `/<name>` 调用；本文件也靠
Claude Code 自动加载。**没有为其它 agent 做任何适配**——换 agent 需要改的机制见 `README.md` §Using
another agent。可用 skill = 下表 + 非 phase 的 `setup`（见上面 first-run gate），别自己编。

## 阶段 × skill 一表（核心路由）

> **每个 skill 都是工具盒，不是必经流水线。** 任一阶段都可以：用 skill / 手工做 / 跳过让 user 自己接手。skill 内部 multi-step 也可在中途人工审核（render / DRC / 仿真），不满意就回退或改方向，再决定要不要推进下一步。

| 工作区 Phase | skill | 入口 | 产出 |
| --- | --- | --- | --- |
| 0 骨架 | `project-init` | `scripts/init_project.py` | 项目目录 + `CLAUDE.md`（决策快照模板）+ `STATUS.md`（live dashboard）+ `.gitignore` |
| 1 拓扑讨论 | `circuit-design` | Skill 工具 | 拓扑 + 锚点件 + 项目 CLAUDE.md 9 章节 |
| 2 选品 gate | `component-selecting-JP` *(按 locale 路由，见下表)* | `scripts/component_select.py` | shortlist JSON（不写 evidence、不下 datasheet） |
| 2.5 落资产 + BOM gate | `component-preparing` | `accept_shortlist.py` / `distributor_query.py` / `inject_mpn_props.py` / `check_readiness.py` 等 | datasheet PDF + lib_external/components.* + evidence JSON + docs/bom.md + **`.bom_readiness.json` sentinel + 采购 BOM CSV** |
| 3 sch 源码 + 生成 | `draw-schematic` | LLM 写 `.py` → `pipeline.py` | circuit-synth 源码 → `.kicad_sch` + ERC clean + L2/L3 视觉验证（生成 gate） |
| 3.5 sch 检查 gate | `check-schematic` | `analyze_schematic.py` + `simulate_subcircuits.py` | sch analyzer JSON + SPICE 仿真 + design review |
| 4 pcb 生成 | `draw-pcb` | `pipeline.py` | `.kicad_pcb` 区域分区 + GND zone + DRC + 视觉 PDF；**可选 Phase E** 自动布线 → `_routed.kicad_pcb` + refill GND + 二次 DRC（也可跳过 skill，由用户在 KiCad GUI 手布） |
| 4.5 pcb 检查 gate（含跨域） | `check-pcb` | `analyze_pcb.py --full` + `analyze_emc.py` + `analyze_thermal.py` + `cross_analysis.py` | pcb analyzer JSON + EMC + thermal + cross-ref + parasitic SPICE |
| 5 出货 umbrella | `release` *(吞并原 kidoc / kicad fab-export / fab)* | `scripts/build_release.py`（前置：校验 4 轴偏好 sentinel） | `release/<ts>/` + Gerber/CPL/生产 BOM + 文档 PDF（HDD/CE/Design Review/ICD/Manufacturing）+ distributor CSV + JLCPCB vs PCBWay 决策 + ORDER_GUIDE.md + `release_<ts>.zip` |

**两类 BOM 别混**：

- **采购 BOM**（component-preparing 写）→ distributor 下单买料
- **生产 BOM / CPL**（release/scripts/export_gerbers.py 写）→ fab 厂贴片装配
- 详见 `.claude/skills/component-preparing/references/bom_lifecycle.md`

## Locale 路由（按 USER.md §0 所属地）

`component-selecting` 是 phase 名，具体 skill 由 `USER.md §0` locale 决定：

| USER.md §0 所属地 | 选品 skill | vendor 链路 | 状态 |
| --- | --- | --- | --- |
| 日本 | `component-selecting-JP` | DigiKey JP + Mouser JP + LCSC（API + JPY） | ✅ 已实现 |
| 中国大陆 | `component-selecting-CN` | LCSC jlcsearch（免 key）+ jlcparts 分片 + EasyEDA library | ✅ 已实现（零 key） |
| 美国 | `component-selecting-US` | DigiKey US + Mouser US（API + USD） | ⛔ 未实现 |
| 其他 | （待建） | — | ⛔ 未实现 |

**当前规则**：USER.md §0 = 日本 → `component-selecting-JP`；= 中国大陆 → `component-selecting-CN`（零 key，共享引擎在 JP skill scripts/）。
其余 locale **不要静默 fallback**——告诉用户对应 locale 的 skill 还没实现，让 user 决定（手工选品 / 临时改 locale / 或先建对应 skill）。

> Phase 2.5 资产抓取（datasheet + library + evidence）由 `component-preparing` 接手，不在 component-selecting 范围。

## 工作区基础设施

| 资源 | 路径 |
| --- | --- |
| Python venv | `.venv/`（Python 3.12） |
| KiCad CLI | `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli`（v10） |
| circuit-synth / easyeda2kicad / ksa | venv 内（0.12.1 / 1.0.1 / 0.5.6） |

### 分销商 API key（已配在 `.env`）

| 服务 | env var | 状态 |
| --- | --- | --- |
| DigiKey | `DIGIKEY_CLIENT_ID` / `DIGIKEY_CLIENT_SECRET` | ✅ Production |
| Mouser | `MOUSER_SEARCH_API_KEY` | ⚠ 需要时申请 |
| LCSC | — | ✅ 免 key |
| element14 | `ELEMENT14_API_KEY` | ⚠ 需要时申请 |

**第一次进 shell**：`set -a && source .env && set +a`

## 红线

- ❌ 不要在工作区 CLAUDE.md 写**项目专属**或 **domain 铁律**——分别归项目 CLAUDE.md / SKILL.md
- ❌ SKILL.md frontmatter 的 `description:` 不要裸写：值里含 `: `（如 `Triggers: 画 PCB`）会让严格 YAML 解析失败，Claude Code 不报错、静默退化成拿目录名当摘要 → **该 skill 自动触发失效**。一律用折叠块标量 `description: >-` 加两空格缩进
- ❌ 不要把 `.env` key 写进 git
- ❌ 不要凭记忆猜 MPN / footprint / pin
- ❌ 不要把 `draw-pcb` 当一键流水线——摆件 / 旋转 / 铺铜要靠电路判断（brief + render），每一步交付前都该人工 review；Phase E 自动布线也是可选项，用户随时可改用 KiCad GUI 手布
- ❌ **`.claude/skills/` 下任何文件禁止把具体项目当例子或证据**——SKILL.md / references / scripts 必须跨项目通用。具体禁止形式 + 替代写法见下节"skill 通用性铁律"。

## skill 通用性铁律

**原则**：`.claude/skills/` 是工具箱，不是某项目的工作日志。任何 SKILL.md / references / scripts / templates / tests 都必须在**新建空白项目**上同样适用。

**禁止形式**：
- ❌ 文档里写 "voltage_sensor 2026-05-05 血泪 case" 这种带项目名 + 日期的特例引用
- ❌ 命令示例里写死 `Projects/voltage_sensor/...`（应该用 `Projects/<name>/...` 或 `Projects/<your_project>/...`）
- ❌ 反模式举例带项目内 ref（"U2 用 IH0505SH 焊不上"应改成"某 isolated DC-DC 借用近似 footprint pinout 不同"）
- ❌ test fixture 用真实项目名当 sample data（用 `test_project` / `sample_project`）
- ❌ 脚本注释里写 "voltage-sensor-class boards"（改成"single-sided analog GND boards"等技术描述）
- ❌ demo 输出贴具体项目跑出来的实际数字（用占位 `<name>` / `33` 等抽象量）

**例外（明确可保留）**：
- ✅ 命名规范的**示例**：`voltage_sensor_400v` 跟 `buck_5v_3a` / `isos_dab_module` 并列演示"功能_参数"格式时
- ✅ 物理 / 标准里的具体数值：`400V CTI Group II`（IEC 60664）、`3.3V LVCMOS` 这种跟项目无关的电气量

**替代写法**：
- 项目特例 → 抽象成"某类电路 / 某类元件 + 失败模式 + 物理原因"
- 时间戳 → 改成"实测（2026-05）"或直接删
- 项目路径 → 用 `Projects/<name>/` 占位
- 真实数据 → 占位变量或 fixture 里的 `test_project`

**写之前自检**：把 SKILL.md 拷到一个空白的新项目里，描述的步骤 / 命令 / 反例还能不能成立？不能就回去脱敏。

## 历史教训索引

| 教训 | 落地位置 |
| --- | --- |
| ERC 解析 `total_errors == 0` | `.claude/skills/draw-schematic/references/known-bugs.md` |
| Phoenix MKDS 端子 pitch 易错 → 不要凭记忆猜 MPN | `.claude/skills/draw-schematic/references/pipeline_stages.md` 铁律节 |
| LCSC `/datasheet/<C>.pdf` 是 HTML 跳转 | `.claude/skills/draw-schematic/scripts/download_datasheet.py` |
| `.kicad_pcb / .kicad_sch` 不要字符串拼接 | `.claude/skills/draw-pcb/SKILL.md` 红线节 |
| `.py` value/footprint 错位 | `.claude/skills/component-preparing/SKILL.md` fidelity 四检 |
