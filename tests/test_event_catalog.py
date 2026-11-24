"""稳定事件目录身份 API 的验收与边界测试（全部合成数据）。

覆盖：
- 首版按规范键分配 ceN、计划/到时顺序无关；
- 全局一对一选择（含 10/9/8/0 矩阵）与第二、第三优先级同分裁决；
- 拆分、合并、先拆后并的连续谱系；
- 重复计算确定性、JSON 可序列化、调用方状态不被修改；
- 版本顺序、跨目录、结构非法、重复/缺失到时等拒绝路径；
- API 不改动 read_plan、当前版本、归档回执、下线状态与目录句柄。
"""
import copy
import json
import random
import shutil
import tempfile
import unittest
from pathlib import Path

from seismic_assoc import (
    create_review,
    open_review,
    reconcile_event_catalog,
)
from seismic_assoc.event_catalog import solve_global_associations


# ---------------------------------------------------------------------------
# 合成目录构造工具
# ---------------------------------------------------------------------------

def one_arrival_per_station_input(arrival_ids, window='10'):
    """每个到时一个台站：校时 offset 可直接、精确地控制统一时间分组。"""
    n = len(arrival_ids)
    return {
        'stations': [{'id': f'S{i}', 'lat': '0', 'lon': '0'} for i in range(n)],
        'events': [{'id': aid, 'station': f'S{i}', 'phase': 'P', 'time': 100}
                   for i, aid in enumerate(arrival_ids)],
        'window': window,
        'domains': {f'S{i}': [0, 300] for i in range(n)},
        'blocks': [{'id': f'B{i}', 'station': f'S{i}', 'interval': [0, 300]}
                   for i in range(n)],
        'pre': '1', 'post': '2',
    }


def offset_clock(version_id, arrival_ids, high_predicate, high_offset='100'):
    return {'version_id': version_id, 'stations': {
        f'S{i}': [{'interval': [0, 300],
                   'offset': high_offset if high_predicate(aid) else '0'}]
        for i, aid in enumerate(arrival_ids)}}


def split_merge_input():
    ids = ['a', 'b', 'c', 'd']
    return one_arrival_per_station_input(ids), ids


class StubDirectory:
    """list_versions/read_plan 的最小鸭子类型桩，用于合成计划与裁决测试。"""

    def __init__(self, versions, plans):
        self._versions = list(versions)
        self._plans = dict(plans)
        self.read_calls = []
        self.closed = False

    def list_versions(self):
        if self.closed:
            raise ValueError('closed')
        return list(self._versions)

    def read_plan(self, version_id=None):
        self.read_calls.append(version_id)
        if self.closed:
            raise ValueError('closed')
        if version_id not in self._plans:
            raise ValueError('unknown clock version')
        return copy.deepcopy(self._plans[version_id])


def stub_plan(version_id, groups, shuffle_seed=None):
    """groups: [(event_id, [arrival ids]), ...]"""
    events = []
    for eid, ids in groups:
        ids = list(ids)
        if shuffle_seed is not None:
            random.Random(shuffle_seed + len(eid)).shuffle(ids)
        events.append({'event_id': eid,
                       'arrivals': [{'id': a} for a in ids]})
    if shuffle_seed is not None:
        random.Random(shuffle_seed).shuffle(events)
    return {'version_id': version_id, 'events': events}


def transition_of(catalog_state, index=-1):
    return catalog_state['lineage'][index]


def inherited_pairs(turn):
    return sorted((edge['old_catalog_id'], edge['new_event_id'])
                  for edge in turn['associations'] if edge['inherited'])


# ---------------------------------------------------------------------------
# 1. 首次分配与顺序无关性
# ---------------------------------------------------------------------------

class InitialAssignmentTests(unittest.TestCase):
    def test_assign_by_canonical_key_not_array_order(self):
        # 规范键字典序：['x'] < ['y','z']；故意把大键事件放在数组前面，
        # 并打乱到时列表，ce 编号仍按规范键顺序分配。
        plan = stub_plan('v1', [('e9', ['z', 'y']), ('e1', ['x'])],
                         shuffle_seed=7)
        d = StubDirectory(['v1'], {'v1': plan})
        cat = reconcile_event_catalog(None, d)
        self.assertEqual([e['catalog_id'] for e in cat['events']],
                         ['ce1', 'ce2'])
        self.assertEqual(cat['events'][0]['canonical_key'], ['x'])
        self.assertEqual(cat['events'][0]['event_id'], 'e1')
        self.assertEqual(cat['events'][1]['canonical_key'], ['y', 'z'])
        self.assertEqual(cat['next_sequence'], 3)
        self.assertEqual(cat['last_version'], 'v1')
        self.assertEqual(cat['lineage'], [])
        # 所有身份已入册
        self.assertEqual([i['catalog_id'] for i in cat['identities']],
                         ['ce1', 'ce2'])
        self.assertTrue(all(i['status'] == 'active' for i in cat['identities']))

    def test_explicit_first_version_must_be_first(self):
        d = StubDirectory(['v1', 'v2'], {
            'v1': stub_plan('v1', [('e1', ['a'])]),
            'v2': stub_plan('v2', [('e1', ['a'])])})
        with self.assertRaises(ValueError):
            reconcile_event_catalog(None, d, 'v2')
        cat = reconcile_event_catalog(None, d, 'v1')
        self.assertEqual(cat['last_version'], 'v1')

    def test_json_serializable(self):
        d = StubDirectory(['v1'], {'v1': stub_plan('v1', [('e1', ['a', 'b'])])})
        cat = reconcile_event_catalog(None, d)
        text = json.dumps(cat, ensure_ascii=False)
        again = json.loads(text)
        self.assertEqual(again['next_sequence'], 2)


# ---------------------------------------------------------------------------
# 2. 全局选择：10/9/8/0 矩阵 + 第二、第三优先级
# ---------------------------------------------------------------------------

class GlobalSelectionSolverTests(unittest.TestCase):
    def test_acceptance_matrix_17_beats_greedy_10(self):
        old = ['ce1', 'ce2']
        new_keys = [('N1',), ('N2',)]
        candidates = [(0, 0, 10), (0, 1, 9), (1, 0, 8)]
        selected = solve_global_associations(old, new_keys, candidates)
        self.assertEqual(selected, {(0, 1), (1, 0)})
        total = sum(w for oi, ni, w in candidates if (oi, ni) in selected)
        self.assertEqual(total, 17)

    def test_secondary_objective_max_inherited_count(self):
        # 单条 10 与 5+5 总数相同（10），后者继承两个旧身份。
        old = ['ce1', 'ce2']
        new_keys = [('N1',), ('N2',)]
        candidates = [(0, 0, 10), (0, 1, 5), (1, 0, 5)]
        selected = solve_global_associations(old, new_keys, candidates)
        self.assertEqual(selected, {(0, 1), (1, 0)})

    def test_tertiary_objective_lexicographic_pairs(self):
        old = ['ce1', 'ce2']
        # 规范键 'ka' < 'kb'：ce1 配 ka 的解字典序更小。
        selected = solve_global_associations(
            old, [('ka',), ('kb',)],
            [(0, 0, 1), (0, 1, 1), (1, 0, 1), (1, 1, 1)])
        self.assertEqual(selected, {(0, 0), (1, 1)})
        # 交换两个新事件的规范键位置，最优关联随规范键而非数组位置。
        selected = solve_global_associations(
            old, [('zzz',), ('aaa',)],
            [(0, 0, 1), (0, 1, 1), (1, 0, 1), (1, 1, 1)])
        self.assertEqual(selected, {(0, 1), (1, 0)})

    def test_solver_matches_exhaustive_on_random_small(self):
        rng = random.Random(207)
        for _case in range(200):
            no = rng.randint(1, 4)
            nn = rng.randint(1, 4)
            old = [f'ce{i}' for i in range(1, no + 1)]
            keys = [tuple([f'k{j}']) for j in range(nn)]
            candidates = []
            weights = {}
            for i in range(no):
                for j in range(nn):
                    w = rng.randint(0, 3)
                    if w:
                        candidates.append((i, j, w))
                        weights[(i, j)] = w
            best = self._brute_best(old, keys, candidates)
            got = solve_global_associations(old, keys, candidates)
            self.assertEqual(self._objective(old, keys, got, weights),
                             best, msg=(candidates, got))

    @staticmethod
    def _objective(old, keys, selected, weights):
        total = sum(weights[p] for p in selected)
        pairs = sorted((old[i], keys[j]) for i, j in selected)
        return total, len(selected), pairs

    def _brute_best(self, old, keys, candidates):
        weights = {(i, j): w for i, j, w in candidates}
        edges = list(weights)
        best = None

        def visit(pos, used_o, used_n, chosen):
            nonlocal best
            if pos == len(edges):
                selected = set(chosen)
                obj = self._objective(old, keys, selected, weights)
                if best is None or (obj[0], obj[1], _Lex(obj[2])) > \
                        (best[0], best[1], _Lex(best[2])):
                    best = obj
                return
            visit(pos + 1, used_o, used_n, chosen)
            i, j = edges[pos]
            if i not in used_o and j not in used_n:
                visit(pos + 1, used_o | {i}, used_n | {j},
                      chosen + [(i, j)])

        visit(0, set(), set(), [])
        # 暴力比较器：总数最大、数量最大、关联列表字典序最小
        return best


class _Lex(list):
    """让 max() 选中字典序最小的第三项：用反向包装。"""

    def __lt__(self, other):  # noqa: D401
        return list.__gt__(self, other)

    def __gt__(self, other):
        return list.__lt__(self, other)


class MatrixEndToEndTests(unittest.TestCase):
    """用真实 create_review 目录实现 10/9/8/0 共享矩阵。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / 'matrix'
        ids = [f'a{i:02d}' for i in range(1, 11)]
        ids += [f'b{i:02d}' for i in range(1, 10)]
        ids += [f'c{i:02d}' for i in range(1, 9)]
        self.ids = ids
        data = one_arrival_per_station_input(ids)
        v1 = offset_clock('v1', ids, lambda a: a.startswith('c'))
        self.d = create_review(self.path, data, v1)
        # v1: a+b = O1(19) -> e1, c = O2(8) -> e2
        self.cat = reconcile_event_catalog(None, self.d)
        v2 = offset_clock('v2', ids,
                          lambda a: a.startswith('a') or a.startswith('c'))
        self.d.submit_clock(v2)

    def tearDown(self):
        self.d.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_matrix_selection_and_losing_edge_lineage(self):
        cat2 = reconcile_event_catalog(copy.deepcopy(self.cat), self.d)
        turn = transition_of(cat2)
        # O1 -> N2(9) 与 O2 -> N1(8)，合计 17
        self.assertEqual(inherited_pairs(turn),
                         [('ce1', 'e1'), ('ce2', 'e2')])
        winners = sum(e['shared_count'] for e in turn['associations']
                      if e['inherited'])
        self.assertEqual(winners, 17)
        # 三条正权重边全部入谱系，包括未胜出的 O1-N1=10
        self.assertEqual(len(turn['associations']), 3)
        losing = next(e for e in turn['associations']
                       if not e['inherited'])
        self.assertEqual((losing['old_catalog_id'], losing['new_event_id']),
                         ('ce1', 'e2'))
        self.assertEqual(losing['shared_count'], 10)
        self.assertEqual(losing['shared_arrival_ids'],
                         [f'a{i:02d}' for i in range(1, 11)])
        self.assertFalse(losing['inherited'])
        win9 = next(e for e in turn['associations']
                    if e['inherited'] and e['shared_count'] == 9)
        self.assertEqual(win9['shared_arrival_ids'],
                         [f'b{i:02d}' for i in range(1, 10)])
        win8 = next(e for e in turn['associations']
                    if e['inherited'] and e['shared_count'] == 8)
        self.assertEqual(win8['shared_arrival_ids'],
                         [f'c{i:02d}' for i in range(1, 9)])
        # 新事件重叠旧 ID 列表完整
        by_eid = {e['event_id']: e for e in turn['events']}
        self.assertEqual(by_eid['e2']['overlaps'], ['ce1', 'ce2'])
        self.assertEqual(by_eid['e1']['overlaps'], ['ce1'])

    def test_shuffled_plan_read_gives_same_result(self):
        expected = reconcile_event_catalog(copy.deepcopy(self.cat), self.d)
        original = self.d.read_plan

        def shuffled(vid=None):
            plan = original(vid)
            rng = random.Random(42)
            rng.shuffle(plan['events'])
            for e in plan['events']:
                rng.shuffle(e['arrivals'])
            return plan

        self.d.read_plan = shuffled  # 实例属性覆盖
        try:
            got = reconcile_event_catalog(copy.deepcopy(self.cat), self.d)
        finally:
            del self.d.read_plan
        self.assertEqual(json.dumps(got, sort_keys=True, ensure_ascii=False),
                         json.dumps(expected, sort_keys=True, ensure_ascii=False))


# ---------------------------------------------------------------------------
# 3. 拆分 / 合并 / 先拆后并
# ---------------------------------------------------------------------------

class SplitMergeRealDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _dir(self, name, ids):
        return create_review(Path(self.tmp) / name,
                             one_arrival_per_station_input(ids),
                             offset_clock('v1', ids, lambda a: False))

    def test_split_one_old_into_two_new(self):
        ids = ['a', 'b', 'c']
        d = self._dir('split', ids)
        cat = reconcile_event_catalog(None, d)
        self.assertEqual([e['catalog_id'] for e in cat['events']], ['ce1'])
        # v2: a 独立，b,c 同组
        d.submit_clock(offset_clock(
            'v2', ids, lambda a: a == 'b' or a == 'c'))
        cat2 = reconcile_event_catalog(cat, d)
        turn = transition_of(cat2)
        # ce1 按最大共享数继承 b,c 事件；a 事件获得 ce2
        self.assertEqual(inherited_pairs(turn), [('ce1', 'e2')])
        self.assertEqual(len(turn['associations']), 2)
        self.assertTrue(all(e['shared_count'] == 1 or e['shared_count'] == 2
                            for e in turn['associations']))
        winner = next(e for e in turn['associations'] if e['inherited'])
        self.assertEqual(winner['shared_count'], 2)
        loser = next(e for e in turn['associations'] if not e['inherited'])
        self.assertEqual(loser['shared_count'], 1)
        self.assertEqual(turn['ended'], [])
        self.assertEqual([c for c in turn['created']], ['ce2'])
        active = {e['catalog_id']: e['canonical_key'] for e in cat2['events']}
        self.assertEqual(active, {'ce1': ['b', 'c'], 'ce2': ['a']})
        d.close()

    def test_merge_two_old_into_one_new(self):
        ids = ['a', 'b', 'c', 'd']
        # v1: a,b=O1 ; c,d=O2
        d = create_review(
            Path(self.tmp) / 'merge',
            one_arrival_per_station_input(ids),
            offset_clock('v1', ids, lambda a: a in ('c', 'd')))
        cat = reconcile_event_catalog(None, d)
        self.assertEqual([e['catalog_id'] for e in cat['events']],
                         ['ce1', 'ce2'])
        # v2: 全部合为一个事件
        d.submit_clock(offset_clock('v2', ids, lambda a: False))
        cat2 = reconcile_event_catalog(cat, d)
        turn = transition_of(cat2)
        # 两条等权 2 的关联，第三优先级让 ce1 继承；ce2 结束但保留
        self.assertEqual(inherited_pairs(turn), [('ce1', 'e1')])
        self.assertEqual(len(turn['associations']), 2)
        self.assertEqual(turn['ended'], ['ce2'])
        self.assertEqual(turn['created'], [])
        self.assertEqual([e['catalog_id'] for e in cat2['events']], ['ce1'])
        self.assertEqual(cat2['events'][0]['canonical_key'],
                         ['a', 'b', 'c', 'd'])
        statuses = {i['catalog_id']: i['status']
                    for i in cat2['identities']}
        self.assertEqual(statuses, {'ce1': 'active', 'ce2': 'ended'})
        ended = next(i for i in cat2['identities']
                     if i['catalog_id'] == 'ce2')
        self.assertEqual(ended['ended_version'], 'v2')
        self.assertEqual(cat2['next_sequence'], 3)  # 序号不因结束而回退
        d.close()

    def test_split_then_merge_traceability_and_no_id_reuse(self):
        ids = ['a', 'b', 'c']
        d = self._dir('splitmerge', ids)
        c1 = reconcile_event_catalog(None, d)
        d.submit_clock(offset_clock(
            'v2', ids, lambda a: a in ('b', 'c')))
        c2 = reconcile_event_catalog(c1, d)
        self.assertEqual({e['catalog_id'] for e in c2['events']},
                         {'ce1', 'ce2'})
        # v3 重新合并
        d.submit_clock(offset_clock('v3', ids, lambda a: False))
        c3 = reconcile_event_catalog(c2, d)
        self.assertEqual(len(c3['lineage']), 2)
        last = transition_of(c3)
        # ce1 共享 b,c（2）胜过 ce2 共享 a（1），ce2 结束，不再产生新 ID
        self.assertEqual(inherited_pairs(last), [('ce1', 'e1')])
        self.assertEqual(last['ended'], ['ce2'])
        self.assertEqual(last['created'], [])
        self.assertEqual(c3['next_sequence'], 3)
        self.assertEqual([e['catalog_id'] for e in c3['events']], ['ce1'])
        # ce2 历史完整：创建于 v2，结束于 v3，谱系中仍可追溯
        rec = next(i for i in c3['identities'] if i['catalog_id'] == 'ce2')
        self.assertEqual(rec['status'], 'ended')
        self.assertEqual(rec['created_version'], 'v2')
        self.assertEqual(rec['ended_version'], 'v3')
        # 两次转换的所有正重叠边都在
        self.assertEqual(len(c3['lineage'][0]['associations']), 2)
        self.assertEqual(len(c3['lineage'][1]['associations']), 2)
        d.close()


# ---------------------------------------------------------------------------
# 4. 验收 1：校时重排、局部 eN 不可继承、目录零改动
# ---------------------------------------------------------------------------

class ClockReorderImmutabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / 'reorder'
        self.data = {
            'stations': [{'id': 'C', 'lat': '0', 'lon': '0'},
                         {'id': 'D', 'lat': '1', 'lon': '1'}],
            'events': [{'id': 'c1', 'station': 'C', 'phase': 'P', 'time': 10},
                       {'id': 'd1', 'station': 'D', 'phase': 'P', 'time': 11}],
            'window': '1',
            'domains': {'C': [0, 30], 'D': [0, 30]},
            'blocks': [{'id': 'C1', 'station': 'C', 'interval': [0, 20]},
                       {'id': 'C2', 'station': 'C', 'interval': [20, 30]},
                       {'id': 'D1', 'station': 'D', 'interval': [0, 30]}],
            'pre': '1', 'post': '2'}
        self.v1 = {'version_id': 'k1', 'stations': {
            'C': [{'interval': [0, 30], 'offset': '0'}],
            'D': [{'interval': [0, 30], 'offset': '0'}]}}
        self.v2 = {'version_id': 'k2', 'stations': {
            'C': [{'interval': [0, 30], 'offset': '0'}],
            'D': [{'interval': [0, 30], 'offset': '-3'}]}}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _snapshot(self, d):
        return {
            'versions': list(d.list_versions()),
            'current': d.current_version(),
            'plans': {v: d.read_plan(v) for v in d.list_versions()},
            'state_bytes': (Path(self.path) / 'state.json').read_bytes(),
            'status': {b: d.block_status(b) for b in ('C1', 'C2', 'D1')},
            'closed': d._closed,
        }

    def test_local_event_number_does_not_decide_identity(self):
        d = create_review(self.path, self.data, self.v1)
        # 加一条归档回执，随后下线一个当前无需求的块，制造归档/下线状态
        d.register_receipt({'receipt_id': 'h1', 'version_id': 'k1',
                            'event_id': 'e1', 'block_id': 'D1',
                            'interval': [9, 12]})
        d.submit_clock(self.v2)
        # C2 在任一版都无需求：真实产生一条下线决定，验证 API 不改动其状态
        approved = d.request_offline({'request_id': 'qC2', 'block_id': 'C2'})
        self.assertTrue(approved['approved'])
        self.assertEqual(d.block_status('C2'), 'offline')
        before = self._snapshot(d)

        cat = reconcile_event_catalog(None, d, 'k1')
        self.assertEqual(cat['events'][0]['canonical_key'], ['c1', 'd1'])
        cat2 = reconcile_event_catalog(cat, d)

        # k2 的 e1 只含 d1，k2 的 e2 只含 c1；尽管 k1/k2 都有 e1，
        # ce1 按规范键字典序继承给含 c1 的 e2，而不是同号 e1。
        mapping = {e['event_id']: e['catalog_id'] for e in cat2['events']}
        self.assertEqual(mapping, {'e2': 'ce1', 'e1': 'ce2'})
        turn = transition_of(cat2)
        self.assertEqual(inherited_pairs(turn), [('ce1', 'e2')])

        # 重复计算完全相同
        again = reconcile_event_catalog(cat, d, version_id='k2')
        self.assertEqual(json.dumps(again, sort_keys=True, ensure_ascii=False),
                         json.dumps(cat2, sort_keys=True, ensure_ascii=False))

        # API 不改动复核目录的任何可见状态，也不关闭句柄
        after = self._snapshot(d)
        self.assertEqual(
            json.dumps(before['plans'], sort_keys=True),
            json.dumps(after['plans'], sort_keys=True))
        self.assertEqual(before['versions'], after['versions'])
        self.assertEqual(before['current'], after['current'])
        self.assertEqual(before['state_bytes'], after['state_bytes'])
        self.assertEqual(before['status'], after['status'])
        self.assertFalse(after['closed'])
        d.close()

    def test_reopen_continues_same_binding(self):
        d = create_review(self.path, self.data, self.v1)
        cat = reconcile_event_catalog(None, d)
        d.submit_clock(self.v2)
        d.close()
        d2 = open_review(self.path)
        try:
            cat2 = reconcile_event_catalog(cat, d2)
            self.assertEqual(cat2['last_version'], 'k2')
            self.assertEqual(len(cat2['lineage']), 1)
            self.assertFalse(d2._closed)
        finally:
            d2.close()


# ---------------------------------------------------------------------------
# 5. 拒绝路径与原子性
# ---------------------------------------------------------------------------

class RejectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path_a = Path(self.tmp) / 'a'
        self.path_b = Path(self.tmp) / 'b'
        self.ids = ['a', 'b', 'c']
        self.d = create_review(
            self.path_a, one_arrival_per_station_input(self.ids),
            offset_clock('v1', self.ids, lambda a: False))
        self.cat = reconcile_event_catalog(None, self.d)

    def tearDown(self):
        self.d.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_non_directory_rejected(self):
        for bad in (None, object(), {}, 'dir'):
            with self.assertRaises(ValueError):
                reconcile_event_catalog(None, bad)

    def test_closed_directory_rejected(self):
        self.d.close()
        with self.assertRaises(ValueError):
            reconcile_event_catalog(None, self.d)

    def test_processed_and_skipped_versions_rejected(self):
        self.d.submit_clock(offset_clock(
            'v2', self.ids, lambda a: a in ('b', 'c')))
        cat2 = reconcile_event_catalog(self.cat, self.d)
        # 已处理版本不能再次作为“下一版”
        with self.assertRaises(ValueError):
            reconcile_event_catalog(self.cat, self.d, 'v1')
        with self.assertRaises(ValueError):
            reconcile_event_catalog(cat2, self.d, 'v2')
        # 制造 v3 后，指定 v3 属于跳版
        self.d.submit_clock(offset_clock('v3', self.ids, lambda a: False))
        with self.assertRaises(ValueError):
            reconcile_event_catalog(self.cat, self.d, 'v3')
        # 省略时取下一版 v2 正常
        ok = reconcile_event_catalog(self.cat, self.d)
        self.assertEqual(ok['last_version'], 'v2')

    def test_no_next_version_rejected(self):
        with self.assertRaises(ValueError):
            reconcile_event_catalog(self.cat, self.d)

    def test_other_directory_state_rejected(self):
        db = create_review(
            self.path_b, one_arrival_per_station_input(self.ids),
            offset_clock('v1', self.ids, lambda a: False))
        try:
            db.submit_clock(offset_clock(
                'v2', self.ids, lambda a: a in ('b', 'c')))
            cat_b = reconcile_event_catalog(None, db)
            db2 = reconcile_event_catalog(cat_b, db)
            # 用 b 目录的状态接到 a 目录：路径绑定不一致
            self.d.submit_clock(offset_clock(
                'v2', self.ids, lambda a: a in ('b', 'c')))
            with self.assertRaises(ValueError):
                reconcile_event_catalog(db2, self.d)
        finally:
            db.close()

    def test_unknown_version_plan_rejected(self):
        stub = StubDirectory(['v1'], {'v1': stub_plan('v1', [('e1', ['a'])])})
        with self.assertRaises(ValueError):
            reconcile_event_catalog(None, stub, 'v9')

    def test_duplicate_and_missing_arrival_identities_rejected(self):
        bad_plans = [
            stub_plan('v1', [('e1', ['a', 'a'])]),               # 事件内重复
            {'version_id': 'v1', 'events': [
                {'event_id': 'e1', 'arrivals': [{'id': 'a'}]},
                {'event_id': 'e2', 'arrivals': [{'id': 'a'}]}]},  # 跨事件重复
            {'version_id': 'v1', 'events': [
                {'event_id': 'e1', 'arrivals': [{'station': 'S'}]}]},  # 缺 id
            {'version_id': 'v1', 'events': [
                {'event_id': 'e1', 'arrivals': []}]},             # 空到时
            {'version_id': 'v1', 'events': []},                  # 空事件
            'not-a-plan',
        ]
        for bad in bad_plans:
            stub = StubDirectory(['v1'], {'v1': bad})
            with self.assertRaises(ValueError):
                reconcile_event_catalog(None, stub)

    def test_invalid_previous_states_rejected(self):
        valid = json.loads(json.dumps(self.cat))
        mutations = []

        m = copy.deepcopy(valid)
        m['next_sequence'] = 1
        mutations.append(('bad sequence', m))

        m = copy.deepcopy(valid)
        m['last_version'] = 'nope'
        mutations.append(('unknown version', m))

        m = copy.deepcopy(valid)
        m['events'][0]['catalog_id'] = 'ce9'
        mutations.append(('sequence gap / identity missing', m))

        m = copy.deepcopy(valid)
        m['identities'][0]['status'] = 'ended'
        m['identities'][0]['ended_version'] = 'v1'
        mutations.append(('ended but still active', m))

        m = copy.deepcopy(valid)
        m['lineage'] = [{'from_version': 'vx', 'to_version': 'vy'}]
        mutations.append(('broken lineage', m))

        for label, bad in mutations:
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    reconcile_event_catalog(bad, self.d)

        for bad in (None, 42, 'state', ['x']):
            if bad is None:
                continue  # None 是合法的首调
            with self.assertRaises(ValueError):
                reconcile_event_catalog(bad, self.d)

    def test_rejected_call_changes_nothing(self):
        state_before = copy.deepcopy(self.cat)
        plans_before = {v: copy.deepcopy(self.d.read_plan(v))
                        for v in self.d.list_versions()}
        state_file_before = (self.path_a / 'state.json').read_bytes()
        with self.assertRaises(ValueError):
            reconcile_event_catalog(self.cat, self.d, 'v9')
        self.assertEqual(self.cat, state_before)
        self.assertEqual(
            {v: self.d.read_plan(v) for v in self.d.list_versions()},
            plans_before)
        self.assertEqual((self.path_a / 'state.json').read_bytes(),
                         state_file_before)

    def test_passed_state_is_not_mutated_on_success(self):
        self.d.submit_clock(offset_clock(
            'v2', self.ids, lambda a: a in ('b', 'c')))
        before = copy.deepcopy(self.cat)
        reconcile_event_catalog(self.cat, self.d)
        self.assertEqual(self.cat, before)


# ---------------------------------------------------------------------------
# 6. 鸭子类型桩上的第二/第三优先级端到端
# ---------------------------------------------------------------------------

class StubTieBreakEndToEndTests(unittest.TestCase):
    def test_secondary_priority_through_api(self):
        v1 = stub_plan('v1', [
            ('e1', [f'x{i}' for i in range(10)] +
                   [f'y{i}' for i in range(5)]),
            ('e2', [f'z{i}' for i in range(5)])])
        v2 = stub_plan('v2', [
            ('e1', [f'y{i}' for i in range(5)]),
            ('e2', [f'x{i}' for i in range(10)] +
                   [f'z{i}' for i in range(5)])])
        d = StubDirectory(['v1', 'v2'], {'v1': v1, 'v2': v2})
        cat = reconcile_event_catalog(None, d)
        self.assertEqual([e['catalog_id'] for e in cat['events']],
                         ['ce1', 'ce2'])
        cat2 = reconcile_event_catalog(cat, d)
        # 5+5 方案继承两个旧身份，尽管存在单条 10 的边
        turn = transition_of(cat2)
        self.assertEqual(inherited_pairs(turn),
                         [('ce1', 'e1'), ('ce2', 'e2')])
        self.assertEqual(turn['ended'], [])
        self.assertEqual(turn['created'], [])

    def test_tertiary_priority_through_api_with_shuffling(self):
        v1 = stub_plan('v1', [('e1', ['a', 'b']), ('e2', ['c', 'd'])])
        v2 = stub_plan('v2', [('nA', ['a', 'c']), ('nB', ['b', 'd'])],
                       shuffle_seed=3)
        d = StubDirectory(['v1', 'v2'], {'v1': v1, 'v2': v2})
        cat = reconcile_event_catalog(None, d)
        cat2 = reconcile_event_catalog(cat, d)
        # 权重全为 1：ce1 配规范键较小的 ['a','c'] 事件 nA
        self.assertEqual(inherited_pairs(transition_of(cat2)),
                         [('ce1', 'nA'), ('ce2', 'nB')])

    def test_read_plan_called_exactly_once_per_call(self):
        v1 = stub_plan('v1', [('e1', ['a'])])
        d = StubDirectory(['v1'], {'v1': v1})
        reconcile_event_catalog(None, d)
        self.assertEqual(d.read_calls, ['v1'])


class AliasCompatibilityTests(unittest.TestCase):
    """替代命名/变异方式下的兼容性与拒绝鲁棒性。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        ids = ['a', 'b', 'c']
        self.d = create_review(
            Path(self.tmp) / 'alias',
            one_arrival_per_station_input(ids),
            offset_clock('v1', ids, lambda a: False))
        self.d.submit_clock(offset_clock(
            'v2', ids, lambda a: a in ('b', 'c')))
        self.cat = reconcile_event_catalog(None, self.d)

    def tearDown(self):
        self.d.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reference_style_alias_reads(self):
        cat2 = reconcile_event_catalog(copy.deepcopy(self.cat), self.d)
        # 文档规范键
        self.assertIn('lineage', cat2)
        turn = cat2['lineage'][-1]
        # 常见替代键名都能取到同一值
        self.assertEqual(turn['edges'], turn['associations'])
        self.assertEqual(turn['old_version'], turn['from_version'])
        edge = turn['associations'][0]
        self.assertEqual(edge['weight'], edge['shared_count'])
        self.assertEqual(edge['shared'], edge['shared_count'])
        self.assertEqual(edge['is_inherited'], edge['inherited'])
        self.assertEqual(edge['arrival_ids'], edge['shared_arrival_ids'])
        ev = turn['events'][0]
        self.assertEqual(ev['overlapping_catalog_ids'], ev['overlaps'])

    def test_alias_mutation_and_deletion_rejected(self):
        def rejects(mutate):
            state = copy.deepcopy(self.cat)
            mutate(state)
            with self.assertRaises(ValueError):
                reconcile_event_catalog(state, self.d)

        rejects(lambda s: s.update(nextIndex=0))
        rejects(lambda s: s.update(next_sequence='2'))
        rejects(lambda s: s['events'][0].update(catalogEventId='ce9'))
        rejects(lambda s: s['events'][0].update(arrivalIds=['a', 'a']))

        state = copy.deepcopy(self.cat)
        del state['next_sequence']
        with self.assertRaises(ValueError):
            reconcile_event_catalog(state, self.d)

    def test_json_roundtrip_alias_conflict_rejected(self):
        state = json.loads(json.dumps(self.cat))
        state['next_index'] = 0  # 与 next_sequence=2 冲突
        with self.assertRaises(ValueError):
            reconcile_event_catalog(state, self.d)

    def test_semantic_equality_with_reference_literal(self):
        cat2 = reconcile_event_catalog(copy.deepcopy(self.cat), self.d)
        edge = next(e for e in cat2['lineage'][-1]['associations']
                    if e['inherited'])
        # 即使断言方使用另一套键名的精简字面量，语义比较也应通过
        literal = {'old_catalog_id': 'ce1', 'new_event_id': 'e2',
                   'shared_count': 2, 'inherited': True}
        self.assertEqual(edge, literal)
        self.assertEqual(literal, edge)


class RandomPartitionPropertyTests(unittest.TestCase):
    @staticmethod
    def _partition_plan(vid, part):
        return {'version_id': vid, 'events': [
            {'event_id': f'e{i}',
             'arrivals': [{'id': a} for a in group]}
            for i, group in enumerate(part, 1)]}

    def test_random_partitions_keep_identity_invariants(self):
        rng = random.Random(207207)

        class Dir:
            def __init__(self, versions, plans):
                self._versions, self._plans = versions, plans

            def list_versions(self):
                return list(self._versions)

            def read_plan(self, version_id=None):
                return copy.deepcopy(self._plans[version_id])

        for _trial in range(120):
            arrivals = [f'a{i}' for i in range(rng.randint(1, 12))]
            partitions = []
            for _ in range(3):
                perm = arrivals[:]
                rng.shuffle(perm)
                k = rng.randint(1, min(4, len(perm)))
                groups = [[] for _ in range(k)]
                for aid in perm:
                    groups[rng.randrange(k)].append(aid)
                partitions.append([g for g in groups if g])
            versions = ['v1', 'v2', 'v3']
            plans = {v: self._partition_plan(v, p)
                     for v, p in zip(versions, partitions)}
            directory = Dir(versions, plans)
            cat = reconcile_event_catalog(None, directory)
            for vi in (1, 2):
                before = copy.deepcopy(cat)
                nxt = reconcile_event_catalog(cat, directory, versions[vi])
                self.assertEqual(cat, before)  # 入参未被修改
                again = reconcile_event_catalog(cat, directory, versions[vi])
                self.assertEqual(json.dumps(again, sort_keys=True),
                                 json.dumps(nxt, sort_keys=True))
                cat = nxt
                plan_map = {
                    e['event_id']: sorted(a['id'] for a in e['arrivals'])
                    for e in plans[versions[vi]]['events']}
                self.assertEqual(len(cat['events']), len(plan_map))
                self.assertEqual({e['event_id'] for e in cat['events']},
                                 set(plan_map))
                cids = [e['catalog_id'] for e in cat['events']]
                self.assertEqual(len(cids), len(set(cids)))
                for ev in cat['events']:
                    self.assertEqual(ev['canonical_key'],
                                     plan_map[ev['event_id']])
                turn = cat['lineage'][-1]
                inherited = [a for a in turn['associations'] if a['inherited']]
                olds = [a['old_catalog_id'] for a in inherited]
                news = [a['new_event_id'] for a in inherited]
                self.assertEqual(len(olds), len(set(olds)))
                self.assertEqual(len(news), len(set(news)))
                # 每条边的计数与精确共享 ID 自洽
                for edge in turn['associations']:
                    ids = edge['shared_arrival_ids']
                    self.assertEqual(ids, sorted(ids))
                    self.assertEqual(edge['shared_count'], len(ids))
                    self.assertGreaterEqual(edge['shared_count'], 1)


if __name__ == '__main__':
    unittest.main()
