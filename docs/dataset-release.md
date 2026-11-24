# 数据集发布账本契约

在[复核目录契约](review-contract.md)之上，为训练集/盲测集发布增加可重开的
**样本用途账本**。本能力只引用台站本地整数采样区间和不可变归档回执，不读取、
不复制、不解码波形，也不执行模型训练。

## API 总览

规范方法位于已打开的 `ReviewDirectory` 句柄：

```python
preview = d.preview_release({
    "version_id": "v1",
    "event_ids": ["e1", "e2", "e3", "e4"],
    "blind_event_ids": {"e3"},          # 可传 list/tuple/set/frozenset；可为空
})

manifest = d.publish_release(
    preview,                            # d 发出的预览
    ["e1,e2,e3"],                       # 批准的非空组键集合
    "release-request-1",                # 独立发布请求 ID
)

same = d.read_release("release-request-1")
maybe = d.get_release("missing")        # 未知 ID 返回 None
```

也支持 dict 请求和另一种常见位置参数顺序：

```python
d.publish_release({
    "request_id": "release-request-2",
    "preview": preview,
    "approved_group_keys": [("e4",)],
})
d.publish_release("release-request-3", preview, {"e4"})
```

包顶层另导出模块级 `preview_release(directory_or_path, ...)`、
`publish_release(directory_or_path, ...)`、`read_release(...)` 和
`get_published_manifest(...)`，以及薄封装 `ReleaseLedger` /
`DatasetReleaseLedger`。`preview_dataset_release`、`publish_dataset_release`、
`read_published_manifest` 等近义方法名也可用。

## 样本身份与不可拆组

- 校时版本必须明确给出；事件必须存在于该版 `read_plan(version_id)`。
- 候选事件集合必须非空；`blind_event_ids` 必须是候选集合的子集。
- 每个事件使用该版计划中的**完整 `ranges`**：不得删站、删段、缩短区间或只挑
  可归档片段。
- 物理样本身份只有 `(station, local_integer_tick)`。区间是半开 `[lo, hi)`：
  `lo < other_hi and other_lo < hi` 才共享样本；`[10,15)` 与 `[15,19)`
  仅端点相接，不重叠。
- 事件 ID、编目/到时 ID、统一时间、原块 ID、归档回执 ID 都不是样本身份。
- 不同台站的相同刻度互不冲突。
- 对**全部本批候选**按区间重叠做传递闭包（并查集/排序扫描，不枚举采样点）。
  即使组内有事件暂无归档，也不能先剔除它再分组。未列入本批的事件不参与分组。

预览顶层包含规范化请求：

```json
{
  "directory": "/abs/path/to/review",
  "version_id": "v1",
  "request": {
    "version_id": "v1",
    "event_ids": ["e1", "e2", "e3", "e4"],
    "blind_event_ids": ["e3"]
  },
  "groups": []
}
```

每个组的关键字段：

```json
{
  "group_key": "e1,e2,e3",
  "key": "e1,e2,e3",
  "members": ["e1", "e2", "e3"],
  "event_ids": ["e1", "e2", "e3"],
  "status": "blind",
  "usage": "blind",
  "conflict": false,
  "sample_unions": {"A": [[10, 23]]},
  "archive_gaps": [],
  "online_gaps": [],
  "missing_gaps": []
}
```

`group_key` 是成员事件 ID 的规范、确定字符串；调用者也可以直接把规范成员集合
（例如 `("e1", "e2", "e3")` 或 `frozenset(...)`）作为组键传回。组成员按事件 ID
规范排序，组顺序由该版计划顺序确定；打乱输入集合不改变预览。

## 训练、盲测与冲突

把该组实际需要的区间做并集，与本目录 journal 中所有已成功发布清单的
`sample_unions` 相交：

- 命中训练历史 → 产生训练约束；
- 命中盲测历史，或本批 `blind_event_ids` 含该组任一成员 → 产生盲测约束；
- 只有盲测约束 → 整组 `usage="blind"`；
- 只有训练约束，或没有任何约束 → 整组 `usage="train"`（无要求默认训练）；
- 训练与盲测约束同时成立 → `status="conflict"` / `conflict=true`，不得批准发布。

冲突组给出 `conflict_reasons`，可能包含：

- `history_train`：与已发布训练样本相交；
- `history_blind`：与已发布盲测样本相交；
- `required_blind_event`：本批显式要求盲测。

历史用途不会因新校时、新发布或回执变化而消失。同一物理样本允许在**同一用途**下
重复交付；因此重复发布训练/盲测用途不冲突，但会继续保留用途历史。

## 归档缺口预览

预览时调用与 `query_sources` 相同的 `_classify` 口径检查当前来源：

- `online_gaps[].kind == "online"`：原块仍在线，但尚无永久归档覆盖；
- `missing_gaps[].kind == "missing"`：块空洞，或块已下线且该段没有归档；
- 已归档片段不出现在缺口中。

缺口只是预览时提示，不占用样本，也不是发布批准的固定比较字段。预览后补归档，
仍可提交原预览和原组键；发布会重新检查当前归档。

## 发布放行规则

`publish_release(preview, group_keys, request_id)`：

1. 校验预览确由当前目录发出，版本、候选事件和选择与预览相符。
2. 批准集合必须非空；只能批准完整组，不能选择组内部分事件，也不能批准冲突组。
3. 用预览中的**完整候选集和盲测要求**，结合当前账本重新分组和计算用途。未批准组
   仍是重算输入，不能靠删除其成员改变批准组。
4. 每个批准组当前必须仍是同一组成员、同一用途且无冲突。旧预览中的训练批准若已被
   新盲测历史改钉为盲测，会按“用途已变化”拒绝，不会暗中改发盲测。未批准组单独
   变化或冲突不影响本次批准的其他组。
5. 对每个批准事件的**每条完整范围**执行当前来源检查。所有片段都必须是
   `kind="archive"`；`online` 不能兜底，任何一小段未归档都会整笔拒绝。
6. 按现有 `query_sources` 的口径选择回执：重叠回执覆盖同一子区间时取字典序最小
   回执 ID。
7. 生成清单并把完整语义请求作为一条 `kind="release"` journal 记录原子提交。

业务拒绝发生在 journal 追加之前，不写清单、不增加样本用途，发布请求 ID 不被占用，
修正后可用同一 ID 重试。

## 清单契约

成功清单是 JSON 可序列化 dict，schema 为
`seismic-assoc.release-manifest/v1`。顶层固定：

- `release_request_id` / `request_id`；
- `directory`：发出预览和发布的目录绝对路径；
- `version_id` / `version`；
- `candidate_event_ids` 与 `blind_event_ids`：预览的完整候选和盲测要求；
- `approved_group_keys` / `released_group_keys`；
- `groups[]`：每组选定用途、成员、完整事件范围、组区间并集和归档引用；
- `waveform_refs[]`：扁平化、可直接消费的取数引用。

每条 waveform reference 至少包含：

```json
{
  "release_request_id": "release-request-1",
  "group_key": "e1,e2,e3",
  "usage": "blind",
  "event_id": "e3",
  "station": "A",
  "requested_range": [18, 23],
  "interval": [18, 20],
  "kind": "archive",
  "receipt_id": "r3a",
  "reference": "r3a",
  "block_id": "A0",
  "receipt_interval": [18, 20]
}
```

同一事件范围可能因回执边界或回执选择口径拆成多条相邻引用；多个事件需要同一样本时，
会按事件重复给出引用，这是允许的同用途重复交付。

清单中的回执 ID、块 ID、区间和用途在首次成功时固定。之后补登字典序更小（优先级
更高）的回执、原块下线、提交新校时或关闭重开，都不改写已发布清单。

## 幂等、崩溃与重开

发布账本复用复核目录现有 journal/LOCK 机制和本地单写者语义：

- release 记录与其他决定一样先写 JSON 行、fsync，再写 `COMMITTED` 标记并 fsync。
- 只有 `COMMITTED` 的 release 会在 `open_review` 时重放到 `_releases` 和区间用途
  索引；未提交的最后一条记录回滚，并重开时从 journal 物理截断，避免之后追加的
  新记录使旧的未提交尾巴在再次重放时复活。
- 在提交标记前注入/发生中断：重开后 `read_release(id)` 抛
  `UnknownReleaseError`（同时是 `ValueError` 和 `KeyError` 子类），
  `get_release(id)` / `find_release(id)` 返回 `None`，且没有任何样本占用。
- 提交标记已落盘但调用方未收到应答：重开后可用语义等价的请求重试，返回首次清单；
  journal 中同一请求 ID 仍只有一条 release 记录。
- 已成功 ID 的等价重放不随当前账本重新审核或重新选择回执，直接返回原清单。
- 同一 ID 携带不同目录、版本、候选集、盲测要求、选中组成员或用途时抛
  `ReleaseRequestError`（`ValueError` 子类）。排列顺序和预览中的缺口/提示字段不
  参与等价性比较。
- 已有复核目录首次使用本能力时不需要迁移；release 历史从空开始，旧 journal 记录
  保持原行为。

## 明确不做

- 不读取、复制、解码波形字节；
- 不训练、评估或保存模型；
- 不枚举单个采样点，不把区间展开成 ID 列表；
- 不修改归并、`read_plan`、回执、下线、事件目录身份等既有记录和 API 行为；
- 不阻止后续校时或原块下线，只固定已经成功发布的引用清单。
