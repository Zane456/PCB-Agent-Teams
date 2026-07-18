# Phase 0 用户偏好（4 轴 · session sticky · 用于推荐不用于过滤）

session 第一颗 role 跑前用 `AskUserQuestion` 一次问完 4 轴；后续 role 不再重问（除非用户主动改）。单颗也要问。

## 偏好的唯一用途

**只在 Final LLM review 阶段起作用**——不参与 Discovery 抽 MPN、不参与脚本 verdict。脚本永远中立跑完、shortlist 字段完整落盘，偏好只在给 user 推 top pick 时排序。user 临时换视角（"严控本看哪个最便宜"）拿同一份 shortlist 重排即可，不重跑脚本。

## 4 轴 + 影响（CN 选项集）

| 轴 | 选项 | 默认 fallback | Final review 怎么用 |
|---|---|---|---|
| 渠道偏好 | (a) 立创直发（顺丰/京东 1-3 天）<br>(b) 与 JLCPCB 打样合单（最省，随板发）<br>(c) 自动选最便宜 | (c) | 命中渠道的候选往前排；解释 top pick 时引用 |
| 品牌偏好 | (a) 国产优先（圣邦微/风华/顺络/兆易/矽力杰）<br>(b) 国际大厂（TI/ST/Murata/Vishay）<br>(c) 都行 | (c) | 命中品牌优先；非命中作候补 |
| 价格 vs 库存 | (a) 严控本<br>(b) 平衡<br>(c) 稳定优先（库存深 + 基础库件） | (b) | 严控本看单价；稳定优先看库存深度 + `is_basic`（立创基础库 = 贴片免换料费且长供） |
| 黑名单 | 用户已知不能用的 MPN | 读 USER.md / 项目 CLAUDE.md | 从推荐里剔除（不从 shortlist 剔除） |

## 问完立刻持久化

```bash
python3 .claude/skills/component-selecting-CN/scripts/record_preferences.py \
  --project <name> --channel <code> --brand <code> --price-vs-stock <code> \
  [--blacklist MPN1,MPN2]
```

code key：
- channel：`cn_domestic_fast` / `lcsc_jlcpcb` / `auto_cheapest`
- brand：`domestic_first` / `international` / `any`
- price-vs-stock：`tight` / `balanced` / `stable_first`

写到 `Projects/<name>/_artifacts/component_selecting/user_preferences.json`；**Phase 5 release 直接读它驱动 ORDER_GUIDE**，不写盘 release 会 fail-fast。

## ❌ 禁止

- 不用偏好 filter Discovery（抽样按技术 spec 中立）
- 不用偏好 override 脚本 verdict
- 不因"用户已明示渠道"跳过其余 3 轴
