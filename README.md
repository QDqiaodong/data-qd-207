# data-qd-207 · 地震到时事件归并与波形复核目录

把多台站P/S到时按显式时间窗归并为事件，并保留台站与相位证据；不定位震源。
另提供**可重开的本地波形复核目录**：用版本化台站校时生成事件与取样计划，
登记中心归档回执并安全决定原始波形块下线；目录只登记数据引用，不传输、
解码或删除波形文件。

本仓主要交付是Python业务API和可供下游使用的结构化数据，不是通用CLI、报表或CRUD模板。字段、身份、单位、上限与失败边界见 [数据契约](docs/contract.md)；复核目录的输入、来源查询、幂等重放与崩溃重开语义见 [复核目录契约](docs/review-contract.md)。训练/盲测数据集发布账本、波形引用清单与重开幂等契约见 [数据集发布账本契约](docs/dataset-release.md)。跨校时版本的稳定事件目录身份 `ceN`、全局继承选择与谱系 API 见 [事件目录身份契约](docs/event-catalog.md)。样本级特征血缘、副本/物理两类缺陷传播与交付凭证见 [特征血缘与交付凭证契约](docs/feature-lineage.md)。

## 安装与验证

Python >=3.11，实测CPython3.12.14、uv0.12.19。运行依赖仅标准库，构建固定setuptools80.9.0、wheel0.45.1；每仓独立 `.venv`。

```sh
uv venv --offline --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
.venv/bin/python -I examples/run_sample.py
.venv/bin/python -I examples/run_review.py
.venv/bin/python -I examples/run_event_catalog.py
```

无 venv 的环境可用系统 Python 3.11 直接跑（仅标准库）：

```sh
python3 -I -c "import sys,unittest; sys.path.insert(0,'src'); \
 r=unittest.TextTestRunner().run(unittest.defaultTestLoader.discover('tests')); \
 sys.exit(0 if r.wasSuccessful() else 1)"
```

上述命令已在本地缓存和独立环境真实执行；样例输出写入 `run/sample.json`，已有目标拒绝覆盖。API可这样调用：

```python
from seismic_assoc import process
from seismic_assoc.io import load, publish
publish("run/another.json", process(load("fixtures/input.json")))
```

复核目录的最小调用形态（完整合成示例见 `examples/run_review.py`）：

```python
from seismic_assoc import create_review, open_review

d = create_review("local/review", data, first_clock)   # data 口径见复核契约
d.submit_clock(new_clock)                              # 追加校时版本
d.register_receipt({"receipt_id": "r1", "version_id": "v1",
                    "event_id": "e1", "block_id": "X", "interval": [9, 12]})
d.request_offline({"request_id": "q1", "block_id": "Y"})
plan = d.read_plan()            # 固定证据与所需本地采样区间（半开整数区间）
sources = d.query_sources()     # archive/online/missing 逐片段来源
d.close()

d2 = open_review("local/review")  # 另一位值班员重开，状态由提交日志重建

# 训练/盲测发布账本：先预览不可拆组，再按发布请求 ID 原子发布已归档组。
preview = d2.preview_release("v1", ["e1", "e2"], blind_event_ids=["e2"])
group_key = preview["groups"][0]["group_key"]
manifest = d2.publish_release(preview, [group_key], "release-1")
saved = d2.read_release("release-1")  # 重开/重试均返回首次固定清单
```

完整发布分组、历史用途冲突、归档缺口、清单引用和崩溃语义见
[数据集发布账本契约](docs/dataset-release.md)。

跨校时版本为事件分配稳定 `ceN` 身份并保留拆分/合并谱系（只读，不改目录）：

```python
from seismic_assoc import reconcile_event_catalog

catalog = reconcile_event_catalog(None, d)        # 从首个校时版建立 ce1、ce2…
d.submit_clock(new_clock)
catalog = reconcile_event_catalog(catalog, d)     # 紧接处理下一校时版
# catalog["lineage"] 含全部正权重关联、共享到时 ID 与是否继承；
# 状态是 JSON 可序列化 dict，可直接作为下一次调用的 previous_catalog。
```

完整规则、状态模式与合成示例见 [事件目录身份契约](docs/event-catalog.md) 与
`examples/run_event_catalog.py`。

样例摘要：

```json
{"arrivals": 3, "events": 2, "multi_station": 1}
```

现有 66 项测试全部通过（含 7 项旧入口回归、复核目录验收/边界/崩溃重开
用例、8 项数据集发布账本验收/API 兼容用例，以及事件目录身份的规范键分配、
全局选择、谱系、别名兼容、随机分区不变量与拒绝路径用例），并核对非法输入拒绝；
样例数据全部原创合成，见 [来源](fixtures/SOURCES.md)。

## 边界

业务结果仅覆盖契约中定义的有限模型。后续需求可沿已有身份和证据字段扩展，不把当前样例当成行业标准或生产认证。
