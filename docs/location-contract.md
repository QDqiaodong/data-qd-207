# 候选震源位置诊断与准入契约

新增纯计算 API `evaluate_location_candidates`：读取现有 `process` 的完整结果、
台站坐标映射、候选位置旅行时表和质量参数，为每个归并事件输出可复核的候选诊断、
逐台站/逐到时证据，以及是否可进入下游定位目录的独立结论。

- 不修改 `process`、复核目录、事件身份、数据集发布或特征血缘。
- 不实现波形处理、震源物理模拟、外部数据获取、CLI 或页面。
- 函数不做 IO、不读取环境、不修改任何输入对象；正常结果可直接
  `json.dumps(..., allow_nan=False)`。
- 输入错误抛 `LocationInputError`（`ValueError` 子类）。验证先于计算，非法调用
  不会返回部分事件结果。

## 公开导入

规范名称：

```python
from seismic_assoc import evaluate_location_candidates, LocationInputError
# 或 from seismic_assoc.location import evaluate_location_candidates
```

为兼容不同命名习惯，也公开 `evaluate_event_locations`、`evaluate_locations`、
`evaluate_candidate_locations`、`assess_event_locations`、
`locate_events`、`locate` 等同义别名；兼容模块包括
`seismic_assoc.location_evaluation`、`seismic_assoc.locations`、
`seismic_assoc.locator` 与 `seismic_assoc.locate`。

## 调用形态

```python
report = evaluate_location_candidates(
    process_result,          # process(data) 的完整 dict；也接受 {"events": [...]}
    station_coordinates,     # {"A": {"lat": 0.0, "lon": 1.0}} 或 {"A": (lat, lon)}
    candidates,              # 候选位置与相位旅行时
    {
        "residual_threshold": 0.1,
        "min_supporting_stations": 3,
        "ambiguity_margin": 0.05,  # 可选；显式给出后启用歧义准入
    },
)
```

也可用关键字 `residual_threshold=...`、`min_supporting_stations=...`、
`ambiguity_margin=...` 代替质量 dict。位置参数允许
`(result, coords, candidates, threshold, min_support, margin)`。

`ambiguity_margin` 被显式提供时必须是有限、非负数，边界差等于 margin 也判定为
歧义。未提供时不执行歧义比较；这对应验收 1 只给定残差阈值和最少支持台站数的调用。

## 输入契约

### `process_result`

只消费 `events[*].arrivals`。每个到时保留原字段：

```json
{"id": "a1", "station": "A", "phase": "P", "time": "102"}
```

- `anchor` 被完全忽略；它是归并窗口锚点，不是发震时刻。修改 `anchor` 不影响结果。
- 一个台站可以有多个不同 `id` 的同相位到时；所有到时都进入证据。
- 到时 ID 在整次调用的所有事件中必须唯一。
- `time` 可以是 JSON 数字、十进制字符串，或当前 `process` 对某些小数输出的
  `"numerator/denominator"` 精确分数字符串。所有时间/旅行时在内部用
  `Fraction` 精确比较，避免边界残差和歧义边界的浮点误差。

### `station_coordinates`

接受：

```python
{
  "A": {"lat": 0.0, "lon": 0.0},
  "B": {"latitude": 1.0, "longitude": 2.0},
  "C": (3.0, 4.0),       # (lat, lon)
}
```

也接受 `process` 输入风格的 `[{"id": "A", "lat": ..., "lon": ...}]`。
纬度必须在 `[-90, 90]`，经度必须在 `[-180, 180]`，数值必须有限。到时或旅行时
表引用坐标映射中不存在的台站时整体拒绝。

### `candidates`

标准形状：

```json
[
  {
    "candidate_id": "c1",
    "lat": 0.0,
    "lon": 0.0,
    "travel_times": {
      "A": {"P": 2.0, "S": 3.5},
      "B": {"P": 4.0, "S": 6.5}
    }
  }
]
```

- `candidate_id` 必须唯一且非空。
- `lat/lon` 必须合法、有限。
- 旅行时按 `station -> phase(P|S) -> nonnegative number` 索引。
- 缺少某个 station/phase 预测表示该条到时在该候选下不可评估；不是残差 0，也不是
  离群证据。显式 `null` 同样按缺失处理。
- 拒绝负旅行时、非有限数、未知台站、非法相位和不完整结构。
- 为兼容便利，也接受相位优先表 `{"P": {"A": 2}}`、
  `{"A.P": 2}` / `{"A:P": 2}` / `{"A_P": 2}`、
  `[{"station":"A","phase":"P","travel_time":2}]`，以及台站到数值的扁平表
  （该数值同时作为 P/S 预测）。规范数据仍推荐两级 P/S 形状。

### 质量参数

| 字段 | 要求 |
| --- | --- |
| `residual_threshold` | 正数，使用与 `time` 相同的单位 |
| `min_supporting_stations` | 整数，至少 `3`；布尔值、小数或 `"3"` 不接受 |
| `ambiguity_margin` | 可选；显式提供时为有限非负数，单位同时间 |

## 计算规则

### 1. 单条到时的发震时刻估计

对候选 `c`，仅当存在匹配 `(arrival.station, arrival.phase)` 的旅行时 `tt` 时可评估：

```text
arrival_origin_estimate = arrival.time - tt
```

### 2. 台站发震时刻

同一台站的所有可评估到时估计取中位数。偶数个值时，中位数是中间两个值的算术平均。
重复到时逐条参与该台站中位数，但台站最终只占一个等权席位。

### 3. 候选发震时刻

所有至少有一条可评估到时的台站，其台站发震时刻再等权取中位数，得到
`origin_time`。没有任何可评估台站的候选为 `evaluable=false`。

### 4. 残差与支持台站

候选发震时刻确定后：

```text
arrival_residual = abs(arrival.time - (origin_time + predicted_travel_time))
station_residual = median(该站所有可评估到时的 arrival_residual)
```

- `station_residual <= residual_threshold` 时，该站是支持站；恰好等于阈值也算支持。
- 否则该站是离群站。
- 没有任何可评估到时的台站是未评估站，不参与发震时刻、残差或支持数。
- 候选的 `support_residual_median` 是所有支持站的 `station_residual` 的中位数；
  只在支持站集合上计算。

增加同一台站的到时条数可能按定义改变该站中位数，但永远不能把一个台站变成多个
支持台站；`support_count` 是支持台站 ID 的数量。

### 5. 候选排序

可评估候选按以下键升序排序：

1. 支持台站数降序；
2. 支持站残差中位数升序；
3. `candidate_id` 字典序升序。

无支持站的候选因支持数为 0，自然排在有支持站者之后；支持数同为 0 时按
`candidate_id` 排序。完全不可评估的候选追加在可评估候选之后，并按
`candidate_id` 排序。输入候选顺序和到时顺序都不影响输出。

排序第一的可评估候选是唯一 `best_candidate`。不可评估事件的
`best_candidate` 为 `null`，但仍返回所有候选诊断。

## 事件状态与准入

| `status` | `usable` | 条件 |
| --- | --- | --- |
| `usable` | `true` | 最佳候选支持站数 `>= min_supporting_stations`，且没有同支持数、残差中位数差 `<= ambiguity_margin` 的其他候选 |
| `ambiguous` | `false` | 最佳候选达到最少支持站数，但存在上述歧义候选 |
| `insufficient_evidence` | `false` | 最佳候选支持站数低于配置下限；即使存在低支持数并列，也优先归类为证据不足 |
| `unresolved` | `false` | 没有任何候选能评估该事件的任一台站/相位 |

歧义判断使用：

```text
other.support_count == best.support_count
abs(other.support_residual_median - best.support_residual_median) <= ambiguity_margin
```

歧义候选不会因 `candidate_id` 字典序靠前而变成唯一可用位置；字典序只决定诊断
排序和 `best_candidate`。

> 数学一致性说明：如果两个候选的所有旅行时只相差同一个常数，自由发震时刻会吸收
> 该常数，使两者残差完全相同。验收 1 的 `c1=(2,4,6)` 与 `c2=(20,22,24)` 正是
> 这种整体平移等价拟合；因此验收 1 仅给定阈值和最少支持站数，结论为 `usable`。
> 若对这组候选显式配置任何非负 `ambiguity_margin`（包括 `0`），它们会按歧义规则
> 成为 `ambiguous`。验收 3 的边界测试使用独立的 `c1/c3` 集合。

## 输出结构

顶层：

```json
{
  "schema": "seismic-assoc.location-evaluation/v1",
  "parameters": {
    "residual_threshold": 0.1,
    "min_supporting_stations": 3,
    "ambiguity_margin": 0.05,
    "ambiguity_check_enabled": true
  },
  "events": [],
  "summary": {
    "events": 4,
    "usable": 1,
    "ambiguous": 1,
    "insufficient_evidence": 1,
    "unresolved": 1,
    "usable_for_downstream": 1
  }
}
```

同时提供 `results`/`event_results`/`decisions` 作为指向同一事件列表的兼容键。

每个事件的稳定主字段：

| 字段 | 含义 |
| --- | --- |
| `event_id` / `id` | 原 process 事件 ID |
| `status` / `usability_status` / `decision` | `usable` / `ambiguous` / `insufficient_evidence` / `unresolved` |
| `usable` / `is_usable` | 是否允许进入下游定位目录 |
| `best_candidate` | 排序第一的可评估候选诊断，或 `null` |
| `best_candidate_id` | 最佳候选 ID，或 `null` |
| `candidates` / `candidate_diagnostics` | 排序后的全部候选诊断 |
| `supporting_stations` | 最佳候选的支持站 ID（字典序） |
| `outlier_stations` | 最佳候选的离群站 ID（字典序） |
| `unevaluated_stations` | 最佳候选下无可评估到时的台站 ID（字典序） |
| `station_evidence` / `station_reasons` | 逐台站数值与原因码 |
| `ambiguous_candidates` | 歧义候选对象、差值、margin 和原因 |
| `ambiguous_candidate_ids` | 稳定排序的歧义候选 ID |
| `reason_code` / `reason` / `message` / `reasons` | 主结论与结构化原因 |

候选诊断包含：

- `candidate_id`、`lat`、`lon`（另附 `id`、`latitude`、`longitude` 兼容键）；
- `evaluable`、`origin_time`、`rank`、`is_best`；
- `support_count`、`supporting_station_count`；
- `supporting_stations`、`outlier_stations`、`unevaluated_stations`；
- `support_residual_median`；
- `station_evidence`（别名 `stations`）；
- `arrival_evidence`（别名 `arrivals`、`evidence`）；
- 分类对象证据 `supporting_station_evidence`、
  `outlier_station_evidence`、`unevaluated_station_evidence`。

每条 `arrival_evidence` 都包含原 `id`、`station`、`phase`、`time`，以及：

- 可评估：`evaluable=true`、`predicted_travel_time`、`origin_estimate`、
  `residual`、`within_residual_threshold`；
- 不可评估：`evaluable=false`、`residual=null`、
  `reason_code="missing_travel_time"`。

台站原因码：

| `reason_code` | 含义 |
| --- | --- |
| `station_residual_within_threshold` | 台站残差中位数不大于阈值 |
| `station_residual_above_threshold` | 台站残差中位数超过阈值 |
| `no_evaluable_arrivals` | 该站在此候选下没有可评估到时 |
| `missing_travel_time` | 单条到时缺少匹配台站/相位旅行时 |

事件主原因码：

| 状态 | 主原因码 |
| --- | --- |
| `usable` | `minimum_support_met_without_ambiguity` |
| `ambiguous` | `ambiguous_candidate_within_margin` |
| `insufficient_evidence` | `best_candidate_below_minimum_support` |
| `unresolved` | `no_evaluable_candidate` |

## 验收示例

下面的最小例子对应验收 1：

```python
from seismic_assoc import process, evaluate_location_candidates

process_result = process({
    "stations": [
        {"id": "A", "lat": "0", "lon": "0"},
        {"id": "B", "lat": "0", "lon": "1"},
        {"id": "C", "lat": "1", "lon": "0"},
    ],
    "events": [
        {"id": "a", "station": "A", "phase": "P", "time": "102"},
        {"id": "b", "station": "B", "phase": "P", "time": "104"},
        {"id": "c", "station": "C", "phase": "P", "time": "106"},
    ],
    "window": "100",
})

report = evaluate_location_candidates(
    process_result,
    {"A": (0, 0), "B": (0, 1), "C": (1, 0)},
    [
        {"candidate_id": "c1", "lat": 0, "lon": 0,
         "travel_times": {"A": {"P": 2}, "B": {"P": 4}, "C": {"P": 6}}},
        {"candidate_id": "c2", "lat": 1, "lon": 1,
         "travel_times": {"A": {"P": 20}, "B": {"P": 22}, "C": {"P": 24}}},
    ],
    {"residual_threshold": 0.1, "min_supporting_stations": 3},
)

event = report["events"][0]
assert event["status"] == "usable"
assert event["best_candidate"]["origin_time"] == 100
```

验收测试见 `tests/test_location_evaluation.py`，覆盖：

1. anchor 无关与错误 anchor 残差；
2. 同站多条/重复到时、逐到时残差和支持站数不按条数增加；
3. ambiguity margin 内、边界相等、超出边界和中位数并列排序；
4. 缺相位旅行时、全候选 unresolved、偶数中位数、阈值边界；
5. 候选/到时顺序打乱不变、JSON 序列化、输入不改写；
6. 重复候选 ID/到时 ID、未知站、非法数值/坐标/质量参数的整体拒绝；
7. 同一调用中 `usable`、`ambiguous`、`insufficient_evidence`、`unresolved`
   四类事件互不污染。
