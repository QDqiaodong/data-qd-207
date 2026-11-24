import json
import shutil
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from seismic_assoc import create_review, open_review, process
from seismic_assoc.review import _build_plan  # 测试用：直接构造计划


def base_input(window='1', pre='1', post='2'):
    return {
        'stations': [{'id': 'A', 'lat': '0', 'lon': '0'},
                     {'id': 'B', 'lat': '1', 'lon': '1'}],
        'events': [{'id': 'aP', 'station': 'A', 'phase': 'P', 'time': 10},
                   {'id': 'bP', 'station': 'B', 'phase': 'P', 'time': 10}],
        'window': window,
        'domains': {'A': [0, 20], 'B': [0, 20]},
        'blocks': [{'id': 'X', 'station': 'A', 'interval': [0, 15]},
                   {'id': 'Y', 'station': 'A', 'interval': [15, 20]},
                   {'id': 'Z', 'station': 'B', 'interval': [0, 20]}],
        'pre': pre,
        'post': post,
    }


def clock_v1(vid='v1'):
    return {'version_id': vid, 'stations': {
        'A': [{'interval': [0, 20], 'offset': '0'}],
        'B': [{'interval': [0, 20], 'offset': '0'}]}}


def clock_v2(vid='v2'):
    # 只把 A 的 [15,20) 偏移改为 -6：统一时间在边界发生跳跃
    return {'version_id': vid, 'stations': {
        'A': [{'interval': [0, 15], 'offset': '0'},
              {'interval': [15, 20], 'offset': '-6'}],
        'B': [{'interval': [0, 20], 'offset': '0'}]}}


def ranges(plan, event_id, station):
    event = next(e for e in plan['events'] if e['event_id'] == event_id)
    return [tuple(iv) for iv in event['ranges'][station]]


def sources_for(query, event_id, station):
    event = next(e for e in query['events'] if e['event_id'] == event_id)
    st = next(s for s in event['stations'] if s['station'] == station)
    return [(tuple(p['interval']), p['kind'], p['reference']) for p in st['sources']]


class AcceptanceOne(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dirpath = Path(self.tmp) / 'review'

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_lifecycle(self):
        d = create_review(self.dirpath, base_input(), clock_v1())
        plan1 = d.read_plan()
        self.assertEqual(plan1['version_id'], 'v1')
        self.assertEqual([e['event_id'] for e in plan1['events']], ['e1'])
        self.assertEqual(ranges(plan1, 'e1', 'A'), [(9, 12)])
        self.assertEqual(ranges(plan1, 'e1', 'B'), [(9, 12)])
        # anchor 与统一时间精确输出
        self.assertEqual(plan1['events'][0]['anchor'], '10')
        self.assertEqual({a['time'] for a in plan1['events'][0]['arrivals']}, {'10'})

        # 给 X 登记首版事件的 [9,12) 副本
        rec = d.register_receipt({'receipt_id': 'r1', 'version_id': 'v1',
                                  'event_id': 'e1', 'block_id': 'X',
                                  'interval': [9, 12]})
        self.assertTrue(rec['accepted'])
        # 等价重放
        self.assertEqual(d.register_receipt({'receipt_id': 'r1', 'version_id': 'v1',
                                             'event_id': 'e1', 'block_id': 'X',
                                             'interval': [9, 12]}), rec)
        # 同 ID 不同内容报错且不改目录
        with self.assertRaises(ValueError):
            d.register_receipt({'receipt_id': 'r1', 'version_id': 'v1',
                                'event_id': 'e1', 'block_id': 'X',
                                'interval': [9, 11]})
        self.assertEqual(d.block_status('X'), 'online')

        d.submit_clock(clock_v2())
        plan2 = d.read_plan()
        # A 必须取 [9,12) 与 [15,18)，不取 [12,15)，也不取刻度 18
        self.assertEqual(ranges(plan2, 'e1', 'A'), [(9, 12), (15, 18)])
        self.assertEqual(ranges(plan2, 'e1', 'B'), [(9, 12)])
        # 旧版计划不变
        self.assertEqual(ranges(d.read_plan('v1'), 'e1', 'A'), [(9, 12)])
        # 证据与 anchor 仍是同样的本地到时
        ev2 = plan2['events'][0]
        self.assertEqual(ev2['anchor'], '10')
        self.assertEqual(sorted((a['station'], a['local_time']) for a in ev2['arrivals']),
                         [('A', 10), ('B', 10)])

        # 当前版查询：A 的 [9,12) 用旧 X 副本；[15,18) 仍指向在线 Y
        q = d.query_sources()
        self.assertEqual(sources_for(q, 'e1', 'A'),
                         [((9, 12), 'archive', 'r1'),
                          ((15, 18), 'online', 'Y')])
        self.assertEqual(sources_for(q, 'e1', 'B'), [((9, 12), 'online', 'Z')])

        # Y 尚未满足归档，下线拒绝；被拒绝 ID 可重试
        with self.assertRaises(ValueError):
            d.request_offline({'request_id': 'qY', 'block_id': 'Y'})
        d.register_receipt({'receipt_id': 'r2', 'version_id': 'v2',
                            'event_id': 'e1', 'block_id': 'Y', 'interval': [15, 17]})
        with self.assertRaises(ValueError):
            d.request_offline({'request_id': 'qY', 'block_id': 'Y'})
        d.register_receipt({'receipt_id': 'r3', 'version_id': 'v2',
                            'event_id': 'e1', 'block_id': 'Y', 'interval': [17, 18]})
        ans = d.request_offline({'request_id': 'qY', 'block_id': 'Y'})
        self.assertTrue(ans['approved'])
        self.assertEqual(d.block_status('Y'), 'offline')

        # 重放返回首次结果
        self.assertEqual(d.request_offline({'request_id': 'qY', 'block_id': 'Y'}), ans)

        # 成功后当前回看仍可用，但不再指向 Y 原块
        q = d.query_sources()
        self.assertEqual(sources_for(q, 'e1', 'A'),
                         [((9, 12), 'archive', 'r1'),
                          ((15, 17), 'archive', 'r2'),
                          ((17, 18), 'archive', 'r3')])
        # 已下线块拒绝新回执
        with self.assertRaises(ValueError):
            d.register_receipt({'receipt_id': 'r4', 'version_id': 'v2',
                                'event_id': 'e1', 'block_id': 'Y',
                                'interval': [18, 19]})

        # 重开后状态一致
        d.close()
        d2 = open_review(self.dirpath)
        self.assertEqual(d2.current_version(), 'v2')
        self.assertEqual(sorted(d2.list_versions()), ['v1', 'v2'])
        self.assertEqual(d2.block_status('Y'), 'offline')
        q = d2.query_sources()
        self.assertEqual(sources_for(q, 'e1', 'A'),
                         [((9, 12), 'archive', 'r1'),
                          ((15, 17), 'archive', 'r2'),
                          ((17, 18), 'archive', 'r3')])
        # 重放成功 ID 仍是首次结果
        self.assertEqual(d2.request_offline({'request_id': 'qY', 'block_id': 'Y'}), ans)
        d2.close()


class AcceptanceTwo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_offline_before_later_clock(self):
        path = Path(self.tmp) / 'review2'
        d = create_review(path, base_input(), clock_v1())
        # 首版 Y 没有任何当前需求，可直接批准下线
        ans = d.request_offline({'request_id': 'qY0', 'block_id': 'Y'})
        self.assertTrue(ans['approved'])
        d.submit_clock(clock_v2())
        plan2 = d.read_plan()
        self.assertEqual(ranges(plan2, 'e1', 'A'), [(9, 12), (15, 18)])
        # Y 对应的 [15,18) 必须如实缺失；不能补回执掩盖
        with self.assertRaises(ValueError):
            d.register_receipt({'receipt_id': 'rX', 'version_id': 'v2',
                                'event_id': 'e1', 'block_id': 'Y',
                                'interval': [15, 18]})
        q = d.query_sources()
        self.assertEqual(sources_for(q, 'e1', 'A'),
                         [((9, 12), 'online', 'X'),
                          ((15, 18), 'missing', None)])
        # X 的可取片段不受影响（同站另一块）
        self.assertEqual(sources_for(q, 'e1', 'B'), [((9, 12), 'online', 'Z')])

        # 重放第一次下线请求仍返回原批准，不重新审核
        self.assertEqual(d.request_offline({'request_id': 'qY0', 'block_id': 'Y'}), ans)
        # 更晚版本之后重放旧校时：等价内容只返回首次结果，不切回当前版
        replay = d.submit_clock(clock_v1())
        self.assertEqual(replay['version_id'], 'v1')
        self.assertEqual(d.current_version(), 'v2')
        d.close()

        d2 = open_review(path)
        self.assertEqual(d2.current_version(), 'v2')
        self.assertEqual(d2.block_status('Y'), 'offline')
        d2.close()

    def test_changed_arrival_order_and_boundary(self):
        # C、D 两站；v1 到时顺序 c10,d11（window=1，边界 <= 归并为一组）；
        # v2 把 D 提前到 8，顺序反转并拆成两个事件。
        data = {
            'stations': [{'id': 'C', 'lat': '0', 'lon': '0'},
                         {'id': 'D', 'lat': '1', 'lon': '1'}],
            'events': [{'id': 'c1', 'station': 'C', 'phase': 'P', 'time': 10},
                       {'id': 'd1', 'station': 'D', 'phase': 'P', 'time': 11}],
            'window': '1',
            'domains': {'C': [0, 30], 'D': [0, 30]},
            'blocks': [{'id': 'C1', 'station': 'C', 'interval': [0, 20]},
                       {'id': 'C2', 'station': 'C', 'interval': [20, 30]},
                       {'id': 'D1', 'station': 'D', 'interval': [0, 30]}],
            'pre': '1', 'post': '2',
        }
        v1 = {'version_id': 'k1', 'stations': {
            'C': [{'interval': [0, 30], 'offset': '0'}],
            'D': [{'interval': [0, 30], 'offset': '0'}]}}
        v2 = {'version_id': 'k2', 'stations': {
            'C': [{'interval': [0, 30], 'offset': '0'}],
            'D': [{'interval': [0, 30], 'offset': '-3'}]}}
        path = Path(self.tmp) / 'review3'
        d = create_review(path, data, v1)
        p1 = d.read_plan()
        self.assertEqual([e['event_id'] for e in p1['events']], ['e1'])
        self.assertEqual(ranges(p1, 'e1', 'C'), [(9, 12)])
        self.assertEqual([a['id'] for a in p1['events'][0]['arrivals']], ['c1', 'd1'])
        # 边界归并证据：anchor=10，D 的统一时间 11，差 1 <= window
        self.assertEqual(p1['events'][0]['anchor'], '10')

        d.submit_clock(v2)
        p2 = d.read_plan()
        self.assertEqual([e['event_id'] for e in p2['events']], ['e1', 'e2'])
        first, second = p2['events']
        # 新顺序：D 统一时间 8 在前，anchor=8；统一窗口 [7,10) 经 offset=-3
        # 映回本地刻度为 [10,13)
        self.assertEqual([a['id'] for a in first['arrivals']], ['d1'])
        self.assertEqual(first['anchor'], '8')
        self.assertEqual(ranges(p2, 'e1', 'D'), [(10, 13)])
        self.assertEqual([a['id'] for a in second['arrivals']], ['c1'])
        self.assertEqual(ranges(p2, 'e2', 'C'), [(9, 12)])

        # 历史与当前同号事件范围不同：k1 的 e1 需要 D 的 [9,12)，
        # k2 的 e1 需要 D 的 [10,13)。回执绑定 (版本,事件)，不能互验。
        ok = d.register_receipt({'receipt_id': 'h1', 'version_id': 'k1',
                                 'event_id': 'e1', 'block_id': 'D1',
                                 'interval': [9, 12]})
        self.assertTrue(ok['accepted'])
        # 拿当前同名 e1 替历史事件验收会失败：[9,12) 不是 k2/e1 所需
        with self.assertRaises(ValueError):
            d.register_receipt({'receipt_id': 'h2', 'version_id': 'k2',
                                'event_id': 'e1', 'block_id': 'D1',
                                'interval': [9, 12]})
        # 历史回执只覆盖同块同采样点：k2 的 e1 范围 [10,13) 中，
        # [10,12) 被 h1（历史版 [9,12)）覆盖，[12,13) 仍在线；回执不按事件号扩大
        q = d.query_sources()
        self.assertEqual(sources_for(q, 'e1', 'D'),
                         [((10, 12), 'archive', 'h1'),
                          ((12, 13), 'online', 'D1')])
        self.assertEqual(sources_for(q, 'e2', 'C'), [((9, 12), 'online', 'C1')])
        # 按历史版查询时，h1 完整命中
        q1 = d.query_sources('k1')
        self.assertEqual(sources_for(q1, 'e1', 'D'),
                         [((9, 12), 'archive', 'h1')])
        d.close()

    def test_receipt_coverage_does_not_expand(self):
        path = Path(self.tmp) / 'review4'
        d = create_review(path, base_input(), clock_v1())
        # 部分归档不会扩大覆盖；重叠回执选字典序最小（相邻片段自然合并表达）
        d.register_receipt({'receipt_id': 'ra', 'version_id': 'v1',
                            'event_id': 'e1', 'block_id': 'X', 'interval': [9, 11]})
        d.register_receipt({'receipt_id': 'rb', 'version_id': 'v1',
                            'event_id': 'e1', 'block_id': 'X', 'interval': [10, 12]})
        q = d.query_sources()
        self.assertEqual(sources_for(q, 'e1', 'A'),
                         [((9, 11), 'archive', 'ra'),
                          ((11, 12), 'archive', 'rb')])
        d.close()


class AcceptanceThree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _journal_requests(self, path):
        text = (Path(path) / 'journal.log').read_text('utf-8')
        return [json.loads(line) for line in text.split('\n')
                if line and line != 'COMMITTED']

    def test_crash_before_commit_leaves_no_decision(self):
        path = Path(self.tmp) / 'crash1'
        d = create_review(path, base_input(), clock_v1())
        # v1 下 Y 无需求，直接申请下线，在 COMMITTED 前注入中断
        d._fault = 'crash_before_commit'
        with self.assertRaises(RuntimeError):
            d.request_offline({'request_id': 'qCrash', 'block_id': 'Y'})
        d.close()
        # 重开：块仍在线，且没有留下成功请求
        d2 = open_review(path)
        self.assertEqual(d2.block_status('Y'), 'online')
        self.assertNotIn('Y', d2._offline)
        self.assertNotIn(('offline', 'qCrash'), d2._responses)
        # 同一请求 ID 可重新提交并成功
        ans = d2.request_offline({'request_id': 'qCrash', 'block_id': 'Y'})
        self.assertTrue(ans['approved'])
        self.assertEqual(d2.block_status('Y'), 'offline')
        d2.close()
        entries = self._journal_requests(path)
        self.assertTrue(all(e['id'] != 'qCrash' or e['response']['approved']
                            for e in entries))

    def test_commit_succeeds_but_caller_loses_response(self):
        path = Path(self.tmp) / 'crash2'
        d = create_review(path, base_input(), clock_v1())
        d._fault = 'crash_after_commit'
        with self.assertRaises(RuntimeError):
            d.request_offline({'request_id': 'qLost', 'block_id': 'Y'})
        d.close()
        # 重开重试：只能取回同一个原批准决定，不产生第二条
        d2 = open_review(path)
        self.assertEqual(d2.block_status('Y'), 'offline')
        ans = d2.request_offline({'request_id': 'qLost', 'block_id': 'Y'})
        self.assertTrue(ans['approved'])
        d2.close()
        entries = [e for e in self._journal_requests(path) if e['id'] == 'qLost']
        self.assertEqual(len(entries), 1)

    def test_state_file_missing_rebuilds_from_journal(self):
        path = Path(self.tmp) / 'crash3'
        d = create_review(path, base_input(), clock_v1())
        d.request_offline({'request_id': 'qOk', 'block_id': 'Y'})
        d.close()
        (Path(path) / 'state.json').unlink()
        d2 = open_review(path)
        self.assertEqual(d2.block_status('Y'), 'offline')
        self.assertTrue(Path(path / 'state.json').exists())
        d2.close()


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_integer_boundaries_reject_bool(self):
        data = base_input()
        data['events'][0]['time'] = True
        with self.assertRaises(ValueError):
            create_review(Path(self.tmp) / 'b1', data, clock_v1())
        data = base_input()
        data['blocks'][0]['interval'] = [0, True]
        with self.assertRaises(ValueError):
            create_review(Path(self.tmp) / 'b2', data, clock_v1())

    def test_decimal_arrival_still_rejected_here(self):
        data = base_input()
        data['events'][0]['time'] = '10.5'
        with self.assertRaises(ValueError):
            create_review(Path(self.tmp) / 'b3', data, clock_v1())

    def test_bad_clock_segments(self):
        path = Path(self.tmp) / 'clockbad'
        d = create_review(path, base_input(), clock_v1())
        # 有空洞
        bad_gap = {'version_id': 'bad1', 'stations': {
            'A': [{'interval': [0, 14], 'offset': '0'},
                  {'interval': [15, 20], 'offset': '-6'}],
            'B': [{'interval': [0, 20], 'offset': '0'}]}}
        with self.assertRaises(ValueError):
            d.submit_clock(bad_gap)
        # 重叠
        bad_overlap = {'version_id': 'bad2', 'stations': {
            'A': [{'interval': [0, 16], 'offset': '0'},
                  {'interval': [15, 20], 'offset': '-6'}],
            'B': [{'interval': [0, 20], 'offset': '0'}]}}
        with self.assertRaises(ValueError):
            d.submit_clock(bad_overlap)
        # 未覆盖全站
        bad_missing = {'version_id': 'bad3', 'stations': {
            'A': [{'interval': [0, 20], 'offset': '0'}]}}
        with self.assertRaises(ValueError):
            d.submit_clock(bad_missing)
        # 非法版本仍可重试同 ID（未占用）
        ok = d.submit_clock({'version_id': 'bad1', 'stations': {
            'A': [{'interval': [0, 20], 'offset': '0'}],
            'B': [{'interval': [0, 20], 'offset': '0'}]}})
        self.assertEqual(ok['version_id'], 'bad1')
        # 同 ID 不同内容冲突
        with self.assertRaises(ValueError):
            d.submit_clock(clock_v2('bad1'))
        d.close()

    def test_block_holes_are_missing(self):
        data = base_input()
        # A 只保留 X=[0,15)，[15,20) 成为采样域内空洞
        data['blocks'] = [b for b in data['blocks'] if b['id'] != 'Y']
        path = Path(self.tmp) / 'holes'
        d = create_review(path, data, clock_v1())
        d.submit_clock(clock_v2())
        q = d.query_sources()
        self.assertEqual(sources_for(q, 'e1', 'A'),
                         [((9, 12), 'online', 'X'),
                          ((15, 18), 'missing', None)])
        d.close()

    def test_receipt_business_rules(self):
        path = Path(self.tmp) / 'rules'
        d = create_review(path, base_input(), clock_v1())
        # 区间不属于块
        with self.assertRaises(ValueError):
            d.register_receipt({'receipt_id': 's1', 'version_id': 'v1',
                                'event_id': 'e1', 'block_id': 'X',
                                'interval': [14, 16]})
        # 不是该事件该站所需
        with self.assertRaises(ValueError):
            d.register_receipt({'receipt_id': 's2', 'version_id': 'v1',
                                'event_id': 'e1', 'block_id': 'X',
                                'interval': [0, 3]})
        # 空区间
        with self.assertRaises(ValueError):
            d.register_receipt({'receipt_id': 's3', 'version_id': 'v1',
                                'event_id': 'e1', 'block_id': 'X',
                                'interval': [5, 5]})
        # 未知块/版本/事件
        with self.assertRaises(ValueError):
            d.register_receipt({'receipt_id': 's4', 'version_id': 'v1',
                                'event_id': 'e1', 'block_id': 'NOPE',
                                'interval': [9, 10]})
        with self.assertRaises(ValueError):
            d.register_receipt({'receipt_id': 's5', 'version_id': 'nope',
                                'event_id': 'e1', 'block_id': 'X',
                                'interval': [9, 10]})
        with self.assertRaises(ValueError):
            d.register_receipt({'receipt_id': 's6', 'version_id': 'v1',
                                'event_id': 'e9', 'block_id': 'X',
                                'interval': [9, 10]})
        d.close()

    def test_overlapping_blocks_rejected(self):
        data = base_input()
        data['blocks'].append({'id': 'W', 'station': 'A', 'interval': [14, 16]})
        with self.assertRaises(ValueError):
            create_review(Path(self.tmp) / 'ov', data, clock_v1())

    def test_equivalent_clock_inputs_replay_as_same(self):
        path = Path(self.tmp) / 'equiv'
        d = create_review(path, base_input(), clock_v1())
        # 等值十进制的不同写法、分段与台站的不同输入顺序都不构成冲突
        reordered = {'version_id': 'v2', 'stations': {
            'B': [{'interval': [0, 20], 'offset': '0.0'}],
            'A': [{'interval': [15, 20], 'offset': '-6.0'},
                  {'interval': [0, 15], 'offset': '0'}]}}
        d.submit_clock(reordered)
        canonical_form = clock_v2()
        # 与规范化后的同内容重放等价，返回首次应答且不新增版本
        replay = d.submit_clock(canonical_form)
        self.assertEqual(replay['version_id'], 'v2')
        self.assertEqual(d.list_versions(), ['v1', 'v2'])
        d.close()

    def test_reopen_existing_path_errors(self):
        path = Path(self.tmp) / 'twice'
        d = create_review(path, base_input(), clock_v1())
        d.close()
        with self.assertRaises(ValueError):
            create_review(path, base_input(), clock_v1())

    def test_decimal_precision_not_snapped(self):
        # 小数 offset 产生非整数统一时间，归并与取样边界按精确十进制处理
        data = base_input(window='0.5')
        v = {'version_id': 'frac', 'stations': {
            'A': [{'interval': [0, 20], 'offset': '0.25'}],
            'B': [{'interval': [0, 20], 'offset': '0'}]}}
        d = create_review(Path(self.tmp) / 'frac', data, v)
        p = d.read_plan()
        # A 统一 10.25、B 统一 10，差 0.25 <= 0.5 仍同组；anchor=10
        self.assertEqual(len(p['events']), 1)
        self.assertEqual(p['events'][0]['anchor'], '10')
        a_arr = next(a for a in p['events'][0]['arrivals'] if a['station'] == 'A')
        self.assertEqual(a_arr['time'], '10.25')  # 精确十进制，不舍入到采样点
        # A: [10-1-0.25, 10+2-0.25)=[8.75,11.75) -> 整数点 9..11
        self.assertEqual(ranges(p, 'e1', 'A'), [(9, 12)])
        d.close()

    def test_build_plan_helper_math(self):
        # 直接验证负偏移段的 ceil 边界：窗口统一时间 [4,7)，两段均命中
        catalog = {'window': Fraction(1), 'pre': Fraction(1), 'post': Fraction(2),
                   'arrivals': [('a', 'S', 'P', 5)], 'domains': {'S': [0, 20]}}
        clocks = {'S': [(0, 7, 0), (7, 20, -3)]}
        plan = _build_plan('t', catalog, clocks)
        # 本地 t=5 -> 统一 5；段一：t in [4,7)；
        # 段二：u=t-3 in [4,7) -> t in [7,10)，两段各自保留、不填中间点
        self.assertEqual([tuple(iv) for iv in plan['events'][0]['ranges']['S']],
                         [(4, 7), (7, 10)])


class LegacyRegression(unittest.TestCase):
    """旧 process 与 JSON 接口保持不变，包括小数到时。"""

    def test_legacy_process_unchanged(self):
        data = json.loads(Path('fixtures/input.json').read_text())
        r = process(data)
        self.assertEqual(r['summary'],
                         {'arrivals': 3, 'events': 2, 'multi_station': 1})
        data['events'][0]['time'] = '10.25'
        self.assertEqual(process(data)['events'][0]['anchor'], '41/4')

    def test_legacy_rejects_bad_decimal(self):
        data = json.loads(Path('fixtures/input.json').read_text())
        data['events'][0]['time'] = 10  # 旧接口仍要求十进制字符串
        with self.assertRaises(ValueError):
            process(data)


if __name__ == '__main__':
    unittest.main()
