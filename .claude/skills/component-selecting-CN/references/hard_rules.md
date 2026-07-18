# Hard rules（脚本与 LLM 边界 · CN）

- **No subagents** — longlist 大也不 spawn subagent；脚本 worker 已并行
- **No HTML scraping in buyable_gate** — 只调 LCSC jlcsearch API（+ jlcparts 快照数据）。无 API 的 vendor（szlcsc / 淘宝）在 URL build 时已过滤，仅 `--include-html-vendors` 时走 Firecrawl 慢路径且不进 gate
- **No LLM classification** — availability / price / stock / package / ranking 全读 API 字段，禁止文本匹配
- **No key begging** — 任何情况下不要求用户申请 / 填写 API key；缺数据（如 lifecycle）诚实标注，不引入 key 依赖
- **Fail fast on schema drift** — API 返回结构变了就 `fetch_error: <reason>`，不 paper-over
- **LLM cannot override hard gates** — Final review 只能 ok / blocked，不能把 fail 拉回 pass；也**不能**把 `lifecycle: unverified` 当 fail 依据

## spec 满足不了时：分类退回 circuit-design

某 role 在 envelope 内**全 fail / 无解**时，不自己硬改 spec 往下走。按改动性质三类（与 circuit-design 回退判据同源）：

- 🟢 **纯换等效件**（envelope 内换 MPN，结构 spec 都没动）→ 选品自己定，不回头
- 🟡 **要松安规 spec**（隔离 / 耐压 / CTI / 温度额定）→ **停，AskUser + 回 circuit-design bless**
- 🔴 **要改结构**（加减级 / 换隔离方案 / 动电源域）→ **停，回 circuit-design 重开**

纯性能 spec（Rds(on) 等）松动归 🟢。🟡/🔴 一律交回 circuit-design。
