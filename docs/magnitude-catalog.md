# 局部震级、版本更正与下游清单增量契约

本功能在现有 `process` 归并结果和 `evaluate_location_candidates` 位置诊断之上
计算局部震级；不读取波形、不重新定位、不访问网络。纯计算入口为：

```python
from seismic_assoc import estimate_local_magnitudes

result = estimate_local_magnitudes(
    process_result,
    location_report,
    station_coordinates,
    amplitude_observations,
    instrument_epochs,
    distance_corrections,
    min_stations=3,
)
```

目录和下游接续入口为：

```python
from seismic_assoc import (
    create_magnitude_catalog,
    open_magnitude_catalog,
    apply_manifest_delta,
)
```

## 1. 输入契约

### `process_result` 与 `location_report`

- `process_result` 必须是现有 `process(data)` 的结果形状。消费
  `events[*].event_id`（兼容 `id`）以及 `arrivals[*].id/station/phase`。
- 到时 ID 必须在整批中唯一；事件 ID 也必须唯一。
- `location_report` 必须是 `evaluate_location_candidates(...)` 的结果形状，
  也兼容直接传入事件列表。必须与本批事件集合完全一致：缺失、多余或重复均为
  `ValueError`。
- 只消费：
  - `event_id`
  - `status`：`usable` / `ambiguous` / `insufficient_evidence` / `unresolved`
  - 当且仅当 `status == "usable"` 时消费
    `best_candidate.lat` / `best_candidate.lon`（也接受
    `latitude`、`longitude/lng` 别名），坐标必须有限且在合法经纬度范围内。
- 非 `usable` 状态下即使 `best_candidate` 为 `null` 或不含合法坐标也不报错；
  该首选位置不参与距离计算。

### `station_coordinates`

采用位置 API 的坐标格式：

```python
{"A": {"lat": 0.0, "lon": 1.0}}
# 或 [{"id": "A", "lat": 0.0, "lon": 1.0}]
# 或 {"A": (0.0, 1.0)}
```

接受数字、数字字符串和 `Decimal/Fraction` 坐标。所有到时台站都必须有坐标；
仪器有效期中的台站也必须在坐标表中。

距离使用 Haversine 大圆公式，地球半径固定为 `6371.0088 km`，不使用归并
`anchor`。公开辅助函数为 `great_circle_distance_km(lat1, lon1, lat2, lon2)`。

### `amplitude_observations`

每条记录必须且只需包含：

| 字段 | 契约 |
| --- | --- |
| `measurement_id` | 整批唯一的非空 ID |
| `arrival_id` | 必须引用本批存在的到时 |
| `station` | 必须与被引用到时的台站一致 |
| `channel` | 非空通道标识 |
| `sample_start` / `sample_end` | 非负整数，拒绝布尔值；`[start, end)` 必须非空 |
| `peak_to_peak_counts` | 正的有限数；拒绝布尔值、`NaN`、无穷和非正数 |

多个测量可引用同一到时；同台站多通道/重复观测都参与该台站中位数，但该台站
最终只投一票。

### `instrument_epochs`

每条记录必须且只需包含 `epoch_id`、`station`、`channel`、
`sample_start`、`sample_end`、`counts_per_mm`：

- `epoch_id` 唯一；
- 采样区间规则与振幅区间相同；
- `counts_per_mm` 为正的有限数；
- 同一 `(station, channel)` 的有效期不得重叠。半开区间端点相接合法，例如
  `[0, 10)` 与 `[10, 20)` 不重叠。

校准规则：

1. 只有一个同站、同通道有效期满足
   `epoch.sample_start <= obs.sample_start` 且
   `obs.sample_end <= epoch.sample_end` 时才校准；
2. 完全没有非空交集：观测状态为 `no_calibration`；
3. 有非空交集但没有任何单段完全覆盖：状态为 `calibration_boundary`。
   横跨相邻两段但每段都不能单独覆盖整个峰峰值区间时属于此类；不能按中点选
   增益，也不能拆分一个峰峰值；
4. 校准振幅（毫米）为
   `peak_to_peak_counts / counts_per_mm`。

### `distance_corrections` 与 `min_stations`

- 至少两个点：`{"distance_km": ..., "correction": ...}`。
- 距离必须有限、非负且按数值唯一；修正值必须有限。
- 点顺序任意；内部按距离排序后作分段线性插值。距离域为闭区间
  `[min_distance, max_distance]`，包含两端，不向外推。
- `min_stations` 必须是整数且 `>= 2`；布尔值（包括 `True/False`）拒绝。

## 2. 震级计算与输出

对校准成功、位置可用且距离在修正表内的 S 相观测：

```text
single_magnitude = log10(amplitude_mm) + distance_correction
```

先对同一事件、同一台站的全部有效单条震级取中位数，再对各有效台站中位数取
中位数。偶数个值时取中间两值的算术平均。

任何无法表示为有限 float 的振幅、修正值、单条震级或中位数都以 `ValueError`
拒绝。缺校准、位置不可用或有效台站不足是业务状态，不是输入错误。

顶层结果为严格 JSON 数据：

```json
{
  "schema": "seismic-assoc.local-magnitudes/v1",
  "min_stations": 3,
  "events": []
}
```

`events` 按 `process_result.events` 的顺序输出；观测按 `measurement_id`
稳定排序，台站按 station ID 排序。`results` / `event_results` 是同一事件
列表的兼容别名。

### 事件字段

| 字段 | 说明 |
| --- | --- |
| `event_id` | 本批事件 ID |
| `location_status` | 位置诊断的四种原状态之一 |
| `magnitude_status` | `usable` / `location_not_usable` / `insufficient_amplitude_stations` |
| `status` | 等于 `magnitude_status`，便于下游直接读取 |
| `usable` | 震级状态是否为 `usable` |
| `magnitude` | 仅 `magnitude_status == "usable"` 时为未舍入数值，否则 `null` |
| `stations` / `station_magnitudes` | 有效台站中位数及参与观测 ID |
| `station_count` / `valid_station_count` | 有效台站票数 |
| `station_medians` | `{station: median}` 映射 |
| `observations` / `amplitude_observations` | 逐观测复核依据 |
| `location` | usable 时为消费的 `{lat, lon}`，否则 `null` |

台站记录的规范字段为：

```json
{
  "station": "A",
  "median_magnitude": 1.0,
  "magnitude": 1.0,
  "observation_ids": ["mA1", "mA2"],
  "measurement_ids": ["mA1", "mA2"],
  "participating_observation_ids": ["mA1", "mA2"]
}
```

### 观测状态与依据

观测记录保留 `measurement_id`（同时给 `id` 别名）、`arrival_id`、
`station`、`channel`、`phase`、`sample_start/sample_end`、
`sample_interval: [start, end]` 和原始 `peak_to_peak_counts`。

状态优先级如下：

| 观测 `status` | 条件 | 校准/距离/震级字段 |
| --- | --- | --- |
| `unsupported_phase` | 非 S 相（现有契约下即 P） | 不校准，校准和震级字段为 `null` |
| `no_calibration` | S 相但与任何同站同通道有效期无交集 | 无增益 |
| `calibration_boundary` | S 相有交集但无单段完全覆盖 | 无增益 |
| `location_not_usable` | 已成功校准，但事件位置非 `usable` | 保留 `epoch_id/counts_per_mm/gain/amplitude_mm`；距离、修正、震级为 `null` |
| `distance_out_of_range` | 位置可用，但距离不在修正表闭区间内 | 保留校准依据和实际 `distance_km`；修正、单条震级为 `null` |
| `usable` | 已校准、位置可用、距离可插值且单条震级有限 | 保留全部字段及 `single_magnitude` |

`calibration_status` 保留校准子状态；成功校准为 `calibrated`。
`calibration` 和 `distance` 是对应扁平字段的嵌套只读视图。
`distance_correction` 与别名 `correction` 同值，`single_magnitude` 与别名
`magnitude` 同值。

所有输入错误均抛 `ValueError`；规范异常子类为 `MagnitudeInputError`。

## 3. 创建和重开本地目录

规范关键字形态：

```python
from seismic_assoc import create_magnitude_catalog, open_magnitude_catalog

catalog = create_magnitude_catalog(
    "local/magnitude-catalog",
    process_result=process_result,
    location_report=location_report_v1,
    station_coordinates=station_coordinates,
    amplitude_observations=amplitude_observations,
    instrument_epochs=instrument_epochs_v1,
    distance_corrections=distance_corrections,
    min_stations=3,
    magnitude_threshold=3.0,
    first_version_id="v1",
)
catalog.close()

catalog = open_magnitude_catalog("local/magnitude-catalog")
```

也兼容现有目录 API 风格的
`create_magnitude_catalog(path, config_dict, first_version_dict)`。

建档后固定：

- 原始 `process_result` 的 JSON 副本（调用方对象不被修改）；
- 规范化台站坐标、振幅观测、距离修正表；
- `min_stations` 和有限的 `magnitude_threshold`；
- 随机生成且写入 `catalog.json` 的 `catalog_id`。

首版必须提供 `instrument_epochs`、`location_report` 和 `first_version_id`。
目录不跨批重新关联事件。

单写者由目录内 `LOCK` 的非阻塞 `flock` 保证；`close()` 释放锁，之后可重开。
类和函数别名包括 `MagnitudeCatalog`、`MagnitudeDirectory`、
`create/open_magnitude_directory`。

## 4. 提交更正与读取历史依据

```python
catalog.submit_correction(
    "v2",                         # 新版本 ID
    "v1",                         # 预期上一版本 ID
    instrument_epochs=new_epochs, # 可选：完整替代表
    location_report=new_location, # 可选：完整替代表
)
```

- 两份表至少明确提供一份；未提供的一份从预期上一版继承。
- 提供的表是**完整替代**，不是 patch。
- 两份表同时提供时，先合并，再只对最终输入运行同一个
  `estimate_local_magnitudes(...)` 契约。
- 新版本 ID 不存在、预期前版等于当前版且整批输入有效时才提交。
- `read_version(version_id=None)` 返回固定批次、该版两张表、完整震级结果、
  该版清单和游标；省略版本 ID 时读当前版。
- `read_magnitudes(version_id=None)` 只返回震级计算结果。
- 旧版本记录不可变；新更正不会回写旧版增益、振幅或震级。

### 幂等请求身份

成功的新版本 ID 按以下规范指纹判同：

```text
expected_previous_version_id
+ 明确提供了哪几张替代表
+ instrument_epochs 的规范内容（若提供）
+ location_report 中本功能消费字段的规范内容（若提供）
```

- 仪器表和位置事件的排列顺序不影响等价性。
- `location_report` 只比较 `event_id/status/best_candidate.lat/lon`
  （usable 时），其余候选诊断、摘要和展示字段不参与。
- 相同新版本 ID + 相同指纹的重试返回首次的目标版 ID 和当时变更包；即使目录
  已有后续版本，也不会重新按当前状态校验、重算包或回退当前版本。
- 相同版本 ID 但预期前版、提供表集合或规范内容不同，抛
  `MagnitudeVersionConflictError`（`ValueError` 子类）。

## 5. 清单、游标和变更包

全量清单只包含震级状态为 `usable` 且**未舍入**的
`magnitude >= magnitude_threshold` 的事件，按 `event_id` 排序：

```json
[
  {"event_id": "e1", "magnitude": 3.0},
  {"event_id": "e2", "magnitude": 4.0}
]
```

接入当前版或指定历史版：

```python
# 一个操作同时取清单和绑定游标
manifest, cursor = catalog.access_manifest("v1")

# 或者分别读取
manifest = catalog.get_manifest("v1")  # list 子类，另带 manifest.cursor
cursor = catalog.get_cursor("v1")
```

游标形状：

```json
{
  "schema": "seismic-assoc.manifest-cursor/v1",
  "catalog_id": "magnitude-catalog-...",
  "version_id": "v1"
}
```

每笔成功更正（不含首版）都可用目标版本 ID 读取确定的变更包：

```python
package = catalog.read_change_package("v2")
# get_change_package/find_change_package/change_package 是不抛异常查询：
# 未知版本或首版返回 None。
```

规范字段：

```json
{
  "schema": "seismic-assoc.magnitude-change-package/v1",
  "catalog_id": "magnitude-catalog-...",
  "previous_version_id": "v1",
  "target_version_id": "v2",
  "version_id": "v2",
  "upserts": [
    {"event_id": "e1", "magnitude": 3.5}
  ],
  "deletions": ["e9"],
  "cursor": {"catalog_id": "...", "version_id": "v2"}
}
```

实际返回还带 `from/to_version_id`、`upserted/updates`、
`deletes/deleted_event_ids/removed` 等只读兼容别名。

差分规则：

- 新版达到阈值而旧版不在清单：`upserts` 发目标版整条
  `{event_id, magnitude}`；
- 两版都在清单但震级数值不同：`upserts` 发新整条，即替换原行；
- 旧版在、新版不在（包括震级变为 `location_not_usable` 或
  `insufficient_amplitude_stations`）：`deletions` 只发 event ID；
- 复核依据变化但清单行不变：不进行 upsert/delete；
- `upserts` 和 `deletions` 分别按 event ID 排序，同一事件不会同时出现；
- 两类操作都为空时仍生成包并推进版本，下游必须应用新游标，不能跳过该版。

首版是接入基线，不存在 `previous_version_id == null` 的首版上一版变更包；
`get_change_package("v1")` 返回 `None`，严格入口
`read_change_package("v1")` 抛 `UnknownChangePackageError`。

## 6. 下游独立应用增量

```python
from seismic_assoc import apply_manifest_delta

new_manifest, new_cursor = apply_manifest_delta(
    manifest, cursor, package
)
```

该函数不打开目录、不访问网络或全局状态，也不修改 `manifest`、`cursor` 或
`package`。返回值是 `(new_manifest, new_cursor)` 元组，同时支持
`result.manifest` / `result.cursor` 和 `result["manifest"]` 访问。

接受条件：

1. `package.catalog_id == cursor.catalog_id`；
2. `package.previous_version_id == cursor.version_id`。

因此：

- 乱序包（游标的版本不是包的前版）拒绝；
- 已消费包（游标已经到达包目标版）拒绝；
- 跨目录包拒绝；
- 包内同一事件同时 upsert/delete、操作结构非法时拒绝。

接受后会先删除全部 deletion IDs，再完整写入 upsert 行，最后按 event ID
排序。只更新游标而漏删、或只添加不删除都不符合契约。

### 用当前游标接续

区域选取程序首次接入时读取当前版：

```python
manifest, cursor = catalog.access_manifest()  # 默认当前版
# 保存 manifest 和 JSON 可序列化的 cursor

package = catalog.get_change_package("vNext")
manifest, cursor = apply_manifest_delta(manifest, cursor, package)
# 持久化新 manifest/cursor 后，再处理下一版
```

### 从历史版本接续

也可以从任意历史版本接入：

```python
manifest, cursor = catalog.access_manifest("v2")
for version_id in ("v3", "v4", "v5"):
    package = catalog.get_change_package(version_id)  # v3 的前版是 v2
    manifest, cursor = apply_manifest_delta(manifest, cursor, package)
```

由任一历史版本接入，并顺次应用后续所有包，所得清单和游标必须与
`catalog.get_manifest(target_version)` /
`catalog.get_cursor(target_version)` 完全一致。目录不提供历版观测下载；
下游只需保存自己的清单和游标。漏掉一个删除包不能靠之后的全量清单纠正，
因此必须按版本顺序应用每个包。

## 7. 持久化、崩溃时机与自动化测试

目录文件：

- `catalog.json`：目录身份、固定批次和首版完整记录，原子替换写入；
- `journal.jsonl`：每笔更正先写一行 JSON，再写一行 `COMMITTED`；
- `LOCK`：单写者文件锁。

重开时只重放 JSON 行后紧跟 `COMMITTED` 的记录；最后一笔未提交 JSON/半行
会被截断。完整版本依据、当前指针推进和变更包位于同一条提交记录中，因此
它们一起生效。

自动化测试 `tests/test_magnitude_catalog.py` 使用与现有 review 目录相同风格
的两个故障注入点，无需真的杀死测试进程：

```python
catalog._fault = "crash_before_commit"
catalog.submit_correction("v2", "v1", ...)
# JSON 已写，但 COMMITTED 未写；模拟生效前中断。

catalog._fault = "crash_after_commit"
catalog.submit_correction("v2", "v1", ...)
# COMMITTED 已 fsync、状态已生效，但 return 前抛 RuntimeError；
# 模拟应答未送达。
```

- `crash_before_commit`：测试关闭句柄并重开，断言当前版仍为 `v1`、没有
  `v2` 历史/变更包，且日志中的未提交尾巴已恢复截断；之后失败的 `v2` ID
  仍可首次成功使用。
- `crash_after_commit`：测试重开后先成功提交 `v3`，再用乱序但规范等价的
  `v2` 表和原预期前版 `v1` 重试 `v2`；返回的必须是首次删除包，当前版仍为
  `v3`，不能按最新清单重算。

运行新增测试：

```sh
python3 -I -c "import sys,unittest; sys.path.insert(0,'src'); \
 r=unittest.TextTestRunner(verbosity=2).run( \
 unittest.defaultTestLoader.discover('tests', pattern='test_*magnitude*.py')); \
 sys.exit(0 if r.wasSuccessful() else 1)"
```

完整回归：

```sh
python3 -I -c "import sys,unittest; sys.path.insert(0,'src'); \
 r=unittest.TextTestRunner().run(unittest.defaultTestLoader.discover('tests')); \
 sys.exit(0 if r.wasSuccessful() else 1)"
```
