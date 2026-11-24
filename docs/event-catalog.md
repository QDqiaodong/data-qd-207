# 校时版本间稳定事件目录身份 API

在 [复核目录契约](review-contract.md) 之上提供的**只读**身份层。校时版本里的
局部 `e1/e2/…` 只在该版本内有效；本 API 用跨版本稳定的到时 ID 作为证据身份，
为每个事件分配终身不变的目录身份 `ce1/ce2/…`，并保留身份随到时成员拆分、合并、
重分组的完整谱系。

- 入口：`reconcile_event_catalog(previous_catalog, review_directory, version_id=None)`
- 模块：`seismic_assoc.event_catalog`（也可从 `seismic_assoc` 顶层导入；
  `seismic_assoc.review.reconcile_event_catalog`、`seismic_assoc.catalog`、
  `seismic_assoc.reconcile` 均为同一函数的导入别名）
- 纯计算：只调用目录的 `list_versions()` 与（恰好一次）`read_plan(version)`，
  **不**写复核目录、校时版本、计划、归档回执或下线状态，**不**关闭调用方句柄，
  也不在磁盘建立身份数据库。身份状态就是返回的 JSON 可序列化普通 `dict`。

## 调用顺序

```python
from seismic_assoc import create_review, reconcile_event_catalog

d = create_review("local/review", data, first_clock)

catalog = reconcile_event_catalog(None, d)          # 从 list_versions()[0] 起
d.submit_clock(next_clock)
catalog = reconcile_event_catalog(catalog, d)       # 紧接处理下一版
```

- 首次 `previous_catalog=None`：必须从 `list_versions()` 的第一个版本开始；
  显式传 `version_id` 时也必须等于该首版本。
- 后续调用：`version_id` 缺省取 `list_versions()` 中紧跟
  `catalog["last_version"]` 的下一版本；显式给出时必须正是该版本。
  重复处理已处理版本、跳版、倒退、处理不存在的版本都抛 `ValueError`
  （具体类型 `seismic_assoc.event_catalog.CatalogStateError`）。
- 状态带目录绑定（真实目录的绝对路径 + 不可变 `catalog.json` 的 SHA-256）。
  把甲目录的状态接到乙目录会被拒绝；同一路径关闭后用 `open_review` 重开可以续接。
- 拒绝时不改动复核目录，也不原地修改调用方传入的 `previous_catalog`
  （成功时同样返回新对象）。

## 规范键与首次分配

一个计划事件的**规范键**是其全部到时 ID 的升序列表。到时 ID 是同一复核目录
跨版本不变的证据身份；计划事件的 `arrivals[*].id` 即该版本事件成员的完整集合。

首次建状态时，把计划事件按规范键的字典序排序，依次分配 `ce1、ce2、…`。
局部事件编号、计划数组顺序、anchor、台站列表、到时列表的输入顺序都**不**参与
身份确定；打乱计划事件数组或事件内到时列表会得到完全相同的结果。

## 跨版本全局继承选择

对相邻两版，旧活动身份 O 与新计划事件 N 仅当成员到时集合相交时存在候选关联，
权重 `w(O,N)=|O∩N|`。继承必须在整批候选关联上做**一对一全局匹配**，依次优化：

1. 最大化被选关联的权重总和（共享到时 ID 总数）；
2. 总和相同时，最大化被继承的旧身份数量（= 被选关联数）；
3. 仍相同时，把每条关联表示为 `(旧编目身份 ID, 新事件规范键)`，按旧 ID 再按
   规范键排序，取所得关联列表字典序最小的方案。

实现把三级目标编码成一个大整数边权
`w·(K+1)·2^M + 2^M + 2^(M-1-rank)`（`M` 为候选边数、`K=min(旧事件数,新事件数)`、
`rank` 为该边在第三优先级排序中的位次），再用最小费用流求一次二分图最大权匹配，
因此同输入必得唯一确定的最优解。纯函数
`solve_global_associations(old_ids, new_keys, candidates)` 可单独用于合成裁决验证。

只有被选中的关联允许旧 `ceN` 继承给新事件：

- 未继承旧身份的新事件，按规范键排序后依次从 `next_sequence` 分配新 `ceN`；
- 没有被选后继的旧身份标记为 `ended`，保留全部历史，序号永不复用；
- 同一旧 ID 不会成为两个新事件的活动身份，一个新事件也不会同时继承多个旧 ID；
- **所有**正权重关联（包括未胜出的）都进入谱系；
- 每个当前计划事件恰好对应一个活动编目身份。

> 经典反例：O1-N1=10、O1-N2=9、O2-N1=8、O2-N2=0 时，贪心先抓最大边只会得到
> O1-N1（10）；全局最优是 O1-N2 + O2-N1（17）。

## 返回状态的规范结构

下面只列规范键，它们也是 `json.dumps` 序列化后唯一落盘的键。返回对象是
`dict` 子类（仍是普通 JSON 可序列化字典），在进程内额外支持常见替代键名的
**别名解析**，例如 `transitions`→`lineage`、`edges`→`associations`、
`old_version`→`from_version`、`weight`/`shared`→`shared_count`、
`arrival_ids`/`members`→`canonical_key`（事件）或
`shared_arrival_ids`（边，按上下文消歧）、`active_events`/`catalog_events`
→`events`、`catalog_event_id`→`catalog_id` 等；对别名的直接赋值/删除会重定向到
规范键，与另一套键名的普通参考字典做 `assertEqual` 时按“对方每个键都能在本对象
按别名匹配”的语义子集比较。经 `json.loads(json.dumps(...))` 往返后得到普通
dict，请使用下列规范键（或继续把它作为 `previous_catalog` 传回，解析器同时识别
常见外部命名）。

```jsonc
{
  "schema": "seismic_assoc.reconcile_event_catalog/v1",
  "directory": {"path": "/abs/path/to/review", "catalog_digest": "<sha256>"},
  "last_version": "v2",          // 最后处理的校时版本
  "next_sequence": 4,            // 下一个待分配序号 ce4
  "events": [                    // 当前活动身份，按 ceN 排序
    {"catalog_id": "ce1", "event_id": "e2",
     "canonical_key": ["a", "b"]} // = 该身份当前到时成员（升序规范键）
  ],
  "identities": [                // ce1..ce(next_sequence-1) 全量历史身份
    {"catalog_id": "ce1", "status": "active", // 或 "ended"
     "created_version": "v1", "ended_version": null,
     "event_id": "e2", "canonical_key": ["a", "b"]},
    {"catalog_id": "ce2", "status": "ended",
     "created_version": "v1", "ended_version": "v2", "...": "..."}
  ],
  "lineage": [ /* 每次相邻版本转换一条，见下 */ ]
}
```

### 版本转换记录

```jsonc
{
  "from_version": "v1",
  "to_version": "v2",
  "associations": [              // 全部正权重候选边，按 (旧 ceID, 新规范键) 排序
    {"old_catalog_id": "ce1",
     "new_event_id": "e2",       // 新版本内的局部计划事件 ID
     "shared_count": 2,
     "shared_arrival_ids": ["a", "b"], // 精确共享证据，升序
     "inherited": true}           // 是否为被全局选中的继承边
  ],
  "events": [                    // 每个新计划事件一条
    {"event_id": "e2", "catalog_id": "ce1",
     "canonical_key": ["a", "b"],
     "inherited_from": "ce1",    // 未继承时为 null
     "overlaps": ["ce1", "ce2"]} // 所有有正重叠的旧编目 ID（升序）
  ],
  "ended": ["ce3"],              // 本转换结束的旧身份
  "created": ["ce4"]             // 本转换新建的身份
}
```

由 `events[].overlaps` 与边的 `inherited` 标记即可识别：

- **拆分**：一个旧 ID 出现在多个新事件的 `overlaps` 中，但至多一个新事件继承它；
- **合并**：一个新事件的 `overlaps` 含多个旧 ID，但只继承其中一个；
- **先拆后并等复杂重分组**：沿 `lineage` 逐跳追踪同一 `ceN` 的活动/结束状态。

## 拒绝规则（一律 `ValueError`，目录与入参状态不变）

- `review_directory` 不提供可调用的 `list_versions`/`read_plan`，或目录已关闭；
- 指定版本无法从 `read_plan` 取得（未知版本、读取异常、计划结构非法）；
- 计划没有事件、事件缺 ID、到时列表为空、到时记录缺身份、到时 ID 在事件内或
  跨事件重复；
- `previous_catalog` 不是 `None`/对象、缺少最后版本、序号非法或会复用已有
  `ceN`、目录绑定不一致、最后版本不在目录版本序列中、版本顺序断裂、
  活动身份冲突、已结束身份仍活动、身份成员与事件不一致等结构非法状态；
- 版本不是规定的下一版本（重复、倒退、跳版、首调不从首版开始）。

外部精简状态（不带本 API 的 `schema` 标记）会被宽松归一化：允许只给
`catalog_id`+到时成员、用 `catalog_events` 映射等；但重复/冲突身份与非法类型
仍然拒绝。本 API 自己产出的严格状态执行完整不变量校验。

## 确定性与验证

同一 `previous_catalog` 与同一目录版本序列/计划输入必得逐键相同的结果；
所有列表均按编目 ID、规范键或到时 ID 排序。测试：

```sh
python3 -I -c "import sys,unittest; sys.path.insert(0,'src'); \
 r=unittest.TextTestRunner().run(unittest.defaultTestLoader.discover('tests')); \
 sys.exit(0 if r.wasSuccessful() else 1)"
python3 -I examples/run_event_catalog.py
```

`tests/test_event_catalog.py` 覆盖规范键首分配、10/9/8/0 全局矩阵、第二/第三
优先级（含 200 组随机小例与穷举最优对照）、真实目录下的拆分/合并/先拆后并、
计划与到时乱序不变性、重开续接、各类拒绝路径，以及“API 前后 `read_plan`、
`current_version`、`state.json`（回执/下线）、块状态逐字节不变”。
