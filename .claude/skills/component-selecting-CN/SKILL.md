---
name: component-selecting-CN
description: >-
  Component selecting expert for China-mainland (CN) locale
  projects ONLY — JP locale → component-selecting-JP, do not reuse this
  path. Zero API keys, deterministic API-only buyable gate. ALWAYS invoke
  this skill when picking ANY component for a CN-locale project — ICs,
  modules, connectors, isolated parts, AND generic passives (resistor /
  capacitor / inductor / ferrite_bead); turning a longlist into shortlist;
  or checking whether an MPN is buyable on LCSC. Do not pick MPNs from
  memory, scrape vendor HTML, or quote stock/price without this skill. Use
  this skill first. Calls LCSC jlcsearch (+ jlcparts shards) only — no
  DigiKey / Mouser, never ask the user for API keys. Outputs verified
  shortlist JSON. Asset acquisition (datasheet / library / evidence /
  lib_external/ writes) belongs to component-preparing.
---

# Component Selecting — CN-only（零 key）

薄壳 skill：共享引擎在 `component-selecting-JP/scripts/component_select.py`（locale 由 yaml 驱动，修引擎去那边；**本 skill 依赖 JP skill 目录存在，不可单独拷走**）；本目录只有 wrapper（`scripts/component_select.py`，自动注入 CN 身份与 `--locale 中国大陆`）+ `scripts/record_preferences.py` + CN references。
**Does**: buyable_gate（LCSC 单 lane，免 key）+ library_probe（lib_external + KiCad std + LCSC C-num）+ solderability_gate（USER.md §3）→ 排名 → shortlist JSON。
**Does NOT**: evidence / datasheet / lib_external / 项目 BOM（→ `component-preparing`）。

## Routine（默认 batch 跑完整 BOM，blocker 才中断；最后一次性 review）

**前提**：circuit-design 已锁每颗 role 的 spec + 4 轴偏好已问 = "约束 envelope"。envelope 内候选随便挑、下游不 cascade；envelope 外才需 user 介入。

每颗 role 严格走 5-step（不开 references）：

```
1. 关键件：Tavily/WebSearch ★一次★ search_depth=advanced 抽 10 颗 MPN；
   被动件：跳过搜索，直接 --discover 参数化拉候选（见下，免 key 无配额）
2. 写 longlist JSON（schema 见下，唯一硬字段 = mpn）；--discover 产物即 longlist
3. 跑脚本（命令见下）
4. ★直接看 stdout summary 表★ 套 4 轴偏好排序 → 出 top pick + 候补（禁止 dump JSON）
5. ★blocker check★：脚本 exit 2 = 全 fail 的**确定性信号**（别靠看表猜）/ 全黑名单 /
   spec 无解 → 立刻 AskUser 放宽哪条约束；envelope 内有戏 → 写 shortlist 并打一行回执
   `decision: <role> ok → <shortlist_path>`，进下一 role
```

全 BOM 跑完 → 一次性 batch review（见 Final review）→ user 同 spec 内换 candidate 即可，**下游不重跑**。

❌ **禁项（违反就慢）**
- Tavily 用 `search_depth=fast`；拆多次 Tavily（一句话覆盖所有候选 family）
- routine 跑前打开 `references/*`（edge case 才开，触发见下表）
- grep 脚本源码看 longlist 字段（唯一硬字段就 `mpn`）
- **dump shortlist JSON 给 LLM 自己排序**（stdout 表已含全字段；JSON 只给 component-preparing）
- ls 探目录 / mkdir；role 之间并行

## spec 满足不了时：退回 circuit-design

envelope 内全 fail / 无解时**不要自己硬改 spec 往下走**——按 🟢 换等效件自己定 / 🟡 松安规 spec / 🔴 改结构分类退回 circuit-design：判据见 `references/hard_rules.md`。

## lifecycle = unverified（CN 特有，3 行必读）

LCSC 无 NRND / 生命周期数据，每条结果自动带 `lifecycle: "unverified"`——这是诚实标注，不是检查通过。关键件（IC / 隔离器 / 控制器）Final review 时提醒 user 自行核实在产状态；被动件通用尺寸不用管。

## Routine path 必用片段（inline，不要去 references 找）

### longlist JSON 最小 schema

```json
{
  "role": "<role_id>",
  "spec": {"function":"...", "package_pref":[...], "key_constraint":"..."},
  "longlist": [{"mpn":"<MPN>", "manufacturer":"<x>", "package":"<x>"}]
}
```

### 路径约定

- longlist 输入：`Projects/<name>/datasheets/component_selecting/<role>_longlist.json`
- shortlist 输出：`Projects/<name>/_artifacts/component_selecting/<role>_shortlist.json`
- 独立调研用 `/tmp/<sandbox>/...` 同结构

### 脚本命令（标准路径）

```bash
python3 .claude/skills/component-selecting-CN/scripts/component_select.py \
  --longlist <longlist_path> --project-path Projects/<name> \
  --expected-role <role> --fetch-web --summary \
  --output <shortlist_path>
```

### 替代入口

- **`--mpn <MPN>`**：单 MPN buyable 速查（"这颗 X 立创买得到吗"）
- **`--discover --role <role> --param key=value`**：被动件参数化海选的**常规路径**（与 JP 相反，免 key 无配额随便跑）。⚠ **产物只是 longlist（step 1，不是选品终点）**——必须回 `--longlist` 跑 buyable / solderability 验证后才算 pick，快照数据不当成品报。R/C 走 jlcsearch 类型化端点（`--param resistance=1000 --param package=0603`）；电感 / 磁珠自动落 jlcparts 分片（首跑下载缓存到 lib_cache）。细则见 `references/script_usage.md`

### 4 轴用户偏好（session 第一颗 role 跑前 AskUser 一次，sticky）

问完 4 轴（渠道 / 品牌 / 价格 vs 库存 / 黑名单）后立刻调本 skill 的 `scripts/record_preferences.py` 持久化。偏好**只用于 Final review 排序**；这道 gate 由 Phase 5 release fail-fast 强制（选品期引擎不读偏好文件），不写盘 release 会打回。CN 选项全集 + code key 见 `references/preferences_4axis.md`。

### 关键件 vs 被动件（batch review 展示粒度）

- **关键件**（IC / 隔离器 / DC-DC 模块 / 控制器 / ADC / MOSFET / HV 连接器 / TVS）：单独列项 + 候补 1-2；TVS 必传 `--expected-role tvs`
- **被动件**（R/C/L/ferrite/LED/通用二极管/排针）：折叠展示 top pick only

### 调度顺序与 BOM ID 命名（硬约束）

- **调度顺序 AI 自挑**（锚点件 → 隔离链 → 控制链 → 接口 → 被动件），**禁止** AskUser 问优先级
- **BOM role 一律 snake_case 功能名**（`iso_amp` / `hv_terminal`），**禁止** R1 / U2 电气编号

### Final review（全 BOM 跑完后一次性）

全 BOM 表一次性展示，4 轴对 pass 候选排序 → top pick + 候补（一句话引用偏好理由）；关键件附 lifecycle 自查提醒。user 可同 spec 内换 candidate，不能 override hard gate。

## 输出契约

- stdout `--summary`：脚本渲染好的表**直接 cat 给 user**；单 LCSC lane + lifecycle 注脚
- JSON `--output`：schema `component-selecting-CN/v1`，**只给下游 component-preparing 读**

## 交棒：→ component-preparing

shortlist 落盘后回告 user 路径 + 「下一步 component-preparing 抓 datasheet / library / evidence」。

## references — 触发条件 → 文件（routine 默认不读）

| 何时打开 | 文件 |
|---|---|
| hard gate 判定边界不清 / 退回分类 | `references/hard_rules.md` |
| 4 轴偏好落到排序的细节 | `references/preferences_4axis.md` |
| Discovery 抽 MPN 覆盖窄 / 参数化端点列表 | `references/discovery_workflow.md` |
| 脚本退出码异常 / phase 内部失败 | `references/pipeline_internals.md` |
| API 限流参数（jlcsearch / wmsc / jlcparts）| `references/api_rate_limits.md` |
| shortlist JSON 下游字段 schema | `references/output_format.md` |
| LCSC 之外渠道（szlcsc / 淘宝）怎么定位 | `references/cn_vendor_priority.md` |
| `--mpn` / `--discover` / jlcparts_shard CLI 细节 | `references/script_usage.md` |

❌ Routine 跑选品**不打开任何 references**——完整流程本文件已说尽。
