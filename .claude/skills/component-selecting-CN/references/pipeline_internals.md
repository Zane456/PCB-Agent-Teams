# Phased Pipeline（共享引擎内部 · CN 视角）

```
Phase 0  SIZE CHECK    → longlist > 25 直接 fail；> 10 警告但继续
Phase 1  LIBRARY PROBE → 离线 lib_external + lib_cache 扫描（毫秒级）
                          ↓ library 状态独立报告，不阻止 vendor API 调用
                          ↓ 例外：role ∈ {capacitor, resistor, ferrite_bead,
                          ↓ inductor_smd, inductor_th} 整段跳过，library_gate
                          ↓ 自动 pass（KiCad std footprint 已覆盖通用尺寸）
Phase 2  VENDOR API    → LCSC 单 lane（jlcsearch 主 + wmsc C 码兜底），免 key
Phase 3  VERDICT       → buyable_gate（单源 pass 政策）+ library_gate
                          + solderability_gate + lifecycle 标注 + 排名
```

CN 与 JP 的差异全部由 `locale_mapping.yaml` 中国大陆 block 驱动（gate_policy / lanes / fx_display / display），引擎代码同一份——修 bug 去 `component-selecting-JP/scripts/component_select.py`，两 locale 同时受益。

## 退出码

- `0`：至少一个候选 pass 或 warn_single_source
- `2`：所有候选 fail
- `3`：缺输入 / longlist 超硬上限 25 / locale unknown
