# 脚本入口（CN wrapper + 共享引擎）

CN 入口是薄壳：`component-selecting-CN/scripts/component_select.py` 自动注入 `--caller-skill component-selecting-CN` 后转发共享引擎（`component-selecting-JP/scripts/component_select.py`）。所有引擎参数原样可用。

## 命令行

### longlist 验证（标准路径）

```bash
python3 .claude/skills/component-selecting-CN/scripts/component_select.py \
  --longlist <path>/<role>_longlist.json \
  --project-path <project_or_tmp_path> \
  --expected-role <role> \
  --fetch-web --summary \
  --output <path>/<role>_shortlist.json
```

### Single MPN 速查

```bash
python3 .claude/skills/component-selecting-CN/scripts/component_select.py \
  --mpn "<MPN>" --project-path <path> \
  --expected-role <role> --fetch-web --summary
```

### 参数化 discover（被动件常规路径）

```bash
# R/C 等（jlcsearch 类型化端点）：--param 用归一化 SI 值
... component_select.py --discover --role resistor \
  --param resistance=1000 --param package=0603 --summary --output <path>

# 电感/磁珠（jlcsearch 无端点 → 自动走 jlcparts 分片）
... component_select.py --discover --role inductor_smd --query 10uH --summary
```

`--discover-source` 可显式选：`lcsc`（关键词）/ `lcsc-parametric` / `jlcparts` / `local`；缺省 `all` = yaml `discover_sources` 顺序。

## jlcparts_shard.py 独立 CLI（引擎侧工具）

```bash
python3 .claude/skills/component-selecting-JP/scripts/jlcparts_shard.py \
  --role inductor_smd --query 10uH --package 0805 --min-stock 100 \
  --basic-only --limit 10 [--refresh] [--output rows.json]
python3 ...(同上)... --list-categories   # 看 manifest 全部品类
python3 ...(同上)... --category "Ferrite Beads" --limit 20
```

缓存在 `lib_cache/sources/jlcparts/`，7 天 TTL，刷新失败自动用旧缓存。

## 路径约定

- `--longlist` 输入：`Projects/<name>/datasheets/component_selecting/<role>_longlist.json`
- `--output` 输出：`Projects/<name>/_artifacts/component_selecting/<role>_shortlist.json`
- 独立调研用 `/tmp/<sandbox>/...` 同结构；`--project-path` 用于 lib 探测和路径作用域

## 特定 role flag：TVS

TVS 必传 `--expected-role tvs`（ROLE_PROFILE 抽 V_RWM / V_BR）。其余关键件传 snake_case role 名。

## 内部 phase / 退出码

→ `pipeline_internals.md`；限流 → `api_rate_limits.md`。
