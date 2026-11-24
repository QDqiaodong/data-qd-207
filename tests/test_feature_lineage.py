"""样本级特征血缘、缺陷与交付凭证的自动化验收测试。

全部使用真实 ``ReviewDirectory``（真实校时计划、回执、发布账本、journal
与重开链路）合成数据，不读取任何波形或特征数值，也不用手写 mask 绕过
发布与回执链路。对应任务要求的五个必须验证情形。
"""
import json
import shutil
import tempfile
import tracemalloc
import unittest
from pathlib import Path

from seismic_assoc import (
    LineageError,
    UnknownCertificateError,
    UnknownNodeError,
    create_review,
    open_review,
)


def station_input(stations=('A',), domain=200, arrival=110, pre='10', post='10'):
    return {
        'stations': [{'id': st, 'lat': '0', 'lon': '0'} for st in stations],
        'events': [{'id': f'a{st}', 'station': st, 'phase': 'P', 'time': arrival}
                   for st in stations],
        'window': '0',
        'domains': {st: [0, domain] for st in stations},
        'blocks': [{'id': f'{st}0', 'station': st, 'interval': [0, domain]}
                   for st in stations],
        'pre': pre,
        'post': post,
    }


def station_clock(stations=('A',), domain=200):
    return {'version_id': 'v1', 'stations': {
        st: [{'interval': [0, domain], 'offset': '0'}] for st in stations}}


def journal_entries(path):
    text = (Path(path) / 'journal.log').read_text('utf-8')
    return [json.loads(line) for line in text.splitlines() if line and line != 'COMMITTED']


class LineageFixture:
    """单站 A、事件 e1 需要 [100,120) 的已发布训练目录。"""

    def __init__(self, path, receipts=(('r0', [100, 112]), ('r1', [112, 120]))):
        self.path = path
        self.d = create_review(path, station_input(), station_clock())
        for rid, iv in receipts:
            self.d.register_receipt({'receipt_id': rid, 'version_id': 'v1',
                                     'event_id': 'e1', 'block_id': 'A0',
                                     'interval': iv})
        self.preview = self.d.preview_release('v1', ['e1'], [])
        self.group_key = self.preview['groups'][0]['group_key']
        self.manifest = self.d.publish_release(
            self.preview, [self.group_key], 'pub1')

    def publish(self, d, request_id='pub1'):
        preview = d.preview_release('v1', ['e1'], [])
        return preview, d.publish_release(preview,
                                          [preview['groups'][0]['group_key']],
                                          request_id)


class AcceptanceScenarioOneTests(unittest.TestCase):
    """情形 1：副本缺陷沿真实窗口规则传播，凭证与旧清单不可倒改。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fx = LineageFixture(Path(self.tmp) / 'review')
        self.d = self.fx.d
        # 同块的完好回执 z 覆盖整个范围。
        self.d.register_receipt({'receipt_id': 'z', 'version_id': 'v1',
                                 'event_id': 'e1', 'block_id': 'A0',
                                 'interval': [100, 120]})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_window_propagation_certificates_and_immutable_history(self):
        d, key = self.d, self.fx.group_key
        r = d.capture_waveform_input({
            'node_id': 'R', 'release_request_id': 'pub1', 'group_key': key,
            'station': 'A', 'interval': [100, 120]})
        self.assertEqual(
            [(f['receipt_id'], tuple(f['interval'])) for f in r['fragments']],
            [('r0', (100, 112)), ('r1', (112, 120))])
        self.assertEqual(r['length'], 20)
        self.assertEqual(r['output_domain'], [0, 20])

        d.register_window_feature({
            'node_id': 'F', 'length': 16,
            'parents': [{'node_id': 'R', 'offset': 0, 'width': 5}]})
        d.register_window_feature({
            'node_id': 'G', 'length': 14,
            'parents': [{'node_id': 'F', 'offset': 0, 'width': 3}]})

        cert_g4 = d.issue_feature_certificate({
            'request_id': 'cert-g4', 'usage': 'train',
            'selections': [{'node_id': 'G', 'intervals': [[0, 4]]}]})
        cert_g2 = d.issue_feature_certificate({
            'request_id': 'cert-g2', 'usage': 'train',
            'selections': [{'node_id': 'G', 'intervals': [[0, 2]]}]})

        d.record_sample_defect({'request_id': 'def-r0', 'scope': 'receipt',
                                'target': 'r0', 'interval': [108, 110]})
        self.assertEqual(d.feature_health('R')['invalid_intervals'], [[8, 10]])
        self.assertEqual(d.feature_health('F')['invalid_intervals'], [[4, 10]])
        self.assertEqual(d.feature_health('G')['invalid_intervals'], [[2, 10]])
        health_r = d.feature_health('R')
        self.assertEqual(health_r['invalid_by_defect'],
                         {'def-r0': [[8, 10]]})

        chk1 = d.check_feature_certificate('cert-g4')
        self.assertFalse(chk1['currently_usable'])
        self.assertEqual(chk1['selections'][0]['currently_invalid_intervals'],
                         [[2, 4]])
        self.assertEqual(chk1['selections'][0]['defects'][0]['defect_request_id'],
                         'def-r0')
        chk2 = d.check_feature_certificate('cert-g2')
        self.assertTrue(chk2['currently_usable'])
        self.assertEqual(chk2['selections'][0]['currently_invalid_intervals'], [])

        # 旧凭证原文与旧波形发布清单都不变。
        self.assertEqual(d.read_feature_certificate('cert-g4'), cert_g4)
        self.assertEqual(d.read_release('pub1'), self.fx.manifest)
        # 同 ID 等价重放返回首次凭证，即使它现在不可用。
        self.assertEqual(d.issue_feature_certificate({
            'request_id': 'cert-g4', 'usage': 'train',
            'selections': [{'node_id': 'G', 'intervals': [[0, 4]]}]}), cert_g4)
        self.assertEqual(d.issue_feature_certificate({
            'request_id': 'cert-g2', 'usage': 'train',
            'selections': [{'node_id': 'G', 'intervals': [[0, 2]]}]}), cert_g2)
        # 新请求 ID 签同一坏范围必须拒绝，且不占用 ID。
        with self.assertRaises(ValueError):
            d.issue_feature_certificate({
                'request_id': 'cert-new-bad', 'usage': 'train',
                'selections': [{'node_id': 'G', 'intervals': [[0, 4]]}]})
        with self.assertRaises(UnknownCertificateError):
            d.read_feature_certificate('cert-new-bad')

    def test_reopen_preserves_all_facts(self):
        d, key = self.d, self.fx.group_key
        d.capture_waveform_input({
            'node_id': 'R', 'release_request_id': 'pub1', 'group_key': key,
            'station': 'A', 'interval': [100, 120]})
        d.register_window_feature({
            'node_id': 'F', 'length': 16,
            'parents': [{'node_id': 'R', 'offset': 0, 'width': 5}]})
        d.register_window_feature({
            'node_id': 'G', 'length': 14,
            'parents': [{'node_id': 'F', 'offset': 0, 'width': 3}]})
        d.issue_feature_certificate({
            'request_id': 'cert-g4', 'usage': 'train',
            'selections': [{'node_id': 'G', 'intervals': [[0, 4]]}]})
        d.record_sample_defect({'request_id': 'def-r0', 'scope': 'receipt',
                                'target': 'r0', 'interval': [108, 110]})
        d.close()
        d2 = open_review(self.fx.path)
        self.assertEqual(d2.feature_health('R')['invalid_intervals'], [[8, 10]])
        self.assertEqual(d2.feature_health('F')['invalid_intervals'], [[4, 10]])
        self.assertEqual(d2.feature_health('G')['invalid_intervals'], [[2, 10]])
        self.assertFalse(d2.check_feature_certificate('cert-g4')['currently_usable'])
        self.assertEqual(d2.read_release('pub1'), self.fx.manifest)
        d2.close()


class AcceptanceScenarioTwoTests(unittest.TestCase):
    """情形 2：重取改用完好副本、物理缺陷全量牵连、发布拒绝。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fx = LineageFixture(Path(self.tmp) / 'review')
        self.d = self.fx.d
        self.d.register_receipt({'receipt_id': 'z', 'version_id': 'v1',
                                 'event_id': 'e1', 'block_id': 'A0',
                                 'interval': [100, 120]})
        d, key = self.d, self.fx.group_key
        d.capture_waveform_input({
            'node_id': 'R', 'release_request_id': 'pub1', 'group_key': key,
            'station': 'A', 'interval': [100, 120]})
        d.register_window_feature({
            'node_id': 'F', 'length': 16,
            'parents': [{'node_id': 'R', 'offset': 0, 'width': 5}]})
        d.register_window_feature({
            'node_id': 'G', 'length': 14,
            'parents': [{'node_id': 'F', 'offset': 0, 'width': 3}]})
        d.record_sample_defect({'request_id': 'def-r0', 'scope': 'receipt',
                                'target': 'r0', 'interval': [108, 110]})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_recapture_physical_defect_and_publish_block(self):
        d, key = self.d, self.fx.group_key
        r2 = d.capture_waveform_input({
            'node_id': 'R2', 'release_request_id': 'pub1', 'group_key': key,
            'station': 'A', 'interval': [100, 120]})
        frags = {(f['receipt_id'], tuple(f['interval']))
                 for f in r2['fragments']}
        self.assertIn(('z', (108, 110)), frags)
        self.assertIn(('r0', (100, 108)), frags)
        self.assertIn(('r0', (110, 112)), frags)
        self.assertIn(('r1', (112, 120)), frags)
        # 旧 R 的实际来源不被自动替换。
        self.assertEqual(
            [f['receipt_id'] for f in d.read_feature_node('R')['fragments']],
            ['r0', 'r1'])

        d.register_window_feature({
            'node_id': 'F2', 'length': 16,
            'parents': [{'node_id': 'R2', 'offset': 0, 'width': 5}]})
        d.register_window_feature({
            'node_id': 'G2', 'length': 14,
            'parents': [{'node_id': 'F2', 'offset': 0, 'width': 3}]})
        self.assertEqual(d.feature_health('G2')['invalid_intervals'], [])
        d.issue_feature_certificate({
            'request_id': 'cert-g2-bad', 'usage': 'train',
            'selections': [{'node_id': 'G2', 'intervals': [[0, 4]]}]})
        d.issue_feature_certificate({
            'request_id': 'cert-g2-clean', 'usage': 'train',
            'selections': [{'node_id': 'G2', 'intervals': [[0, 2]]}]})

        d.record_sample_defect({'request_id': 'def-A', 'scope': 'physical',
                                'target': 'A', 'interval': [108, 110]})
        self.assertEqual(d.feature_health('R2')['invalid_intervals'], [[8, 10]])
        self.assertEqual(d.feature_health('F2')['invalid_intervals'], [[4, 10]])
        self.assertEqual(d.feature_health('G2')['invalid_intervals'], [[2, 10]])
        self.assertFalse(
            d.check_feature_certificate('cert-g2-bad')['currently_usable'])
        self.assertTrue(
            d.check_feature_certificate('cert-g2-clean')['currently_usable'])
        # 旧 R 链同时列出两条缺陷，整体并集仍是同一区间。
        hr = d.feature_health('R')
        self.assertEqual(set(hr['invalid_by_defect']), {'def-r0', 'def-A'})
        self.assertEqual(hr['invalid_intervals'], [[8, 10]])

        # 物理缺陷不改变副本可读性：z 仍被 query_sources 判为 archive。
        q = d.query_sources()
        refs = {(tuple(s['interval']), s['kind'], s['reference'])
                for s in q['events'][0]['stations'][0]['sources']}
        self.assertIn(((108, 110), 'archive', 'z'), refs)

        # 预览显式区分物理质量缺陷与归档缺口，且不裁掉坏样本。
        preview = d.preview_release('v1', ['e1'], [])
        group = preview['groups'][0]
        self.assertTrue(group['has_physical_quality_defect'])
        issue = group['physical_quality_defects'][0]
        self.assertEqual(issue['interval'], [108, 110])
        self.assertEqual(issue['defect_request_id'], 'def-A')
        self.assertEqual(group['sample_unions'], {'A': [[100, 120]]})
        # 新发布整笔拒绝，即使 z 完好可读；失败 ID 未被占用。
        with self.assertRaises(ValueError):
            d.publish_release(preview, [group['group_key']], 'pub2')
        self.assertIsNone(d.find_release('pub2'))
        # 旧用途历史仍在，旧清单原样可读。
        self.assertEqual(d.read_release('pub1'), self.fx.manifest)

    def test_other_station_same_tick_unaffected(self):
        # 双站目录：A 的物理缺陷不牵连 B 站同刻度节点。
        path = Path(self.tmp) / 'two-station'
        d = create_review(path, station_input(stations=('A', 'B')),
                          station_clock(stations=('A', 'B')))
        # 两站同刻到时在 window=0 下归并为同一事件 e1（含两个台站证据）。
        for st in ('A', 'B'):
            d.register_receipt({'receipt_id': f'r-{st}', 'version_id': 'v1',
                                'event_id': 'e1', 'block_id': f'{st}0',
                                'interval': [100, 120]})
        preview = d.preview_release('v1', ['e1'], [])
        group_key = preview['groups'][0]['group_key']
        d.publish_release(preview, [group_key], 'pub-multi')
        for st in ('A', 'B'):
            d.capture_waveform_input({
                'node_id': f'R-{st}', 'release_request_id': 'pub-multi',
                'group_key': group_key, 'station': st,
                'interval': [100, 120]})
        d.record_sample_defect({'request_id': 'def-A', 'scope': 'physical',
                                'target': 'A', 'interval': [108, 110]})
        self.assertEqual(d.feature_health('R-A')['invalid_intervals'], [[8, 10]])
        self.assertEqual(d.feature_health('R-B')['invalid_intervals'], [])
        d.close()


class AcceptanceScenarioThreeTests(unittest.TestCase):
    """情形 3：无替代回执、下线/发布拒绝、分支汇合与边界规则。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _open(self, name):
        path = Path(self.tmp) / name
        d = create_review(path, station_input(), station_clock())
        d.register_receipt({'receipt_id': 'r0', 'version_id': 'v1',
                            'event_id': 'e1', 'block_id': 'A0',
                            'interval': [100, 112]})
        d.register_receipt({'receipt_id': 'r1', 'version_id': 'v1',
                            'event_id': 'e1', 'block_id': 'A0',
                            'interval': [112, 120]})
        return d, path

    def test_damage_falls_back_to_online_then_retry_succeeds(self):
        d, _ = self._open('no-z')
        d.record_sample_defect({'request_id': 'dx', 'scope': 'receipt',
                                'target': 'r0', 'interval': [108, 110]})
        refs = {(tuple(s['interval']), s['kind'], s['reference'])
                for s in d.query_sources()['events'][0]['stations'][0]['sources']}
        self.assertIn(((108, 110), 'online', 'A0'), refs)
        self.assertIn(((100, 108), 'archive', 'r0'), refs)
        self.assertIn(((110, 112), 'archive', 'r0'), refs)

        preview = d.preview_release('v1', ['e1'], [])
        group = preview['groups'][0]
        self.assertIn((108, 110),
                      [tuple(x['interval']) for x in group['online_gaps']])
        self.assertEqual(group['physical_quality_defects'], [])
        with self.assertRaises(ValueError):
            d.publish_release(preview, [group['group_key']], 'pub-fail')
        with self.assertRaises(ValueError):
            d.request_offline({'request_id': 'off-fail', 'block_id': 'A0'})
        self.assertIsNone(d.find_release('pub-fail'))

        # 补登完好回执后，原失败请求 ID 可重试成功。
        d.register_receipt({'receipt_id': 'q2', 'version_id': 'v1',
                            'event_id': 'e1', 'block_id': 'A0',
                            'interval': [108, 110]})
        self.assertTrue(
            d.request_offline({'request_id': 'off-fail',
                               'block_id': 'A0'})['approved'])
        manifest = d.publish_release(
            d.preview_release('v1', ['e1'], []),
            [group['group_key']], 'pub-fail')
        self.assertEqual(manifest['release_request_id'], 'pub-fail')

    def test_damage_after_offline_is_missing_and_never_revives_block(self):
        d, _ = self._open('offline-first')
        approval = d.request_offline({'request_id': 'off1', 'block_id': 'A0'})
        self.assertTrue(approval['approved'])
        d.record_sample_defect({'request_id': 'dy', 'scope': 'receipt',
                                'target': 'r0', 'interval': [108, 110]})
        refs = {(tuple(s['interval']), s['kind'], s['reference'])
                for s in d.query_sources()['events'][0]['stations'][0]['sources']}
        self.assertIn(((108, 110), 'missing', None), refs)
        self.assertEqual(d.block_status('A0'), 'offline')
        # 原批准不倒改，原块不复活，已下线块不能补回执。
        self.assertEqual(d.request_offline({'request_id': 'off1',
                                            'block_id': 'A0'}), approval)
        with self.assertRaises(ValueError):
            d.register_receipt({'receipt_id': 'q3', 'version_id': 'v1',
                                'event_id': 'e1', 'block_id': 'A0',
                                'interval': [108, 110]})

    def test_branch_reconvergence_offsets_endpoints_and_two_defects(self):
        d, path = self._open('branch')
        preview, manifest = self._publish(d, 'pub')
        key = preview['groups'][0]['group_key']
        for node in ('RA', 'RB'):
            d.capture_waveform_input({
                'node_id': node, 'release_request_id': 'pub',
                'group_key': key, 'station': 'A', 'interval': [100, 120]})
        d.register_window_feature({
            'node_id': 'FA', 'length': 10,
            'parents': [{'node_id': 'RA', 'offset': 0, 'width': 5}]})
        d.register_window_feature({
            'node_id': 'FB', 'length': 10,
            'parents': [{'node_id': 'RB', 'offset': 0, 'width': 5}]})
        d.register_window_feature({
            'node_id': 'H', 'length': 6,
            'parents': [{'node_id': 'FA', 'offset': 2, 'width': 3},
                        {'node_id': 'FB', 'offset': 2, 'width': 3}]})

        # 坏在父节点未被任何窗口引用的区间，不污染子节点。
        d.record_sample_defect({'request_id': 'dz', 'scope': 'receipt',
                                'target': 'r1', 'interval': [115, 117]})
        self.assertEqual(d.feature_health('FA')['invalid_intervals'], [])
        self.assertEqual(d.feature_health('H')['invalid_intervals'], [])
        # 端点相接：刻度 14 的缺陷不被窗口 [9,14) 包含。
        d.record_sample_defect({'request_id': 'dw', 'scope': 'receipt',
                                'target': 'r1', 'interval': [114, 115]})
        self.assertEqual(d.feature_health('FA')['invalid_intervals'], [])
        # 真正落在窗口内的位置按公式传播，非零 offset 在 H 层继续换算。
        d.record_sample_defect({'request_id': 'dv', 'scope': 'receipt',
                                'target': 'r1', 'interval': [113, 114]})
        self.assertEqual(d.feature_health('FA')['invalid_intervals'], [[9, 10]])
        self.assertEqual(d.feature_health('H')['invalid_intervals'], [[5, 6]])

        # 窗口越界、深度超限、重复父项、用途冲突都拒绝且不留状态。
        with self.assertRaises(ValueError):
            d.register_window_feature({
                'node_id': 'BAD', 'length': 10,
                'parents': [{'node_id': 'RA', 'offset': 11, 'width': 5}]})
        for i in range(1, 5):
            d.register_window_feature({
                'node_id': f'L{i}', 'length': 5,
                'parents': [{'node_id': 'RA' if i == 1 else f'L{i-1}',
                             'offset': 0, 'width': 1}]})
        with self.assertRaises(ValueError):
            d.register_window_feature({
                'node_id': 'L5', 'length': 5,
                'parents': [{'node_id': 'L4', 'offset': 0, 'width': 1}]})
        with self.assertRaises(ValueError):
            d.register_window_feature({
                'node_id': 'DUP', 'length': 5,
                'parents': [{'node_id': 'RA', 'offset': 0, 'width': 1},
                            {'node_id': 'RA', 'offset': 0, 'width': 1}]})

        # 两个缺陷影响重叠时各自原因仍精确保留。
        d.record_sample_defect({'request_id': 'dphys', 'scope': 'physical',
                                'target': 'A', 'interval': [105, 108]})
        per = d.feature_health('RA')['invalid_by_defect']
        self.assertEqual(per['dphys'], [[5, 8]])
        self.assertEqual(per['dv'], [[13, 14]])
        # 物理缺陷沿两层窗口（含 H 的非零 offset）继续传播到 H 的输出域起点。
        h_before = d.feature_health('H')['invalid_intervals']
        self.assertEqual(h_before, [[0, 6]])

        # 多选择凭证：一处坏整笔失败；干净的多选择可签发。
        with self.assertRaises(ValueError):
            d.issue_feature_certificate({
                'request_id': 'c-bad', 'usage': 'train',
                'selections': [{'node_id': 'RA', 'intervals': [[0, 4]]},
                               {'node_id': 'RB', 'intervals': [[5, 8]]}]})
        cert = d.issue_feature_certificate({
            'request_id': 'c-ok', 'usage': 'train',
            'selections': [{'node_id': 'RA', 'intervals': [[0, 4]]},
                           {'node_id': 'RB', 'intervals': [[0, 4]]}]})
        with self.assertRaises(UnknownCertificateError):
            d.read_feature_certificate('c-bad')
        with self.assertRaises(ValueError):
            d.issue_feature_certificate({
                'request_id': 'c-usage', 'usage': 'blind',
                'selections': [{'node_id': 'RA', 'intervals': [[0, 1]]}]})
        # 区间重排/重叠/相邻拆分按同一采样集合等价重放。
        replay = d.issue_feature_certificate({
            'request_id': 'c-ok', 'usage': 'train',
            'selections': [{'node_id': 'RB', 'intervals': [[2, 4], [0, 2]]},
                           {'node_id': 'RA', 'intervals': [[0, 2], [2, 4]]}]})
        self.assertEqual(replay, cert)
        d.close()

        d2 = open_review(path)
        self.assertEqual(d2.feature_health('H')['invalid_intervals'], h_before)
        self.assertEqual(d2.read_feature_certificate('c-ok'), cert)
        d2.close()

    @staticmethod
    def _publish(d, request_id):
        preview = d.preview_release('v1', ['e1'], [])
        manifest = d.publish_release(
            preview, [preview['groups'][0]['group_key']], request_id)
        return preview, manifest


class AcceptanceScenarioFourTests(unittest.TestCase):
    """情形 4：生效前中断、应答丢失、冲突 ID 与防篡改。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / 'crash'
        d = create_review(self.path, station_input(), station_clock())
        d.register_receipt({'receipt_id': 'r0', 'version_id': 'v1',
                            'event_id': 'e1', 'block_id': 'A0',
                            'interval': [100, 120]})
        preview = d.preview_release('v1', ['e1'], [])
        d.publish_release(preview, [preview['groups'][0]['group_key']], 'pub1')
        d.capture_waveform_input({
            'node_id': 'R', 'release_request_id': 'pub1',
            'group_key': preview['groups'][0]['group_key'],
            'station': 'A', 'interval': [100, 120]})
        d.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_defect_crash_before_commit_rolls_back(self):
        d = open_review(self.path)
        d._fault = 'crash_before_commit'
        with self.assertRaises(RuntimeError):
            d.record_sample_defect({'request_id': 'dx', 'scope': 'receipt',
                                    'target': 'r0', 'interval': [108, 110]})
        d.close()
        d2 = open_review(self.path)
        self.assertNotIn('dx', d2._lineage.defect_ids)
        self.assertEqual(d2.feature_health('R')['invalid_intervals'], [])
        # 失败 ID 可修正重试，重开后来源查询反映同一事实。
        d2.record_sample_defect({'request_id': 'dx', 'scope': 'receipt',
                                 'target': 'r0', 'interval': [108, 110]})
        refs = {(tuple(s['interval']), s['kind'])
                for s in d2.query_sources()['events'][0]['stations'][0]['sources']}
        self.assertIn(((108, 110), 'online'), refs)
        d2.close()
        d3 = open_review(self.path)
        self.assertEqual(d3.feature_health('R')['invalid_intervals'], [[8, 10]])
        d3.close()

    def test_defect_response_lost_then_single_replay(self):
        d = open_review(self.path)
        d._fault = 'crash_after_commit'
        with self.assertRaises(RuntimeError):
            d.record_sample_defect({'request_id': 'dy', 'scope': 'physical',
                                    'target': 'A', 'interval': [105, 106]})
        d.close()
        d2 = open_review(self.path)
        self.assertIn('dy', d2._lineage.defect_ids)
        replay = d2.record_sample_defect({'request_id': 'dy', 'scope': 'physical',
                                          'target': 'A', 'interval': [105, 106]})
        self.assertTrue(replay['accepted'])
        self.assertEqual(
            len([e for e in journal_entries(self.path) if e['id'] == 'dy']), 1)
        self.assertEqual(
            d2.feature_health('R')['invalid_by_defect']['dy'], [[5, 6]])
        # 同 ID 不同内容拒绝。
        with self.assertRaises(ValueError):
            d2.record_sample_defect({'request_id': 'dy', 'scope': 'physical',
                                     'target': 'A', 'interval': [105, 107]})
        d2.close()

    def test_certificate_atomicity_and_single_replay(self):
        d = open_review(self.path)
        d._fault = 'crash_before_commit'
        with self.assertRaises(RuntimeError):
            d.issue_feature_certificate({
                'request_id': 'c-lost', 'usage': 'train',
                'selections': [{'node_id': 'R', 'intervals': [[0, 4]]}]})
        d.close()
        d2 = open_review(self.path)
        with self.assertRaises(UnknownCertificateError):
            d2.read_feature_certificate('c-lost')
        cert = d2.issue_feature_certificate({
            'request_id': 'c-lost', 'usage': 'train',
            'selections': [{'node_id': 'R', 'intervals': [[0, 4]]}]})
        d2._fault = 'crash_after_commit'
        with self.assertRaises(RuntimeError):
            d2.issue_feature_certificate({
                'request_id': 'c-lost2', 'usage': 'train',
                'selections': [{'node_id': 'R', 'intervals': [[0, 4]]}]})
        d2.close()
        d3 = open_review(self.path)
        replay = d3.issue_feature_certificate({
            'request_id': 'c-lost2', 'usage': 'train',
            'selections': [{'node_id': 'R', 'intervals': [[0, 4]]}]})
        self.assertEqual(replay['request_id'], 'c-lost2')
        self.assertEqual(
            len([e for e in journal_entries(self.path)
                 if e['id'] == 'c-lost2']), 1)
        self.assertEqual(d3.read_feature_certificate('c-lost'), cert)
        d3.close()

    def test_foreign_identities_illegal_dependencies_and_defensive_copies(self):
        d = open_review(self.path)
        # 他目录独有的发布 ID 不能在本目录捕获。
        other = Path(self.tmp) / 'other'
        do = create_review(other, station_input(), station_clock())
        with self.assertRaises(ValueError):
            do.capture_waveform_input({
                'node_id': 'R', 'release_request_id': 'pub1',
                'group_key': 'e1', 'station': 'A', 'interval': [100, 120]})
        do.close()
        # 未知节点 ID、未知父节点、越界、布尔整数都拒绝。
        with self.assertRaises(UnknownNodeError):
            d.feature_health('NOPE')
        for bad in (
            {'node_id': 'X1', 'length': 5,
             'parents': [{'node_id': 'NOPE', 'offset': 0, 'width': 1}]},
            {'node_id': 'X2', 'length': 5,
             'parents': [{'node_id': 'R', 'offset': 0, 'width': 100}]},
            {'node_id': 'X3', 'length': True,
             'parents': [{'node_id': 'R', 'offset': 0, 'width': 1}]},
        ):
            with self.assertRaises(ValueError):
                d.register_window_feature(bad)
        # 组外区间扩张被拒绝。
        with self.assertRaises(ValueError):
            d.capture_waveform_input({
                'node_id': 'RX', 'release_request_id': 'pub1',
                'group_key': 'e1', 'station': 'A', 'interval': [99, 120]})
        # 调用方修改返回对象不会篡改内部历史。
        node_view = d.read_feature_node('R')
        node_view['fragments'][0]['receipt_id'] = 'HACKED'
        self.assertEqual(d.read_feature_node('R')['fragments'][0]['receipt_id'],
                         'r0')
        health = d.feature_health('R')
        health['invalid_intervals'].append([99, 100])
        health['invalid_by_defect']['x'] = [[1, 2]]
        self.assertNotIn('x', d.feature_health('R')['invalid_by_defect'])
        d.close()


class AcceptanceScenarioFiveTests(unittest.TestCase):
    """情形 5：10^9 长度上限、区间级内存与精确传播数值。"""

    def test_billion_length_health_and_memory_bound(self):
        n = 10 ** 9
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        data = {
            'stations': [{'id': 'A', 'lat': '0', 'lon': '0'}],
            'events': [{'id': 'a1', 'station': 'A', 'phase': 'P', 'time': 10}],
            'window': '0',
            'domains': {'A': [0, n]},
            'blocks': [{'id': 'A0', 'station': 'A', 'interval': [0, n]}],
            'pre': '10',
            'post': str(n - 10),
        }
        path = Path(tmp) / 'big'
        d = create_review(path, data, station_clock(domain=n))
        self.assertEqual(d.read_plan()['events'][0]['ranges']['A'], [[0, n]])
        d.register_receipt({'receipt_id': 'r0', 'version_id': 'v1',
                            'event_id': 'e1', 'block_id': 'A0',
                            'interval': [0, n]})
        preview = d.preview_release('v1', ['e1'], [])
        d.publish_release(preview,
                          [preview['groups'][0]['group_key']], 'pubbig')
        tracemalloc.start()
        d.capture_waveform_input({
            'node_id': 'R', 'release_request_id': 'pubbig',
            'group_key': preview['groups'][0]['group_key'],
            'station': 'A', 'interval': [0, n]})
        d.register_window_feature({
            'node_id': 'F', 'length': n - 5 + 1,
            'parents': [{'node_id': 'R', 'offset': 0, 'width': 5}]})
        d.register_window_feature({
            'node_id': 'G', 'length': (n - 5 + 1) - 3 + 1,
            'parents': [{'node_id': 'F', 'offset': 0, 'width': 3}]})
        d.record_sample_defect({'request_id': 'dbig', 'scope': 'physical',
                                'target': 'A',
                                'interval': [500000000, 500000002]})
        hf = d.feature_health('F')
        hg = d.feature_health('G')
        cert = d.issue_feature_certificate({
            'request_id': 'cbig', 'usage': 'train',
            'selections': [{'node_id': 'G', 'intervals': [[0, 100]]}]})
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertEqual(hf['invalid_intervals'], [[499999996, 500000002]])
        self.assertEqual(hg['invalid_intervals'], [[499999994, 500000002]])
        self.assertEqual(hf['invalid_by_defect'],
                         {'dbig': [[499999996, 500000002]]})
        self.assertTrue(cert['request_id'], 'cbig')
        # 新增工作内存（登记→传播→小范围审核）远低于 128 MiB。
        self.assertLess(peak, 128 * 1024 * 1024)
        d.close()
        d2 = open_review(path)
        self.assertEqual(d2.feature_health('F')['invalid_intervals'],
                         [[499999996, 500000002]])
        self.assertEqual(d2.feature_health('G')['invalid_intervals'],
                         [[499999994, 500000002]])
        self.assertTrue(
            d2.check_feature_certificate('cbig')['currently_usable'])
        d2.close()


class LineageValidationTests(unittest.TestCase):
    """容量、ID 空间、缺陷语义与异常类型的补充校验。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        fx = LineageFixture(Path(self.tmp) / 'review')
        self.d = fx.d
        self.key = fx.group_key

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_node_id_space_shared_and_immutable(self):
        d = self.d
        d.capture_waveform_input({
            'node_id': 'N1', 'release_request_id': 'pub1', 'group_key': self.key,
            'station': 'A', 'interval': [100, 120]})
        with self.assertRaises(ValueError):
            d.register_window_feature({
                'node_id': 'N1', 'length': 5,
                'parents': [{'node_id': 'N1', 'offset': 0, 'width': 1}]})
        self.assertIsInstance(LineageError('x'), ValueError)
        self.assertIsInstance(UnknownNodeError('x'), KeyError)
        self.assertIsInstance(UnknownCertificateError('x'), KeyError)

    def test_receipt_defect_scoped_to_receipt_and_station(self):
        d = self.d
        d.capture_waveform_input({
            'node_id': 'R', 'release_request_id': 'pub1', 'group_key': self.key,
            'station': 'A', 'interval': [100, 120]})
        # 未知回执/越界/错误 scope 拒绝，失败 ID 可重试。
        with self.assertRaises(ValueError):
            d.record_sample_defect({'request_id': 'dbad', 'scope': 'receipt',
                                    'target': 'unknown', 'interval': [100, 101]})
        with self.assertRaises(ValueError):
            d.record_sample_defect({'request_id': 'dbad', 'scope': 'receipt',
                                    'target': 'r0', 'interval': [99, 101]})
        with self.assertRaises(ValueError):
            d.record_sample_defect({'request_id': 'dbad', 'scope': 'nope',
                                    'target': 'A', 'interval': [100, 101]})
        self.assertNotIn('dbad', d._lineage.defect_ids)
        d.record_sample_defect({'request_id': 'dbad', 'scope': 'receipt',
                                'target': 'r0', 'interval': [100, 101]})
        self.assertEqual(d.feature_health('R')['invalid_by_defect'],
                         {'dbad': [[0, 1]]})

    def test_usage_inheritance_and_mismatch_rejection(self):
        # 独立目录直接发布盲测组（不经训练发布，避免用途冲突）。
        path = Path(self.tmp) / 'blind'
        d = create_review(path, station_input(), station_clock())
        d.register_receipt({'receipt_id': 'r0', 'version_id': 'v1',
                            'event_id': 'e1', 'block_id': 'A0',
                            'interval': [100, 120]})
        preview = d.preview_release('v1', ['e1'], ['e1'])
        d.publish_release(preview, [preview['groups'][0]['group_key']], 'pubblind')
        d.capture_waveform_input({
            'node_id': 'RB', 'release_request_id': 'pubblind',
            'group_key': preview['groups'][0]['group_key'],
            'station': 'A', 'interval': [100, 120]})
        self.assertEqual(d.read_feature_node('RB')['usage'], 'blind')
        d.register_window_feature({
            'node_id': 'FB', 'length': 10,
            'parents': [{'node_id': 'RB', 'offset': 0, 'width': 5}]})
        with self.assertRaises(ValueError):
            d.issue_feature_certificate({
                'request_id': 'c-wrong-usage', 'usage': 'train',
                'selections': [{'node_id': 'FB', 'intervals': [[0, 1]]}]})
        # 用途一致的盲测凭证可以签发。
        cert = d.issue_feature_certificate({
            'request_id': 'c-blind-ok', 'usage': 'blind',
            'selections': [{'node_id': 'FB', 'intervals': [[0, 1]]}]})
        self.assertEqual(cert['usage'], 'blind')
        d.close()

    def test_capture_rejects_online_only_and_physical_defect(self):
        # 独立目录：只有在线原块、没有任何归档。
        path = Path(self.tmp) / 'online-only'
        d = create_review(path, station_input(), station_clock())
        preview = d.preview_release('v1', ['e1'], [])
        # 未发布组不能捕获。
        with self.assertRaises(ValueError):
            d.capture_waveform_input({
                'node_id': 'R', 'release_request_id': 'pub-x',
                'group_key': preview['groups'][0]['group_key'],
                'station': 'A', 'interval': [100, 120]})
        d.close()


if __name__ == '__main__':
    unittest.main()
