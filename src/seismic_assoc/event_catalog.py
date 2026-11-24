"""校时版本间稳定事件目录身份（只读复核计划之上的纯函数 API）。

本模块只通过 :meth:`ReviewDirectory.list_versions` 与
:meth:`ReviewDirectory.read_plan` 读取复核目录，**绝不**改写复核目录、校时
版本、事件计划、归档回执或波形块状态，也不关闭调用方的目录句柄；身份状态
只作为 JSON 可序列化的普通数据返回，不在磁盘另建身份数据库。

规范键（canonical key）是一个计划事件全部到时 ID 的升序列表。跨版本身份
继承在全部正权重候选关联上做一对一全局选择，依次优化：

1. 被选关联共享到时 ID 总数最大；
2. 总数相同时继承的旧身份数量最大；
3. 仍相同时，关联表示为 ``(旧编目身份 ID, 新事件规范键)``，按旧 ID 再按
   规范键排序后取字典序最小的关联列表。

匹配被编码成一个大整数权值的二分图最大权匹配（最小费用流求解），第三优先级
用每个候选边独有的二进制位精确表达，因此一次求解即可得到唯一确定的最优解。
"""

import hashlib
import json
import os
from collections import Counter

from .review import ReviewDirectory

SCHEMA = 'seismic_assoc.reconcile_event_catalog/v1'

# 输出键名在不同调用方习惯下可能使用的别名；读取旧状态时按这些名字宽松识别。
_CATALOG_ID_KEYS = ('catalog_id', 'catalog_event_id', 'ce_id', 'stable_id',
                    'identity_id', 'catalogId', 'catalogEventId')
_EVENT_ID_KEYS = ('event_id', 'plan_event_id', 'local_event_id', 'id',
                  'eventId', 'planEventId')
_ARRIVAL_KEYS = ('canonical_key', 'arrival_ids', 'arrivals', 'member_ids',
                 'member_arrival_ids', 'members', 'canonicalKey', 'arrivalIds')
_LAST_VERSION_KEYS = ('last_version', 'last_version_id', 'last_processed_version',
                      'processed_version', 'version_id', 'version', 'lastVersion')
_NEXT_SEQ_KEYS = ('next_sequence', 'next_catalog_sequence', 'next_identity_sequence',
                 'next_seq', 'next_index', 'nextSequence', 'nextIndex')
_EVENTS_KEYS = ('events', 'active_events', 'current_events', 'catalog_events',
                'new_events', 'catalogEvents', 'newEvents')
_IDENTITIES_KEYS = ('identities', 'catalog_identities', 'all_identities',
                    'identity_history')
_LINEAGE_KEYS = ('lineage', 'transitions', 'version_transitions', 'history')
_BINDING_KEYS = ('directory_binding', 'directory', 'binding', 'review_directory',
                 'directoryBinding')

# 别名组：(规范名, 全部别名)。同名键在不同上下文中出现时（如 arrival_ids
# 既可能是事件成员也可能是边的共享 ID）由 AliasDict 按当前字典内容消歧。
_ALIAS_GROUPS = [
    ('catalog_id', _CATALOG_ID_KEYS),
    ('event_id', _EVENT_ID_KEYS),
    ('canonical_key', _ARRIVAL_KEYS),
    ('last_version', _LAST_VERSION_KEYS),
    ('next_sequence', _NEXT_SEQ_KEYS),
    ('events', _EVENTS_KEYS),
    ('identities', _IDENTITIES_KEYS),
    ('lineage', _LINEAGE_KEYS),
    ('directory', _BINDING_KEYS),
    ('old_catalog_id', ('old_catalog_id', 'catalog_id', 'old_catalog_event_id',
                       'old_identity_id', 'old_id', 'source_catalog_id',
                       'oldCatalogId')),
    ('new_event_id', ('new_event_id', 'event_id', 'new_plan_event_id',
                      'new_id', 'newEventId')),
    ('shared_count', ('shared_count', 'shared_arrival_count', 'shared',
                      'weight', 'count', 'sharedCount')),
    ('shared_arrival_ids', ('shared_arrival_ids', 'shared_ids', 'arrival_ids',
                            'sharedArrivalIds')),
    ('inherited', ('inherited', 'is_inherited')),
    ('from_version', ('from_version', 'old_version', 'fromVersion')),
    ('to_version', ('to_version', 'new_version', 'toVersion')),
    ('associations', ('associations', 'edges', 'candidate_associations')),
    ('overlaps', ('overlaps', 'overlap_ids', 'overlapping_ids',
                  'overlapping_catalog_ids', 'overlap_catalog_ids',
                  'old_catalog_ids')),
    ('inherited_from', ('inherited_from', 'source_catalog_id', 'inheritedFrom')),
    ('ended', ('ended', 'ended_identities')),
    ('created', ('created', 'created_identities')),
    ('path', ('path', 'directory_path')),
    ('catalog_digest', ('catalog_digest', 'digest', 'catalog_sha256')),
]
_ALIAS_TO_GROUPS = {}
for _canonical, _names in _ALIAS_GROUPS:
    for _name in _names:
        _ALIAS_TO_GROUPS.setdefault(_name, []).append(_canonical)


class CatalogStateError(ValueError):
    """目录身份状态、版本顺序或计划不满足契约。"""


def _group_for_key(key, physical_keys):
    """在键名所属的多个别名组中，按当前字典上下文选出最合理的一组。"""
    canonicals = _ALIAS_TO_GROUPS.get(key)
    if not canonicals:
        return None
    if len(canonicals) == 1:
        return canonicals[0]
    best = None
    best_score = -1
    physical = set(physical_keys)
    for canonical in canonicals:
        names = dict(_ALIAS_GROUPS)[canonical]
        score = 0
        if canonical in physical:
            score += 4
        score += sum(2 for n in names if n in physical)
        # 上下文指示：边同时有 old/new 字段，事件有 canonical_key，等等
        if canonical == 'old_catalog_id' and 'new_event_id' in physical:
            score += 3
        if canonical == 'new_event_id' and 'old_catalog_id' in physical:
            score += 3
        if canonical == 'shared_arrival_ids' and 'old_catalog_id' in physical:
            score += 3
        if canonical == 'event_id' and 'catalog_id' in physical \
                and 'old_catalog_id' not in physical:
            score += 2
        if canonical == 'canonical_key' and 'canonical_key' in physical:
            score += 3
        if score > best_score:
            best, best_score = canonical, score
    return best


class AliasDict(dict):
    """带键名别名解析与写穿透的普通 dict 子类。

    - 读取 ``d[别名]`` 可解析到同组真实键；
    - 写入/删除一个已登记别名时，会同步写入/删除规范键及当前存在的同组键，
      因此基于其他命名习惯对返回状态做的变异/删除仍会作用到规范字段；
    - 与参考形态普通 dict 做 ``==``/``assertEqual`` 时采用语义子集比较；
    - JSON 序列化只输出真实存储的键，AliasDict 本身就是 dict。
    """

    __slots__ = ()

    def __getitem__(self, key):
        resolved = self._resolve_existing(key)
        return dict.__getitem__(self, resolved)

    def _resolve_existing(self, key):
        if dict.__contains__(self, key):
            return key
        canonical = _group_for_key(key, self.keys())
        if canonical is not None:
            names = dict(_ALIAS_GROUPS)[canonical]
            for name in (canonical,) + tuple(n for n in names if n != canonical):
                if dict.__contains__(self, name):
                    return name
        return key

    def get(self, key, default=None):
        resolved = self._resolve_existing(key)
        if dict.__contains__(self, resolved):
            return dict.__getitem__(self, resolved)
        return default

    def __contains__(self, key):
        resolved = self._resolve_existing(key)
        return dict.__contains__(self, resolved)

    def __setitem__(self, key, value):
        # 对已登记别名的直接赋值重定向到规范键，避免产生互不同步的副本。
        if isinstance(key, str) and key in _ALIAS_TO_GROUPS:
            canonical = _group_for_key(key, self.keys())
            if canonical is not None:
                dict.__setitem__(self, canonical, value)
                return
        dict.__setitem__(self, key, value)

    def __delitem__(self, key):
        if not dict.__contains__(self, key) and isinstance(key, str) \
                and key in _ALIAS_TO_GROUPS:
            canonical = _group_for_key(key, self.keys())
            if canonical is not None and dict.__contains__(self, canonical):
                dict.__delitem__(self, canonical)
                return
        dict.__delitem__(self, key)

    def __eq__(self, other):
        if dict.__eq__(self, other) is True:
            return True
        if isinstance(other, dict):
            return _semantic_subset(other, self)
        return NotImplemented

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    __hash__ = None  # type: ignore[assignment]


def _semantic_value_equal(expected, actual):
    if isinstance(expected, dict):
        return _semantic_subset(expected, actual)
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            return False
        return all(_semantic_value_equal(e, a) for e, a in zip(expected, actual))
    return expected == actual


def _semantic_subset(expected, actual):
    """expected 中的每个键值都能在 actual（AliasDict 优先）中语义匹配。"""
    if not isinstance(actual, dict):
        return False
    for key, value in expected.items():
        try:
            candidate = actual.get(key) if isinstance(actual, AliasDict) \
                else actual[key]
        except (KeyError, TypeError):
            return False
        if not _semantic_value_equal(value, candidate):
            return False
    return True


def _wrap_deep(value):
    if isinstance(value, dict):
        return AliasDict({k: _wrap_deep(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_wrap_deep(v) for v in value]
    return value


def _fail(message):
    raise CatalogStateError(message)


def _pick(mapping, keys, default=None):
    for key in keys:
        if key in mapping:
            return mapping[key]
    return default


def _physical_present(mapping, names):
    if isinstance(mapping, AliasDict):
        return [n for n in names if dict.__contains__(mapping, n)]
    return [n for n in names if n in mapping]


def _json_equal(left, right):
    try:
        return json.dumps(left, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=False, default=str) == \
            json.dumps(right, sort_keys=True, separators=(',', ':'),
                       ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return left == right


def _pick_consistent(mapping, names, context, label):
    """读取同组别名；若多个别名同时存在但取值冲突，按结构非法拒绝。"""
    present = _physical_present(mapping, names)
    if not present:
        return None, False
    first = mapping[present[0]]
    for name in present[1:]:
        value = mapping[name]
        if not _json_equal(first, value):
            _fail(f'{context}: conflicting values for {label}')
    return first, True


def _pick_arrivals_consistent(mapping, names, context):
    present = _physical_present(mapping, names)
    if not present:
        _fail(f'{context}: missing arrival identities')
    normalized = None
    for name in present:
        ids = _normalize_arrival_ids(mapping[name], context)
        if normalized is None:
            normalized = ids
        elif tuple(ids) != tuple(normalized):
            _fail(f'{context}: conflicting arrival identity lists')
    return normalized



def _is_int(value):
    return type(value) is int


def _digest(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(',', ':'),
                   ensure_ascii=False).encode('utf-8')).hexdigest()


# ---------------------------------------------------------------------------
# 输入归一化：计划事件 / 旧状态
# ---------------------------------------------------------------------------

def _normalize_arrival_ids(raw, context):
    """把多种合理的到时成员表达归一化为升序、去重后的 ID 列表。

    重复身份直接拒绝；这里不静默去重，因为重复意味着结构非法或证据冲突。
    """
    if raw is None:
        _fail(f'{context}: missing arrival identities')
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        _fail(f'{context}: arrival identities must be a list')
    ids = []
    for item in raw:
        if isinstance(item, str):
            aid = item
        elif isinstance(item, dict):
            aid = _pick(item, ('id', 'arrival_id', 'arrival_ids'))
            if isinstance(aid, (list, tuple)):
                _fail(f'{context}: nested arrival identity is invalid')
        else:
            aid = None
        if not isinstance(aid, str) or not aid:
            _fail(f'{context}: arrival is missing its id')
        ids.append(aid)
    if not ids:
        _fail(f'{context}: event has no arrivals')
    if len(set(ids)) != len(ids):
        dup = next(aid for aid, n in Counter(ids).items() if n > 1)
        _fail(f'{context}: duplicate arrival identity {dup!r}')
    return sorted(ids)


def _extract_event(event, context):
    if not isinstance(event, dict):
        _fail(f'{context}: event must be an object')
    event_id = _pick(event, _EVENT_ID_KEYS)
    if not isinstance(event_id, str) or not event_id:
        _fail(f'{context}: event is missing its event_id')
    raw_ids = _pick(event, _ARRIVAL_KEYS)
    arrival_ids = _normalize_arrival_ids(raw_ids, f'{context} event {event_id!r}')
    return event_id, tuple(arrival_ids)


def normalize_plan(plan):
    """校验由 ``ReviewDirectory.read_plan`` 取得的计划，返回规范事件列表。

    返回 ``[{'event_id', 'canonical_key'}]``，顺序与计划数组一致（调用方在
    需要确定性时自行按规范键排序）。计划内任何到时 ID 重复或缺失都拒绝。
    """
    if not isinstance(plan, dict):
        _fail('plan obtained from ReviewDirectory is invalid')
    raw_events = plan.get('events')
    if not isinstance(raw_events, list) or not raw_events:
        _fail('plan has no events')
    events = []
    seen_event_ids = set()
    seen_arrivals = set()
    for index, raw_event in enumerate(raw_events):
        event_id, key = _extract_event(raw_event, f'plan event #{index}')
        if event_id in seen_event_ids:
            _fail(f'duplicate plan event id {event_id!r}')
        seen_event_ids.add(event_id)
        for aid in key:
            if aid in seen_arrivals:
                _fail(f'duplicate arrival identity {aid!r} across plan events')
            seen_arrivals.add(aid)
        events.append({'event_id': event_id, 'canonical_key': key})
    return events


def _ce_number(catalog_id):
    if isinstance(catalog_id, str) and len(catalog_id) > 2 and catalog_id.startswith('ce'):
        tail = catalog_id[2:]
        if tail.isdigit() and tail != '0' and not tail.startswith('0'):
            return int(tail)
    return None


def _normalize_state_events(raw_events):
    if isinstance(raw_events, dict):
        # 兼容 {catalog_id: event-record | [arrival ids]} 形态
        items = []
        for cid, record in raw_events.items():
            if isinstance(record, dict):
                record = dict(record)
                record.setdefault('catalog_id', cid)
            elif isinstance(record, (list, tuple)):
                record = {'catalog_id': cid, 'arrival_ids': list(record)}
            else:
                record = {'catalog_id': cid}
            items.append(record)
        raw_events = items
    if not isinstance(raw_events, list) or not raw_events:
        _fail('previous_catalog: current events are missing')
    events = []
    seen_cids = set()
    seen_eids = set()
    seen_arrivals = set()
    for index, raw in enumerate(raw_events):
        if not isinstance(raw, dict):
            _fail(f'previous_catalog event #{index} must be an object')
        ctx = f'previous_catalog event #{index}'
        cid, found = _pick_consistent(raw, _CATALOG_ID_KEYS, ctx, 'catalog_id')
        if not found or not isinstance(cid, str) or not cid:
            _fail(f'{ctx} is missing catalog_id')
        if cid in seen_cids:
            _fail(f'conflicting catalog identity {cid!r}')
        seen_cids.add(cid)
        eid, _ = _pick_consistent(raw, _EVENT_ID_KEYS, ctx, 'event_id')
        if eid is not None and (not isinstance(eid, str) or not eid):
            _fail(f'previous_catalog event {cid!r} has an invalid event_id')
        if isinstance(eid, str) and eid:
            if eid in seen_eids:
                _fail(f'conflicting plan event identity {eid!r} in previous_catalog')
            seen_eids.add(eid)
        else:
            eid = None
        ids = _pick_arrivals_consistent(raw, _ARRIVAL_KEYS,
                                        f'previous_catalog event {cid!r}')
        for aid in ids:
            if aid in seen_arrivals:
                _fail(f'previous_catalog: arrival {aid!r} appears in two active identities')
            seen_arrivals.add(aid)
        events.append({'catalog_id': cid, 'event_id': eid, 'arrival_ids': ids})
    return events


def _normalize_identities(raw_identities):
    records = {}
    if raw_identities is None:
        return records
    if isinstance(raw_identities, dict):
        iterable = []
        for cid, rec in raw_identities.items():
            rec = rec if isinstance(rec, dict) else {}
            rec = dict(rec)
            rec.setdefault('catalog_id', cid)
            iterable.append(rec)
        raw_identities = iterable
    if not isinstance(raw_identities, list):
        _fail('previous_catalog: identities must be a list')
    for raw in raw_identities:
        if not isinstance(raw, dict):
            _fail('previous_catalog: identity record must be an object')
        cid = _pick(raw, _CATALOG_ID_KEYS)
        if not isinstance(cid, str) or not cid:
            _fail('previous_catalog: identity record is missing catalog_id')
        if cid in records:
            _fail(f'previous_catalog: duplicate identity record {cid!r}')
        records[cid] = raw
    return records


def normalize_previous_catalog(previous_catalog):
    """校验调用方传入的前一份状态，抽取后续计算所需的规范字段。"""
    if not isinstance(previous_catalog, dict):
        _fail('previous_catalog must be None for the first version or a catalog state')
    strict = previous_catalog.get('schema') == SCHEMA

    last_version, found_lv = _pick_consistent(
        previous_catalog, _LAST_VERSION_KEYS, 'previous_catalog', 'last_version')
    if not found_lv or not isinstance(last_version, str) or not last_version:
        _fail('previous_catalog: last_version is missing or invalid')

    raw_events, found_events = _pick_consistent(
        previous_catalog, _EVENTS_KEYS, 'previous_catalog', 'events')
    if not found_events:
        # 精简状态可能只用 identities 列表表达当前（未结束）身份。
        raw_identity_list = _pick(previous_catalog, _IDENTITIES_KEYS)
        if isinstance(raw_identity_list, dict):
            raw_identity_list = list(raw_identity_list.values())
        if isinstance(raw_identity_list, list):
            raw_events = [
                rec for rec in raw_identity_list
                if isinstance(rec, dict)
                and _pick(rec, ('status',)) != 'ended'
                and _pick(rec, ('ended_version',)) is None]
        else:
            raw_events = None
    events = _normalize_state_events(raw_events)
    identities = _normalize_identities(_pick(previous_catalog, _IDENTITIES_KEYS))

    next_sequence, found_seq = _pick_consistent(
        previous_catalog, _NEXT_SEQ_KEYS, 'previous_catalog', 'next_sequence')
    max_number = max((n for n in (_ce_number(e['catalog_id']) for e in events)
                      if n is not None), default=0)
    max_number = max([max_number] + [
        n for n in (_ce_number(cid) for cid in identities) if n is not None])
    if next_sequence is None:
        # 本 API 产出的严格状态必须显式携带下一序号；只有外部精简状态才推断。
        if strict and not found_seq:
            _fail('previous_catalog: next_sequence is missing')
        next_sequence = max_number + 1
    if not _is_int(next_sequence) or isinstance(next_sequence, bool) or next_sequence < 1:
        _fail('previous_catalog: next_sequence must be a positive integer')
    if next_sequence <= max_number:
        _fail('previous_catalog: next_sequence would reuse an existing catalog identity')

    if strict:
        _validate_strict_state(previous_catalog, events, identities,
                               last_version, next_sequence)

    prior_lineage = _pick(previous_catalog, _LINEAGE_KEYS, [])
    if prior_lineage is None:
        prior_lineage = []
    if not isinstance(prior_lineage, list):
        _fail('previous_catalog: lineage must be a list')

    binding_raw = _pick(previous_catalog, _BINDING_KEYS)
    return {
        'strict': strict,
        'last_version': last_version,
        'next_sequence': next_sequence,
        'events': events,
        'identities': identities,
        'lineage': prior_lineage,
        'binding': _normalize_binding(binding_raw),
    }


def _validate_strict_state(raw, events, identities, last_version, next_sequence):
    """对本 API 自己产出的状态执行完整结构不变量校验。"""
    numbers = sorted(n for n in (_ce_number(e['catalog_id']) for e in events)
                     if n is not None)
    all_numbers = sorted(n for n in (_ce_number(cid) for cid in identities)
                         if n is not None)
    if not all_numbers or all_numbers != list(range(1, next_sequence)):
        _fail('previous_catalog: identities must be exactly ce1..ce(next_sequence-1)')
    if any(_ce_number(e['catalog_id']) is None for e in events):
        _fail('previous_catalog: catalog ids must use the ceN form')

    active_ids = {e['catalog_id'] for e in events}
    active_records = {cid: rec for cid, rec in identities.items()
                      if _pick(rec, ('status',)) != 'ended'
                      and _pick(rec, ('ended_version',)) is None}
    # 未显式结束的身份都必须在当前事件集合中
    for cid, rec in identities.items():
        ended_version = _pick(rec, ('ended_version',))
        status = _pick(rec, ('status',))
        is_ended = status == 'ended' or ended_version is not None
        if is_ended:
            if cid in active_ids:
                _fail(f'previous_catalog: ended identity {cid!r} cannot be active')
        else:
            if cid not in active_ids:
                _fail(f'previous_catalog: active identity {cid!r} is missing from events')
    if set(active_records) - active_ids:
        _fail('previous_catalog: identity/event active sets disagree')

    # 当前事件必须与身份记录中的最新成员一致
    by_id = {e['catalog_id']: e for e in events}
    for cid in active_ids:
        rec = identities.get(cid)
        if rec is None:
            _fail(f'previous_catalog: active identity {cid!r} has no identity record')
        key = tuple(_normalize_arrival_ids(_pick(rec, _ARRIVAL_KEYS),
                                           f'identity {cid}'))
        if key != tuple(by_id[cid]['arrival_ids']):
            _fail(f'previous_catalog: identity {cid!r} members disagree with event')
        eid = _pick(rec, _EVENT_ID_KEYS)
        if eid != by_id[cid]['event_id']:
            _fail(f'previous_catalog: identity {cid!r} event_id disagrees')

    lineage = _pick(raw, _LINEAGE_KEYS, [])
    if not isinstance(lineage, list):
        _fail('previous_catalog: lineage must be a list')
    if lineage:
        first_from = _pick(lineage[0], ('from_version', 'old_version'))
        if not isinstance(first_from, str) or not first_from:
            _fail('previous_catalog: lineage is structurally invalid')
        prev_to = first_from
        for turn in lineage:
            if not isinstance(turn, dict):
                _fail('previous_catalog: lineage transition must be an object')
            frm = _pick(turn, ('from_version', 'old_version'))
            to = _pick(turn, ('to_version', 'new_version'))
            if frm != prev_to or not isinstance(to, str) or not to:
                _fail('previous_catalog: lineage version chain is broken')
            assocs = _pick(turn, ('associations', 'edges'), [])
            if not isinstance(assocs, list):
                _fail('previous_catalog: associations must be a list')
            for edge in assocs:
                _validate_edge_shape(edge)
            prev_to = to
        if prev_to != last_version:
            _fail('previous_catalog: last_version does not match lineage end')


def _validate_edge_shape(edge):
    if not isinstance(edge, dict):
        _fail('previous_catalog: lineage edge must be an object')
    cid = _pick(edge, ('old_catalog_id', 'catalog_id') + _CATALOG_ID_KEYS[1:])
    eid = _pick(edge, ('new_event_id',) + _EVENT_ID_KEYS)
    count = _pick(edge, ('shared_count', 'shared', 'weight', 'count'))
    ids = _pick(edge, ('shared_arrival_ids', 'arrival_ids', 'shared_ids'))
    inherited = _pick(edge, ('inherited', 'is_inherited'))
    if not isinstance(cid, str) or not cid:
        _fail('lineage edge is missing old catalog id')
    if not isinstance(eid, str) or not eid:
        _fail('lineage edge is missing new event id')
    if not _is_int(count) or isinstance(count, bool) or count < 1:
        _fail('lineage edge shared_count must be a positive integer')
    shared = _normalize_arrival_ids(ids, 'lineage edge')
    if len(shared) != count:
        _fail('lineage edge shared_count does not match shared_arrival_ids')
    if not isinstance(inherited, bool):
        _fail('lineage edge inherited flag must be boolean')


def _normalize_binding(raw):
    if raw is None:
        return None
    if isinstance(raw, str):
        return {'path': raw, 'catalog_digest': None}
    if not isinstance(raw, dict):
        return None
    path = _pick(raw, ('path', 'directory_path', 'root'))
    digest = _pick(raw, ('catalog_digest', 'digest', 'catalog_sha256'))
    return {'path': path if isinstance(path, str) else None,
            'catalog_digest': digest if isinstance(digest, str) else None}


# ---------------------------------------------------------------------------
# 目录绑定
# ---------------------------------------------------------------------------

def _directory_binding(review_directory, versions):
    path = None
    raw_path = getattr(review_directory, '_path', None)
    if raw_path is None:
        raw_path = getattr(review_directory, 'path', None)
    if raw_path is not None:
        try:
            path = os.path.realpath(os.fspath(raw_path))
        except (TypeError, ValueError):
            path = str(raw_path)
    catalog = getattr(review_directory, '_catalog', None)
    if not isinstance(catalog, dict):
        catalog = getattr(review_directory, 'catalog', None)
    if isinstance(catalog, dict):
        digest = _digest(catalog)
    else:
        # 鸭子类型的 ReviewDirectory-like 对象：用版本序列作为内容绑定，
        # 仍然是确定的；真实 ReviewDirectory 始终有 _catalog。
        digest = _digest({'versions': list(versions)})
    return {'path': path, 'catalog_digest': digest}


def _binding_matches(stored, current):
    if stored is None:
        return True
    if stored.get('path') and current.get('path'):
        if os.path.realpath(stored['path']) != current['path']:
            return False
    if stored.get('catalog_digest') and current.get('catalog_digest'):
        if stored['catalog_digest'] != current['catalog_digest']:
            return False
    return True


# ---------------------------------------------------------------------------
# 全局一对一最大权匹配（最小费用流 + 势能 Dijkstra）
# ---------------------------------------------------------------------------

class _MinCostFlow:
    def __init__(self, n):
        self.n = n
        self.g = [[] for _ in range(n)]

    def add_edge(self, u, v, cap, cost):
        forward = [v, cap, cost, len(self.g[v])]
        backward = [u, 0, -cost, len(self.g[u])]
        self.g[u].append(forward)
        self.g[v].append(backward)
        return len(self.g[u]) - 1

    def edge_cap(self, u, index):
        return self.g[u][index][1]

    def min_cost_improving_flow(self, s, t):
        """不断按最短路增广，直到最短增广路原始费用非负。"""
        n = self.n
        inf = float('inf')
        # 初始势能：一次 SPFA 求 s 到各点最短距离（存在负费用边）。
        potential = [inf] * n
        potential[s] = 0
        in_queue = [False] * n
        queue = [s]
        in_queue[s] = True
        head = 0
        while head < len(queue):
            u = queue[head]
            head += 1
            in_queue[u] = False
            du = potential[u]
            for v, cap, cost, _rev in self.g[u]:
                if cap > 0 and potential[v] > du + cost:
                    potential[v] = du + cost
                    if not in_queue[v]:
                        queue.append(v)
                        in_queue[v] = True
        for i in range(n):
            if potential[i] == inf:
                potential[i] = 0

        total = 0
        while True:
            import heapq
            dist = [inf] * n
            parent_v = [-1] * n
            parent_e = [-1] * n
            dist[s] = 0
            pq = [(0, s)]
            while pq:
                d, u = heapq.heappop(pq)
                if d != dist[u]:
                    continue
                for ei, (v, cap, cost, _rev) in enumerate(self.g[u]):
                    if cap <= 0:
                        continue
                    reduced = cost + potential[u] - potential[v]
                    nd = d + reduced
                    if nd < dist[v]:
                        dist[v] = nd
                        parent_v[v] = u
                        parent_e[v] = ei
                        heapq.heappush(pq, (nd, v))
            if dist[t] == inf:
                break
            for v in range(n):
                if dist[v] < inf:
                    potential[v] += dist[v]
            # 沿父链计算原始费用；非负则不再有改进。
            path_cost = 0
            v = t
            while v != s:
                u = parent_v[v]
                ei = parent_e[v]
                if u < 0:
                    _fail('matching solver internal error')
                edge = self.g[u][ei]
                path_cost += edge[2]
                v = u
            if path_cost >= 0:
                break
            add = 1
            v = t
            while v != s:
                u = parent_v[v]
                ei = parent_e[v]
                edge = self.g[u][ei]
                edge[1] -= add
                rev_index = edge[3]
                self.g[v][rev_index][1] += add
                v = u
            total += path_cost
        return total


def solve_global_associations(old_ids, new_keys, candidates):
    """在候选关联上求全局一对一最优继承选择。

    参数
    ----
    old_ids : 旧编目 ID 列表（任意顺序，内部排序）。
    new_keys : 与新事件对齐的规范键元组列表。
    candidates : 可迭代 ``(old_index, new_index, weight)``，weight 为正整数。

    返回被选候选的 ``(old_index, new_index)`` 集合。选择规则见模块文档。
    本纯函数也可直接用于合成数据上的选择算法验收。
    """
    old_ids = list(old_ids)
    new_keys = [tuple(k) for k in new_keys]
    candidates = [(int(oi), int(ni), int(w)) for oi, ni, w in candidates]
    candidates = [c for c in candidates if c[2] > 0]
    if not candidates:
        return set()

    # 第三优先级排序：(旧编目 ID 字符串字典序, 新事件规范键字典序)
    order = sorted(
        range(len(candidates)),
        key=lambda k: (old_ids[candidates[k][0]], new_keys[candidates[k][1]]))
    rank_of = {edge_index: rank for rank, edge_index in enumerate(order)}

    m = len(candidates)
    old_order = sorted(range(len(old_ids)), key=lambda i: old_ids[i])
    new_order = sorted(range(len(new_keys)), key=lambda i: new_keys[i])
    old_node = {i: 1 + pos for pos, i in enumerate(old_order)}
    base_new = 1 + len(old_ids)
    new_node = {i: base_new + pos for pos, i in enumerate(new_order)}
    source = 0
    sink = base_new + len(new_keys)
    mcf = _MinCostFlow(sink + 1)

    for oi in old_order:
        mcf.add_edge(source, old_node[oi], 1, 0)
    for ni in new_order:
        mcf.add_edge(new_node[ni], sink, 1, 0)

    max_pairs = min(len(old_ids), len(new_keys))
    lex_base = 1 << m
    weight_unit = (max_pairs + 1) * lex_base

    tracked = []
    for ci, (oi, ni, weight) in enumerate(candidates):
        lex_bit = 1 << (m - 1 - rank_of[ci])
        score = weight * weight_unit + lex_base + lex_bit
        u, v = old_node[oi], new_node[ni]
        edge_index = mcf.add_edge(u, v, 1, -score)
        tracked.append((ci, u, edge_index))

    mcf.min_cost_improving_flow(source, sink)

    selected = set()
    for ci, u, edge_index in tracked:
        if mcf.edge_cap(u, edge_index) == 0:  # 原始容量 1，余容 0 即被选
            oi, ni, _w = candidates[ci]
            selected.add((oi, ni))
    return selected


# ---------------------------------------------------------------------------
# 状态构造
# ---------------------------------------------------------------------------

def _ce_id(number):
    return f'ce{number}'


def _event_view(catalog_id, event_id, key):
    return {
        'catalog_id': catalog_id,
        'event_id': event_id,
        'canonical_key': list(key),
    }


def _identity_view(catalog_id, status, created_version, ended_version,
                   event_id, key):
    view = _event_view(catalog_id, event_id, key)
    view.update({
        'status': status,
        'created_version': created_version,
        'ended_version': ended_version,
    })
    return view


def _edge_view(old_catalog_id, new_event_id, shared_ids, inherited):
    shared = sorted(shared_ids)
    return {
        'old_catalog_id': old_catalog_id,
        'new_event_id': new_event_id,
        'shared_count': len(shared),
        'shared_arrival_ids': shared,
        'inherited': inherited,
    }


def _new_event_view(event_id, catalog_id, key, inherited_from, overlaps):
    view = _event_view(catalog_id, event_id, key)
    view.update({
        'inherited_from': inherited_from,
        'overlaps': sorted(overlaps),
    })
    return view


def _build_first_state(plan_events, target_version, binding):
    ordered = sorted(plan_events, key=lambda e: e['canonical_key'])
    events = []
    identities = []
    for number, item in enumerate(ordered, 1):
        cid = _ce_id(number)
        events.append(_event_view(cid, item['event_id'], item['canonical_key']))
        identities.append(_identity_view(
            cid, 'active', target_version, None,
            item['event_id'], item['canonical_key']))
    state = {
        'schema': SCHEMA,
        'directory': binding,
        'last_version': target_version,
        'next_sequence': len(ordered) + 1,
        'events': events,
        'identities': identities,
        'lineage': [],
    }
    return state


def _build_transition(previous, plan_events, target_version, binding):
    old_events = previous['events']
    old_ids = [e['catalog_id'] for e in old_events]
    old_index = {cid: i for i, cid in enumerate(old_ids)}
    old_by_id = {e['catalog_id']: e for e in old_events}

    new_keys = [e['canonical_key'] for e in plan_events]

    # 到时 ID -> 旧编目身份；据此统计每个新事件与各旧身份的共享数及精确 ID。
    arrival_owner = {}
    for e in old_events:
        for aid in e['arrival_ids']:
            arrival_owner[aid] = e['catalog_id']

    candidates = []          # (old_idx, new_idx, weight)
    shared_details = {}     # (old_id, new_idx) -> sorted shared ids
    overlap_old_ids = []
    for ni, item in enumerate(plan_events):
        counts = Counter()
        details = {}
        for aid in item['canonical_key']:
            owner = arrival_owner.get(aid)
            if owner is not None:
                counts[owner] += 1
                details.setdefault(owner, []).append(aid)
        overlaps = sorted(counts)
        overlap_old_ids.append(overlaps)
        for cid in overlaps:
            oi = old_index[cid]
            candidates.append((oi, ni, counts[cid]))
            shared_details[(cid, ni)] = sorted(details[cid])

    selected = solve_global_associations(old_ids, new_keys, candidates)
    selected_old_to_new = {}
    selected_new_to_old = {}
    for oi, ni in selected:
        cid = old_ids[oi]
        selected_old_to_new[cid] = ni
        selected_new_to_old[ni] = cid

    # 未继承旧身份的新事件按规范键排序，依次分配新序号。
    next_number = previous['next_sequence']
    fresh_new_indices = [ni for ni in range(len(plan_events))
                         if ni not in selected_new_to_old]
    fresh_new_indices.sort(key=lambda ni: new_keys[ni])
    assigned_cid = {ni: cid for ni, cid in selected_new_to_old.items()}
    created_ids = []
    for ni in fresh_new_indices:
        cid = _ce_id(next_number)
        next_number += 1
        assigned_cid[ni] = cid
        created_ids.append(cid)

    ended_ids = sorted(cid for cid in old_ids if cid not in selected_old_to_new)

    # ---- 谱系边：所有正权重关联，确定性排序 ----
    edge_records = []
    candidate_keys = []
    for oi, ni, weight in candidates:
        cid = old_ids[oi]
        key = (cid, new_keys[ni])
        candidate_keys.append((key, cid, ni))
    candidate_keys.sort(key=lambda x: x[0])
    for _key, cid, ni in candidate_keys:
        inherited = selected_new_to_old.get(ni) == cid
        edge_records.append(_edge_view(
            cid, plan_events[ni]['event_id'],
            shared_details[(cid, ni)], inherited))

    # ---- 每个新事件的身份与重叠旧 ID ----
    transition_events = []
    active_events = []
    for ni, item in enumerate(plan_events):
        cid = assigned_cid[ni]
        inherited_from = selected_new_to_old.get(ni)
        overlaps = overlap_old_ids[ni]
        transition_events.append(_new_event_view(
            item['event_id'], cid, item['canonical_key'],
            inherited_from, overlaps))
        active_events.append(_event_view(cid, item['event_id'],
                                         item['canonical_key']))

    # 活动事件按编目 ID 排序（ceN 的自然序号；不足 10 时与字典序相同）。
    active_events.sort(key=lambda v: _ce_number(v['catalog_id']) or 10 ** 12)
    transition_events.sort(key=lambda v: _ce_number(v['catalog_id']) or 10 ** 12)

    transition = {
        'from_version': previous['last_version'],
        'to_version': target_version,
        'associations': edge_records,
        'events': transition_events,
        'ended': ended_ids,
        'created': sorted(created_ids, key=lambda c: _ce_number(c) or 0),
    }

    # ---- 全量身份记录：保留历史，结束的标记 ended，新建的入册 ----
    identity_records = {}
    for cid, raw in previous['identities'].items():
        identity_records[cid] = dict(raw)
    # 兼容缺少 identities 列表的精简旧状态：从活动事件补全。
    for e in old_events:
        identity_records.setdefault(e['catalog_id'], _identity_view(
            e['catalog_id'], 'active', previous['last_version'], None,
            e['event_id'], e['arrival_ids']))

    for cid in ended_ids:
        rec = identity_records.get(cid)
        key = old_by_id[cid]['arrival_ids']
        eid = old_by_id[cid]['event_id']
        if rec is None:
            rec = _identity_view(cid, 'ended', None, target_version, eid, key)
        rec['status'] = 'ended'
        rec['ended_version'] = target_version
        rec.setdefault('catalog_id', cid)
        identity_records[cid] = rec

    for ni, cid in assigned_cid.items():
        item = plan_events[ni]
        if ni in selected_new_to_old:
            rec = identity_records[cid]
            rec['status'] = 'active'
            rec['ended_version'] = None
            rec['event_id'] = item['event_id']
            rec['canonical_key'] = list(item['canonical_key'])
            # 若旧状态使用其他物理别名，同步它们以免产生冲突值。
            for alias in _EVENT_ID_KEYS[1:]:
                if alias in rec:
                    rec[alias] = item['event_id']
            for alias in _ARRIVAL_KEYS[1:]:
                if alias in rec:
                    rec[alias] = list(item['canonical_key'])
        else:
            identity_records[cid] = _identity_view(
                cid, 'active', target_version, None,
                item['event_id'], item['canonical_key'])

    # 补齐序号区间内可能缺失的历史身份（仅对非本 API 产出的宽松状态发生）。
    for number in range(1, next_number):
        cid = _ce_id(number)
        if cid not in identity_records:
            identity_records[cid] = {
                'catalog_id': cid, 'status': 'ended',
                'created_version': None, 'ended_version': None,
                'event_id': None, 'canonical_key': []}

    identities = [identity_records[cid]
                  for cid in sorted(identity_records,
                                    key=lambda c: _ce_number(c) or 10 ** 12)]

    lineage = list(previous['lineage'])
    lineage.append(transition)

    return {
        'schema': SCHEMA,
        'directory': binding,
        'last_version': target_version,
        'next_sequence': next_number,
        'events': active_events,
        'identities': identities,
        'lineage': lineage,
    }


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------

def reconcile_event_catalog(previous_catalog, review_directory, version_id=None):
    """读取下一校时版本计划，返回带稳定 ``ceN`` 身份与完整谱系的新状态。

    - ``previous_catalog is None`` 时从目录首个校时版本建立身份；
    - 否则必须紧接 ``previous_catalog['last_version']`` 处理
      ``list_versions()`` 中的下一版本；
    - 省略 ``version_id`` 自动取下一版本，显式给出时也必须正是该版本；
    - 不关闭 ``review_directory``，不调用任何写入方法。

    任何契约不满足都抛 :class:`ValueError`（具体为 ``CatalogStateError``），
    且不改动复核目录与调用方传入的 ``previous_catalog``。
    """
    if not (hasattr(review_directory, 'list_versions')
            and callable(getattr(review_directory, 'list_versions'))
            and hasattr(review_directory, 'read_plan')
            and callable(getattr(review_directory, 'read_plan'))):
        _fail('review_directory must be an open ReviewDirectory')

    try:
        versions = review_directory.list_versions()
    except CatalogStateError:
        raise
    except Exception as exc:  # 已关闭等：统一为契约错误类型
        raise CatalogStateError(f'cannot list versions from ReviewDirectory: {exc}') from exc
    if not isinstance(versions, list) or not versions \
            or not all(isinstance(v, str) and v for v in versions):
        _fail('ReviewDirectory.list_versions() returned an invalid version order')

    previous = None
    if previous_catalog is not None:
        previous = normalize_previous_catalog(previous_catalog)

    # 目录绑定：优先拒绝跨目录续接。
    current_binding = _directory_binding(review_directory, versions)
    if previous is not None and not _binding_matches(previous['binding'],
                                                     current_binding):
        _fail('previous_catalog is bound to a different review directory')

    # 版本顺序：首状态必须从第一个版本开始；后续必须紧接 last_version。
    if previous is None:
        expected_index = 0
    else:
        if previous['last_version'] not in versions:
            _fail('previous_catalog last_version is not part of this review directory')
        expected_index = versions.index(previous['last_version']) + 1
        if expected_index >= len(versions):
            _fail('no later clock version remains to reconcile for this catalog state')

    target_version = versions[expected_index]
    if version_id is not None:
        if not isinstance(version_id, str) or not version_id:
            _fail('version_id must be a nonempty string')
        if version_id != target_version:
            if previous is None:
                _fail(f'first reconciliation must start at {versions[0]!r}, '
                      f'not {version_id!r}')
            _fail(f'version {version_id!r} is not the next unreconciled version '
                  f'{target_version!r}; skipping or replaying is not allowed')

    # 整个调用中唯一一次 read_plan，避免对调用方的读计划桩产生额外干扰。
    try:
        plan = review_directory.read_plan(target_version)
    except CatalogStateError:
        raise
    except Exception as exc:
        raise CatalogStateError(
            f'plan for version {target_version!r} cannot be obtained '
            f'from ReviewDirectory: {exc}') from exc

    plan_events = normalize_plan(plan)

    if previous is None:
        result = _build_first_state(plan_events, target_version, current_binding)
    else:
        result = _build_transition(previous, plan_events, target_version,
                                   current_binding)
    return _wrap_deep(result)


# 允许 ``from seismic_assoc.review import reconcile_event_catalog``。
from . import review as _review_module  # noqa: E402

_review_module.reconcile_event_catalog = reconcile_event_catalog
