# data-qd-207 · 地震到时事件归并与波形复核目录

把多台站P/S到时按显式时间窗归并为事件，并保留台站与相位证据；不定位震源。
另提供**可重开的本地波形复核目录**：用版本化台站校时生成事件与取样计划，
登记中心归档回执并安全决定原始波形块下线；目录只登记数据引用，不传输、
解码或删除波形文件。

本仓主要交付是Python业务API和可供下游使用的结构化数据，不是通用CLI、报表或CRUD模板。字段、身份、单位、上限与失败边界见 [数据契约](docs/contract.md)；复核目录的输入、来源查询、幂等重放与崩溃重开语义见 [复核目录契约](docs/review-contract.md)。

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
```

样例摘要：

```json
{"arrivals": 3, "events": 2, "multi_station": 1}
```

已通过 26 项测试（含 7 项旧入口回归与 19 项复核目录验收/边界/崩溃重开用例），
并核对非法输入拒绝；样例数据全部原创合成，见 [来源](fixtures/SOURCES.md)。

## 边界

业务结果仅覆盖契约中定义的有限模型。后续需求可沿已有身份和证据字段扩展，不把当前样例当成行业标准或生产认证。
