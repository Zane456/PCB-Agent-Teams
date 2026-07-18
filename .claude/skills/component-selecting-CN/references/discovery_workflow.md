# Discovery workflow（CN：参数化优先，搜索兜底）

## 与 JP 的关键差异

**`--discover` 是被动件的常规路径，不是兜底**——CN 链路免 key 无配额，参数化海选随便跑；Tavily/WebSearch 只留给关键件（IC / 模块 / 特殊连接器）。

## 被动件：参数化 discover（首选）

```bash
python3 .claude/skills/component-selecting-CN/scripts/component_select.py \
  --discover --role resistor --param resistance=1000 --param package=0603 \
  --locale 中国大陆 --summary --output <role>_longlist.json
```

- `--param key=value` 用归一化 SI 值（resistance=1000 = 1kΩ；capacitance 可写 `100000pf`）
- 有 jlcsearch 类型化端点的 role：resistor / capacitor / led / diode / ldo / voltage_regulator / mosfet / bjt / fuse / switch / relay / potentiometer / header / mcu / adc / dac
- **电感 / 磁珠**（jlcsearch 无端点）：自动落 jlcparts 分片车道，首跑下载该品类数据到 `lib_cache/sources/jlcparts/`（7 天缓存），加 `--query 10uH` 缩范围
- 产出 JSON 即 longlist，直接进 `--longlist` 验证或人工挑 MPN

## 关键件：LLM 搜索抽 longlist

一次 Tavily `search_depth=advanced` 覆盖所有候选 family（"AMC1311 alternatives isolated amplifier 立创" 一句话搜，不拆多次），目标 10 颗 MPN，按技术 spec 中立采样——国产（圣邦微/兆易/矽力杰）+ 国际（TI/ST/ADI）都拉，偏好不参与。中文源（立创商城页 / 电子发烧友 / CSDN 选型贴）可信度按"有无实测数据"判断，型号最终一律走脚本验证。

## 覆盖窄 / 漏 family 时

- 加一次 `--discover --role <role> --query <关键词>`（jlcsearch 关键词端点，中英文皆可）
- jlcparts 分片支持 `--category "<manifest 品类名>"` 直接指定品类（`jlcparts_shard.py --list-categories` 看全集）
- 软上限 10 颗 / 硬上限 25 颗（超硬上限脚本直接 fail）
