# 输出契约（output contract · CN）

两个输出通道，职责严格分开。

## 1. stdout `--summary` —— 给真人看的硬代码渲染

单 LCSC lane 表 + lifecycle 注脚，**直接 cat 给 user 不二次美化**：

```
=== Component Selecting / locale=中国大陆 currency=CNY ===

| # | MPN | 厂家 · spec · 封装 | 🇨🇳 LCSC (CNY/在库) | lib |
|---|---|---|---|:-:|
| 1 | <MPN_A> | <厂商> · <spec> · 0805 | ¥0.0016 CNY (8.0M) | ✅ |

注：LCSC 无 NRND/生命周期数据 —— 本表候选 lifecycle=unverified，关键件请自行核实在产状态。
注：购买 URL / datasheet / fail 候选完整数据 → JSON。LCSC 直发或与 JLCPCB 打样合单。
```

约定（与 JP 同源，CN 差异标注）：
- fail 候选隐藏（数据留 JSON）；单元格只显示 `status ∈ {active}`，其余 `—`
- **无 fx 行**（CN 原币 CNY，不换算）；价格自适应精度（≥0.1 两位小数，<0.1 四位——被动件单价常在厘级）
- **无 `local_jp_active` / `lcsc_only_active` 注脚**（单 lane 无车道歧义）
- `warn_single_source` 在 CN 只来自 solderability / library 警告（单源本身 = pass），MPN 后加 ⚠
- lib 列 ✅/❌ 两态；全 fail 时提示回 longlist 重 spec

## 2. JSON `--output <path>` —— 给下游 skill 的稳定字段契约

schema：`component-selecting-CN/v1`（discover 为 `component-selecting-CN/discover-v1`）。下游读 JSON，不 grep stdout。

与 JP 版共同字段见 `component-selecting-JP/references/output_format.md` §2（引擎同一套）。CN 差异：

```
lifecycle              str   恒 "unverified"（CN 每条结果都带；JP 无此字段）
local_jp_active        —     不存在（emit_lane_flags=none 抑制）
lcsc_only_active       —     不存在
vendor_results[i]:
  lane                 str   cn_domestic（LCSC）
  price_jpy_estimated  null  恒 null（fx_display=none）
  fx_rate / fx_source  null  恒 null
  raw_parameters       dict  jlcsearch attributes 归一化（有则出现，喂 key_parameters）
```

下游过滤买源用 `lane == "cn_domestic"`。
