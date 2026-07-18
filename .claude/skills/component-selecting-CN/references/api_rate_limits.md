# API 限流 + 配额（CN：全部免 key，无 daily quota）

## 节流表（共享引擎 `_dk_throttle.py` 强制执行，跨进程 flock）

| host | 间距 | key | daily quota |
|---|---|---|---|
| jlcsearch.tscircuit.com | 1.0s | 无 | 无 |
| wmsc.lcsc.com | 1.0s | 无 | 无 |
| yaqwsx.github.io（jlcparts 分片）| 无节流（一次性下载 + 7 天缓存）| 无 | 无 |

## 配额数学（吸收 JP 版 quota_math——CN 无配额可算）

- 零 daily quota：longlist 25 颗 × 1 lane = 25 次调用，无预算压力，`--discover` 随便跑
- 仅存的上限是**质量上限**：longlist 硬上限 25 颗（超了 discovery 已在堆重复低质候选，脚本直接 fail）
- jlcsearch 是社区服务（tscircuit 维护）：礼貌性 1s 间距照守；偶发 5xx → 脚本记 `fetch_error` fail-fast，稍后重跑即可，别改节流参数硬怼

## jlcparts 分片缓存策略

- 缓存位置 `lib_cache/sources/jlcparts/`（manifest + attributes-lut + 按需品类分片）
- 7 天 TTL；`--refresh` 强刷；刷新失败自动用旧缓存 + note（离线可用）
- 上游快照 3×/日重建，库存/价格最坏 ~8h 旧——gate 判定够用，下单前 component-preparing 会再核
