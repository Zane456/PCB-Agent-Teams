---
name: draw-pcb
description: >-
  KiCad PCB component-placement expert (Phase 4) — an AI toolbox plus an agentic placement loop, not a one-shot pipeline. ALWAYS invoke this skill when placing components into a .kicad_pcb from a frozen schematic + project CLAUDE.md, doing HV/LV/ISO region partitioning, isolation-barrier placement, or GND copper pour. Placement is circuit judgment — run placement_brief to understand loops / decoupling / isolation devices / pin functions BEFORE placing, never grid-fill. Do not string-concatenate .kicad_pcb, place before reading placement_brief, default rot=0 on isolation/polarized parts, or treat check_placement's score as an objective to minimize. Use this skill first. Optional Phase E auto-routes the placed board via the vendored KiCadRoutingTools (KRT). Deep review (analyzer / EMC / thermal / cross-ref) belongs to check-pcb. Triggers: 画 PCB / 布局 / 摆元件 / 区域分区 / 隔离屏障 / GND zone / 自动布局 / 自动布线 / 走线 / draw pcb / place components / generate pcb layout / run pcb placement / route pcb.
---

# Draw PCB — AI 驱动的 KiCad 元件布局

不是一键流水线,是**工具箱 + agentic 回路**:AI 用工具看懂电路、按电路把元件摆开、验证、迭代。
**draw-pcb 独立产出 route-ready 布局**——摆件 + 板框 + GND 铺铜全部摆到位,**不需要在 GUI 里二次补摆**。
布局做好后,**可选 Phase E 用自带的 KiCadRoutingTools(KRT)自动布线**——产出全连通 + DRC 干净的成品板。

> 确定性 vs agentic 的分工:种子分区 / 板框 / 隔离槽 / `bridge_slot` / `add_zones` / DRC 这些
> **机械且要稳定**的事走脚本(硬代码);**摆哪、转多少、回路怎么收紧**这种电路判断走 agentic 回路。
> 别把整个布局做成纯算法——纯算法布局已验证效果差,回路才是质量来源。

## 核心信条(读懂这四条再动手)

1. **布局是电路判断,不是铺格子。** 好布局来自理解回路——去耦电容贴 IC、电流回路面积小、信号链按序、隔离器件横跨屏障。**先 `placement_brief` 看懂电路,再摆。**
2. **`check_placement` 是合法性闸门,不是目标函数。** 它只答"合不合法"(重叠 / 间距 / 越界 / 非隔离件穿屏障)。**绝不**把回路 / EMC / 美观折进一个分数让 AI 最小化——那是用 LLM 重造已删掉的 SA,同样失败。质量是 AI 的判断,靠 brief + 渲染图。
3. **旋转由真实引脚坐标定,不靠封装名猜。** 隔离 / 极性器件**禁止默认 rot=0**——同一个 SOIC-8 引脚可能左右排也可能上下排。**先 `get_geometry` 读该件每个 pad 的真实 x/y**,再按 `barrier_devices` 的 pad→net 把各域引脚转到对应域。
4. **交付物是 route-ready 布局,不是"够用就好"。** 退出条件不是 `score=100`,是下面"route-ready 验收"那张清单——draw-pcb 自己把布局摆到能直接布线,GUI 不补摆。

## 何时用 / 不用

| 用 | 不用 |
|---|---|
| sch ERC pass,要把元件摆进 PCB | sch 没画好(先 `draw-schematic`) |
| 改了 sch 重新布局 | 深度审图 / EMC / thermal / cross-ref(用 `check-pcb`) |
| 区域分区 + 隔离屏障 + GND 铺铜 | 出 Gerber / 下单(用 `release`) |

## 工具箱(`scripts/tools/`,每个独立 CLI,JSON 进出)

| 工具 | 干什么 |
|---|---|
| `init_pcb` | sch → 空 `.kicad_pcb`(底层 `scripts/sch_to_pcb.py`) |
| `placement_brief` | 抽电路事实:域 / barrier 器件 + pad→net / cap-IC 链接 / chains / net-pad |
| `init_layout` | 确定性区域初始解,当种子(底层 `scripts/place_components.py`) |
| `get_geometry` | 每件 ref / center / courtyard bbox / pad / net |
| `move` | 移动 / 旋转元件到目标(target = body-bbox 中心) |
| `check_placement` | 合法性闸门:重叠 / 间距 / 越界 / 穿屏障 → `hard_fail` |
| `render` | 标注 PNG(courtyard / 重叠红框 / 隔离屏障线 / 飞线) |
| `refit_board` | 把 Edge.Cuts + 隔离槽缩到元件实际范围 + margin;返回 `fill_ratio` 紧凑度 |
| `route` | 自动布线(底层 vendored KiCadRoutingTools);产出 `_routed.kicad_pcb` |

参数 + 输出 schema → `references/tools.md`

## 布局回路(A→D,**每步必打印可见输出**)

```
A 理解电路   init_pcb → placement_brief → 读项目 CLAUDE.md 的 placement 意图
   打印:域划分 / barrier 器件 / 去耦对 / chains —— 不打印不算做过
B 按电路布局  init_layout 出种子 → AI 按 brief 摆:去耦贴 IC、回路收紧、
   隔离器件按引脚定向、chain 按序 → move 落子
   打印:这一轮移了哪些件 + 每个为什么
C 验证迭代   check_placement(闸门) + render(看图)
   打印:hard_fail / 违例清单 / 对照 brief 看飞线判断回路紧不紧
   → 修被判断出的具体问题,回到 B,重复
D 收尾       refit_board → bridge_slot → add_zones → run_drc → render 终图
   add_zones 不是无脑收尾:铺哪面 / 哪个 GND 网 / 铺不铺,按 references/copper_pour.md
   逐面逐区判断(安规 veto 优先);多 GND 网用 --nets 按域分开调用
   打印:板框尺寸 / fill_ratio / 铺了哪些 (net,layer) + 为什么 / DRC 违例数 / 终图 /
   route-ready 验收逐条结果
```

## Phase E — 自动布线(可选,布局通过 route-ready 验收后)

```
route <placed.kicad_pcb> [配方参数]  →  _routed.kicad_pcb(KRT 自动布线)
   route 不是裸跑:先给 net 分类(信号/电源/差分对),再按 references/routing_strategy.md
   定 --track-width / --power-nets / --impedance / --ordering;简单全信号板可吃默认
   打印:net 分类 + 选了哪些配方参数 + 为什么 —— 不打印不算判断过
run_drc <_routed.kicad_pcb>  →  几何裁判:0 违例 + 0 unconnected 才算布通
add_zones <_routed.kicad_pcb>  →  布线后重铺 GND 铜(Phase D 那块铜绕的是空板,
   加了走线/过孔后已 stale)。create+fill 幂等一步,铜重新绕开走线/过孔
run_drc <_routed.kicad_pcb>  →  再查:refill 后可能新冒 clearance 违例
   打印:布通网数 / vias / 重铺了哪些 zone / DRC 违例数 + unconnected —— 不打印不算布过
```

> 布线后**必须重铺铜**——Phase D 的铜是绕空板灌的,布完线已过期。
> 铺哪面 / 哪个网仍按 `references/copper_pour.md` 判断,与 Phase D 一致。

Phase E 5 条铁律(先过验收再布线 / 必跑 `run_drc` / `copper_edge_clearance` → 调 `--board-edge-clearance`
重布 / 不在 `_routed` 叠布 / 布完重铺铜)、`route` 配方 flag、KRT vendored + 一次性 `build_router.py`
→ `references/routing.md`;配方逐网判断(线宽 / 电源网 / 差分对阻抗 / ordering)→ `references/routing_strategy.md`。
**先看再跑,别裸跑吃 KRT 默认。**

## route-ready 验收(回路退出条件)

退出 = 9 项逐条全过:`check_placement hard_fail=false` / `run_drc` 无 courtyard·clearance·hole
违例(unconnected 预期忽略)/ 隔离器件跨屏障旋转正确 / 连接器·开关贴板边 / 去耦 HF 电容 ≤2mm 贴 IC /
退化 courtyard 件按真实体积复核 / `refit_board` 后 `fill_ratio` 达标 / Read 终图三域分明信号链按序。
**逐条判断标准 + 各项阈值 → `references/loop.md`。**

任一条不过 → 回 B 修。硬上限 6 轮 + keep-best(存分最高那版);到顶仍不过,
报告剩余项交用户,**不把布局补摆甩给 GUI**。

## 前置依赖

KiCad 10 + `kicad-cli` + bundled `pcbnew` Python + 工作区 `.venv`;`draw-schematic` ERC pass;
`component-preparing` sentinel `.bom_readiness.json` 的 `all_pass`（这道关在上游 `draw-schematic` 入场时已挡,本 skill 不复查——它吃的是已过 gate 的 `.kicad_sch`）。
Phase E 自动布线需 KRT 的 Rust 模块已编译(`build_router.py`,一次性,需 `cargo`)。
下游:`check-pcb`(深度检查)→ `release`(出货)。

## 红线

- ❌ 字符串拼接 `.kicad_pcb` / `.kicad_mod`——一律走工具(底层 `_kicad_python_helper.py` 的 mode)
- ❌ 不跑 `placement_brief` 就摆——等于盲摆,回路 / 隔离 / 引脚全靠猜
- ❌ 给隔离 / 极性器件默认 `rot=0`——旋转看引脚功能(信条 3)
- ❌ 连接器 / 开关摆板内——J* / SW* 必须贴板边(`placement_brief` 的 `edge_devices`),否则线缆够不着
- ❌ 把 `check_placement` 的 score 当目标函数死磕——它只是闸门,质量靠判断 + 看图
- ❌ `score=100` 就报完成——合法 ≠ 好;**必须 Read 渲染图**做视觉判断 + 对照 brief 看回路
- ❌ Edge.Cuts 多闭合环 / passive silk 印 MPN → `references/known_issues.md`

## references

- `loop.md` — A→D 回路详细 + 判断标准(route-ready 验收 / 旋转推理 / keep-best / N)
- `tools.md` — 工具箱参数 + 输出 schema
- `routing.md` — Phase E 自动布线:KRT 构建 / `route` 用法 / 布线铁律
- `routing_strategy.md` — KRT 布线配方逐网判断框架(net 分类 / 线宽 / 电源网 / 差分对 / ordering)
- `copper_pour.md` — GND 铺铜逐面逐区判断框架(五轴否决 / 多 GND 网 / 布线后重铺)
- `claude_md_constraints.md` — 项目 CLAUDE.md 的 `placement` 字段
- `placement_rules.md` — 确定性种子(init_layout)4 阶段算法 + CLAUDE.md placement schema
- `pipeline_phases.md` — pipeline.py 内部 phase 流程 + 板框 / 隔离槽几何
- `known_issues.md` — Edge.Cuts 单闭合环 + silk value audit + 范围边界
- `helper_modes.md` — `_kicad_python_helper.py` 的 mode 清单
