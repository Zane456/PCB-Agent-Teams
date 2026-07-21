---
name: component-selecting-JP
description: >-
  Component selecting expert (JP-only, deterministic API-only
  buyable gate). ALWAYS invoke this skill when picking ANY component for a
  JP-locale project — ICs, modules, connectors, isolated parts, AND generic
  passives (resistor / capacitor / inductor / ferrite_bead); turning a
  longlist into shortlist; or checking whether an MPN is locally buyable
  in Japan. Do not pick MPNs from memory, scrape vendor
  HTML, or quote stock/price without this skill. Use this skill first.
  Calls DigiKey JP + Mouser JP REST APIs + LCSC jlcsearch only; outputs
  verified shortlist JSON. Asset acquisition (datasheet / library /
  evidence / lib_external/ writes) belongs to component-preparing. Non-JP
  locales need a separate skill (component-selecting-CN /
  component-selecting-US, not yet implemented) — do not reuse this path.
---

# Component Selecting — JP-only

Single deterministic entrypoint: `scripts/component_select.py`。
**Does**: buyable_gate (DK_JP / Mouser_JP / LCSC 三 lane 并行) + library_probe
(lib_external + KiCad std + DK models + LCSC C-num) + solderability_gate
(USER.md §3) → 排名 → shortlist JSON。
**Does NOT**: evidence / datasheet / lib_external / 项目 BOM（→ `component-preparing`）。

## Routine（默认 batch 跑完整 BOM，blocker 才中断；最后一次性 review）

**前提**：circuit-design 已锁每颗 role 的 spec（电压 / 电流 / 隔离 / 封装 / 依赖）+ 4 轴偏好已问 = "约束 envelope"。envelope 内候选随便挑、下游不 cascade；envelope 外才需 user 介入。

每颗 role 严格走 5-step（不开 references）：

```
1. Tavily/WebSearch — ★ 一次 ★ search_depth=advanced，抽 10 颗 MPN（一个 query 覆盖所有候选 family）
2. 写 longlist JSON（schema 见下，唯一硬字段 = mpn）
3. 跑脚本（命令见下）
4. ★直接看 stdout summary 表★ 套 4 轴偏好排序 → 出 top pick + 候补（禁止 dump JSON——shortlist JSON 只给下游 component-preparing 读）
5. ★blocker check★：候选全 fail / 全黑名单 / 无 lib_external 命中（焊不上）/ spec 无解 → 立刻 AskUser 放宽哪条约束；envelope 内有戏 → 静默写 shortlist，进下一 role
```

全 BOM 跑完 → 一次性 batch review（见下方 Final review）→ user 在同 spec 内换 candidate 即可，**下游不重跑**（spec 没动）。

❌ **禁项（违反就慢）**
- Tavily 用 `search_depth=fast`（fast 经常空返要重跑）—— 默认 `advanced`
- 拆多次 Tavily（"AMC1311 + ACPL + ADuM" 一句话搜，不要分 3 次）
- routine 跑前打开 `references/*`（references 是 edge case 用的，下表写明触发）
- grep 脚本源码看 longlist 字段（schema 在本文件，唯一硬字段就 `mpn`）
- **dump shortlist JSON 给 LLM 自己排序**（stdout summary 表已含 mpn / 3 lane 价格库存 / lib 状态全字段——直接当排序输入；JSON 只给下游 component-preparing）
- ls 探目录 / mkdir（脚本会自己建路径）
- role 之间并行（前一颗错下游连锁错）

## spec 满足不了时：退回 circuit-design

某 role 在 envelope 内全 fail / 无解时**不要自己硬改 spec 往下走**——按改动性质分三类（🟢 换等效件自己定 / 🟡 松安规 spec / 🔴 改结构）退回 circuit-design：判据见 `references/hard_rules.md`。

## Routine path 必用片段（inline，不要去 references 找）

### longlist JSON 最小 schema

```json
{
  "role": "<role_id>",
  "spec": {"function":"...", "package_pref":[...], "key_constraint":"..."},
  "longlist": [{"mpn":"<MPN>", "manufacturer":"<x>", "package":"<x>"}]
}
```

唯一硬字段：`mpn`。其余字段可空，仅作 LLM 自己决策辅助。

### 路径约定

- longlist 输入：`Projects/<name>/datasheets/component_selecting/<role>_longlist.json`
- shortlist 输出：`Projects/<name>/_artifacts/component_selecting/<role>_shortlist.json`
- 独立调研用 `/tmp/<sandbox>/...` 同结构

### 脚本命令（标准路径）

```bash
python3 .claude/skills/component-selecting-JP/scripts/component_select.py \
  --longlist <longlist_path> --project-path Projects/<name> \
  --expected-role <role> --fetch-web --summary \
  --output <shortlist_path>
```

### 替代入口（非 routine，特定场景才用）

- **`--mpn <MPN>`**：单 MPN buyable 速查，免写 longlist（"这颗 X 日本买得到吗"）
- **`--discover --keywords "..."`**：无候选 MPN 时从 DK / LCSC API 反向拉（Tavily 配额耗尽 / 早期粗调研）
- 完整参数见 `references/script_usage.md`

### Discovery query

一句话覆盖所有候选 family（不拆多次），目标 10 颗 MPN、按技术 spec 中立采样（日系/欧美/国产都拉），偏好不参与。模板见 `references/discovery_workflow.md`。

### 4 轴用户偏好（session 第一颗 role 跑前 AskUser 一次，sticky）

session 第一颗 role 跑前 `AskUserQuestion` 一次问完 4 轴（渠道 / 品牌 / 价格 vs 库存 / 黑名单），答完调 `record_preferences.py` 持久化到 `_artifacts/component_selecting/user_preferences.json`（Phase 5 release 直接读它驱动 ORDER_GUIDE，不写盘会 fail-fast）。偏好**只用于 Final review 排序**，不参与 Discovery / 脚本 verdict。选项全集 + code key + 脚本命令 + 影响细则见 `references/preferences_4axis.md`。

### 关键件 vs 被动件（用于 batch review 展示粒度，不再决定是否 ask）

- **关键件**（IC / 隔离器 / DC-DC 模块 / 控制器 / ADC / MOSFET / HV 连接器 / **TVS**）：batch review 单独列项 + 候补 1-2，user 可逐颗换 pick。TVS 等特定 role 的 `--expected-role` flag 见 `references/script_usage.md`
- **被动件**（R/C/L/ferrite/LED/通用二极管/通用排针）：batch review 折叠展示（top pick only），user 不需要逐颗审

### 调度顺序与 BOM ID 命名（硬约束）

- **调度顺序 AI 自挑**（锚点件 → 隔离链 → 控制链 → 接口 → 被动件）。**禁止** AskUser "先做哪颗 / 优先级 / 起点"。AskUser 只用在：4 轴偏好（session 一次）/ blocker（spec 市场无解）/ Final batch review（一次）/ 真正的设计分叉。
- **BOM role 一律 snake_case 功能名**（`iso_amp` / `iso_dcdc` / `hv_terminal`，同项目 CLAUDE.md §5 ID 列）——shortlist JSON `role`、stdout、change log、blocker、对话回引都遵守。**禁止** R1 / U2 / C1 电气编号（Phase 3 sch 才有）。

### Final review（全 BOM 跑完后一次性 batch review）

全 BOM 表一次性展示（关键件单列 + 候补 1-2 / 被动件折叠 top pick only），4 轴对 pass 候选排好序 → top pick + 候补（一句话引用偏好理由）。top pick 跟项目 spec / USER.md 矛盾 → `blocked` 复核（blocker check 阶段已触发，这里是 fallback）；不矛盾 → user 可在同 spec 内换 candidate（不重跑下游、不能 override hard gate）。

## 输出契约

- stdout `--summary`：脚本渲染好的表，**直接 cat 给 user 不二次美化**；也是 LLM 4 轴排序的输入——不 dump JSON 二次校验
- JSON `--output`：**只给下游 component-preparing 读**（它读 JSON 不 grep stdout）；本 skill 排序阶段禁止打开

## 交棒：→ component-preparing

shortlist 落盘后回告 user 路径 + 「下一步 component-preparing 抓 datasheet / library / evidence」。

## references — 触发条件 → 文件（routine 路径默认全部不读）

| 何时打开 | 文件 |
|---|---|
| 怀疑 hard gate 判定边界（buyable / library / solderability 不清）| `hard_rules.md` |
| 4 轴偏好怎么具体落到排序细节有疑问 | `preferences_4axis.md` |
| Discovery 抽 MPN 覆盖窄 / 漏 family | `discovery_workflow.md` |
| 脚本退出码异常 / phase 内部失败需要看源 | `pipeline_internals.md` |
| 脚本 quota fail（DK / Mouser 额度耗尽）| `quota_math.md` |
| API 限流参数不清楚 | `api_rate_limits.md` |
| shortlist JSON 下游字段需要查 schema | `output_format.md` |
| 替代 vendor 不在 DK / Mouser / LCSC 内（Akizuki / Marutsu 等）| `jp_vendor_priority.md` |
| `--longlist` 之外的入口（`--mpn` / `--discover`）| `script_usage.md` |

❌ Routine 跑选品**不打开任何 references**——完整流程本文件已说尽。
