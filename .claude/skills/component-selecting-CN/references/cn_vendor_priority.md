# 中国大陆采购铁律（CN vendor priority）

适用：component-selecting-CN + component-preparing。本 skill wrapper 已钉死 locale=中国大陆（`USER.md §0` 只决定进不进本 skill，不再影响 wrapper 内部路由）。

## 仓优先级 source of truth

唯一权威 = `component-selecting-JP/scripts/locale_mapping.yaml` 的 `中国大陆` block（共享引擎的多 locale 配置文件，物理上放在 JP skill 目录）。改优先级 / 加 vendor / 调 gate 政策 = 只改 yaml，不改脚本、不改本文件。

## 铁律

1. **零 key 是设计底线**：全链路只走 LCSC jlcsearch + jlcparts 公共接口。不接 DigiKey / Mouser、不要求用户申请或填写任何 API key。缺生命周期数据用 `lifecycle: unverified` 诚实标注，不引入 key 依赖来"补齐"。
2. **单源 gate 语义**：`gate_policy.min_local_sources = 1`——LCSC 单 lane active + 库存 ≥ 阈值即 `pass`（reason `single_source_meets_locale_policy`）。没有第二结构化源可交叉验证，warn 不是常态别滥标。
3. **lifecycle=unverified 的处置**：关键件（IC / 隔离器 / 控制器 / 功率器件）Final review 提醒 user 去厂商官网 / 立创详情页自查在产状态；被动件通用尺寸免查。**不许**用"lifecycle 未验证"当 fail 依据。
4. **两条 fulfilment 路等价展示**：立创直发（顺丰/京东 1-3 天）vs 与 JLCPCB 打样合单（最省，随板发）。脚本不替 user 选，4 轴渠道偏好只在 Final review 排序。
5. **szlcsc / 淘宝只是 HTML 兜底**：无结构化 API，仅 `--include-html-vendors` + Firecrawl key 时跑，**不进 buyable gate 判定**。淘宝无 active 状态概念，只能粗判有无卖家。
6. **library 抓取按 locale 路由**：`library_fetch_strategy = lcsc_easyeda`（easyeda2kicad 免 key），不走 DigiKey models 页。

## 国产替代参考（海外料缺货 / 太贵时的常见平替方向）

| 海外 | 国产替代方向 | 备注 |
|---|---|---|
| TI/ADI 运放 | 圣邦微 SGM / 3PEAK 思瑞浦 | pin-to-pin 型号多，核对失调/带宽 |
| Murata/TDK 被动件 | 风华 FH / 顺络 Sunlord / 宇阳 | KiCad std footprint 通用 |
| ST MCU | 兆易创新 GD32 | GD32F 兼容 STM32F pinout，外设寄存器有差异 |
| 通用 LDO/DC-DC | 芯朋微 / 矽力杰 Silergy / 南芯 | 立创基础库常备 |
| Vishay/onsemi 二极管/MOS | 扬杰 YJ / 华润微 / 新洁能 | 核对 Vds/Rds(on) 档位 |

平替只是方向提示——最终 MPN 仍必须走本 skill 的 buyable gate 验证，不许凭表直接定料。
