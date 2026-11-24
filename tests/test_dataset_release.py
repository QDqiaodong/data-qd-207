import json
import shutil
import tempfile
import unittest
from pathlib import Path

from seismic_assoc import (
    ReleaseConflictError,
    create_review,
    open_review,
)


def chain_input():
    """A 站四个分离到时；D 站用偏移得到同刻度但不同台站的事件。"""
    return {
        'stations': [
            {'id': 'A', 'lat': '0', 'lon': '0'},
            {'id': 'D', 'lat': '1', 'lon': '1'},
        ],
        'events': [
            {'id': 'a1', 'station': 'A', 'phase': 'P', 'time': 10},
            {'id': 'a2', 'station': 'A', 'phase': 'P', 'time': 14},
            {'id': 'a3', 'station': 'A', 'phase': 'P', 'time': 18},
            {'id': 'a4', 'station': 'A', 'phase': 'P', 'time': 23},
            {'id': 'd1', 'station': 'D', 'phase': 'P', 'time': 23},
        ],
        'window': '0',
        'domains': {'A': [0, 30], 'D': [0, 30]},
        'blocks': [
            {'id': 'A0', 'station': 'A', 'interval': [0, 20]},
            {'id': 'A1', 'station': 'A', 'interval': [20, 30]},
            {'id': 'D0', 'station': 'D', 'interval': [0, 30]},
        ],
        'pre': '0',
        'post': '5',
    }


def chain_clock(version_id='v1'):
    return {'version_id': version_id, 'stations': {
        'A': [{'interval': [0, 30], 'offset': '0'}],
        'D': [{'interval': [0, 30], 'offset': '1'}],
    }}


def archive_chain_events(directory):
    receipts = [
        ('r1', 'e1', 'A0', [10, 15]),
        ('r2a', 'e2', 'A0', [14, 18]),
        ('r2b', 'e2', 'A0', [18, 19]),
        ('r3a', 'e3', 'A0', [18, 20]),
        ('r3b', 'e3', 'A1', [20, 23]),
        ('r4', 'e4', 'A1', [23, 28]),
        ('r5', 'e5', 'D0', [23, 28]),
    ]
    for rid, eid, bid, iv in receipts:
        directory.register_receipt({
            'receipt_id': rid,
            'version_id': 'v1',
            'event_id': eid,
            'block_id': bid,
            'interval': iv,
        })


def group_by_members(preview):
    return {tuple(g['members']): g for g in preview['groups']}


def journal_release_entries(path, request_id=None):
    text = (Path(path) / 'journal.log').read_text('utf-8')
    entries = [json.loads(line) for line in text.splitlines()
               if line and line != 'COMMITTED']
    entries = [e for e in entries if e.get('kind') == 'release']
    if request_id is not None:
        entries = [e for e in entries if e.get('id') == request_id]
    return entries


class DatasetReleaseAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / 'review'

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_acceptance_one_grouping_gap_and_partial_publish(self):
        d = create_review(self.path, chain_input(), chain_clock())
        plan = d.read_plan('v1')
        self.assertEqual(
            [e['ranges']['A'] for e in plan['events'][:4]],
            [[[10, 15]], [[14, 19]], [[18, 23]], [[23, 28]]],
        )
        self.assertEqual(plan['events'][4]['ranges']['D'], [[23, 28]])

        # 第二个事件只归档 [14,18)，[18,19) 仍在线未归档。
        d.register_receipt({
            'receipt_id': 'r2partial', 'version_id': 'v1', 'event_id': 'e2',
            'block_id': 'A0', 'interval': [14, 18],
        })
        d.register_receipt({
            'receipt_id': 'r4', 'version_id': 'v1', 'event_id': 'e4',
            'block_id': 'A1', 'interval': [23, 28],
        })

        preview = d.preview_release(
            'v1', {'e5', 'e4', 'e3', 'e2', 'e1'}, {'e3'})
        groups = group_by_members(preview)
        self.assertEqual(
            sorted(tuple(g['members']) for g in preview['groups']),
            [('e1', 'e2', 'e3'), ('e4',), ('e5',)],
        )
        g123 = groups[('e1', 'e2', 'e3')]
        g4 = groups[('e4',)]
        g5 = groups[('e5',)]
        self.assertEqual(g123['status'], 'blind')
        self.assertEqual(g123['usage'], 'blind')
        self.assertEqual(g4['status'], 'train')
        self.assertEqual(g5['status'], 'train')
        self.assertFalse(g123['conflict'])

        online_gap_intervals = [tuple(x['interval']) for x in g123['online_gaps']]
        self.assertTrue(any(lo <= 18 and 19 <= hi for lo, hi in online_gap_intervals),
                        online_gap_intervals)
        self.assertEqual(online_gap_intervals, [(10, 14), (18, 20), (20, 23)])
        self.assertTrue(all(x['kind'] == 'online' for x in g123['online_gaps']))

        k123, k4 = g123['group_key'], g4['group_key']
        with self.assertRaises(ValueError):
            d.publish_release(preview, [k123, k4], 'pub-blocked')
        self.assertIsNone(d.find_release('pub-blocked'))
        self.assertEqual(d._release_usage['train'], {})

        manifest4 = d.publish_release(preview, {k4}, 'pub4')
        self.assertEqual(manifest4['groups'][0]['members'], ['e4'])
        self.assertEqual(manifest4['groups'][0]['usage'], 'train')
        self.assertTrue(all(r['receipt_id'] == 'r4'
                            for r in manifest4['waveform_refs']))

        # 补齐归档后，原预览中的缺口提示虽已过时，仍可按原批准发布盲测组。
        archive_chain_events(d)
        manifest123 = d.publish_release(preview, [k123], 'pub123')
        self.assertEqual(manifest123['groups'][0]['usage'], 'blind')
        self.assertEqual(manifest123['groups'][0]['members'], ['e1', 'e2', 'e3'])
        self.assertTrue(manifest123['waveform_refs'])
        self.assertTrue(all(r['kind'] == 'archive'
                            for r in manifest123['waveform_refs']))
        d.close()

        d2 = open_review(self.path)
        self.assertEqual(d2.read_release('pub4'), manifest4)
        self.assertEqual(d2.read_published_manifest('pub123'), manifest123)
        d2.close()

    def test_acceptance_two_history_blocks_old_preview_atomically(self):
        d = create_review(self.path, chain_input(), chain_clock())
        archive_chain_events(d)

        preview_34 = d.preview_release('v1', ['e3', 'e4'], {'e3'})
        groups = group_by_members(preview_34)
        k3 = groups[('e3',)]['group_key']
        k4 = groups[('e4',)]['group_key']
        self.assertEqual(groups[('e3',)]['usage'], 'blind')
        self.assertEqual(groups[('e4',)]['usage'], 'train')

        preview_2 = d.preview_release('v1', ['e2'], set())
        k2 = preview_2['groups'][0]['group_key']
        d.publish_release(preview_2, [k2], 'pub-e2-train')

        with self.assertRaises(ReleaseConflictError):
            d.publish_release(preview_34, [k3, k4], 'pub-old-both')
        self.assertIsNone(d.find_release('pub-old-both'))

        # e4 未被连带记账，可从同一旧预览单独批准。
        manifest4 = d.publish_release(preview_34, [k4], 'pub-e4')
        self.assertEqual(manifest4['groups'][0]['usage'], 'train')

        # 旧训练批准被新盲测历史改钉为 blind 时，不得按旧用途发出。
        # 选尚未入账的 D 站 e5：它与 A 站历史不冲突，适合单独验证用途漂移。
        old_e5_preview = d.preview_release('v1', ['e5'], [])
        self.assertEqual(old_e5_preview['groups'][0]['usage'], 'train')
        blind_e5_preview = d.preview_release('v1', ['e5'], {'e5'})
        d.publish_release(blind_e5_preview,
                          [blind_e5_preview['groups'][0]['group_key']],
                          'pub-e5-blind-first')
        pinned = d.preview_release('v1', ['e5'], [])
        self.assertEqual(pinned['groups'][0]['usage'], 'blind')
        self.assertFalse(pinned['groups'][0]['conflict'])
        with self.assertRaises(ValueError):
            d.publish_release(old_e5_preview,
                              [old_e5_preview['groups'][0]['group_key']],
                              'pub-e5-stale-train')
        fresh_manifest = d.publish_release(
            pinned, [pinned['groups'][0]['group_key']], 'pub-e5-blind-repeat')
        self.assertEqual(fresh_manifest['groups'][0]['usage'], 'blind')
        d.close()

    def test_acceptance_three_raw_sample_identity_across_clock_versions(self):
        data = chain_input()
        data['stations'] = [{'id': 'A', 'lat': '0', 'lon': '0'}]
        data['events'] = [
            {'id': 'a10', 'station': 'A', 'phase': 'P', 'time': 10},
            {'id': 'a18', 'station': 'A', 'phase': 'P', 'time': 18},
        ]
        data['domains'] = {'A': [0, 30]}
        data['blocks'] = [
            {'id': 'A0', 'station': 'A', 'interval': [0, 18]},
            {'id': 'A1', 'station': 'A', 'interval': [18, 30]},
        ]
        d = create_review(self.path, data, {
            'version_id': 'v1',
            'stations': {'A': [{'interval': [0, 30], 'offset': '0'}]},
        })
        d.register_receipt({'receipt_id': 'r-train', 'version_id': 'v1',
                            'event_id': 'e1', 'block_id': 'A0',
                            'interval': [10, 15]})
        d.register_receipt({'receipt_id': 'r-blind', 'version_id': 'v1',
                            'event_id': 'e2', 'block_id': 'A1',
                            'interval': [18, 23]})
        p1 = d.preview_release('v1', ['e1'], [])
        p2 = d.preview_release('v1', ['e2'], {'e2'})
        train_manifest = d.publish_release(
            p1, [p1['groups'][0]['group_key']], 'pub-train')
        blind_manifest = d.publish_release(
            p2, [p2['groups'][0]['group_key']], 'pub-blind')

        v2 = {'version_id': 'v2', 'stations': {'A': [
            {'interval': [0, 18], 'offset': '0'},
            {'interval': [18, 23], 'offset': '-8'},
            {'interval': [23, 30], 'offset': '0'},
        ]}}
        d.submit_clock(v2)
        plan2 = d.read_plan('v2')
        self.assertEqual(len(plan2['events']), 1)
        self.assertEqual(plan2['events'][0]['ranges']['A'],
                         [[10, 15], [18, 23]])

        merged_preview = d.preview_release('v2', ['e1'], [])
        group = merged_preview['groups'][0]
        self.assertTrue(group['conflict'])
        self.assertIn('history_train', group['conflict_reasons'])
        self.assertIn('history_blind', group['conflict_reasons'])
        with self.assertRaises(ReleaseConflictError):
            d.publish_release(merged_preview, [group['group_key']], 'pub-merged')

        # 更高优先级（字典序更小）的新回执不改写已固定清单。
        d.register_receipt({'receipt_id': 'r0-higher', 'version_id': 'v2',
                            'event_id': 'e1', 'block_id': 'A1',
                            'interval': [18, 23]})
        self.assertEqual(d.read_release('pub-train'), train_manifest)
        self.assertEqual(d.read_release('pub-blind'), blind_manifest)
        train_refs = train_manifest['waveform_refs']
        blind_refs = blind_manifest['waveform_refs']
        self.assertEqual([r['receipt_id'] for r in train_refs], ['r-train'])
        self.assertEqual([r['receipt_id'] for r in blind_refs], ['r-blind'])
        d.close()
        d2 = open_review(self.path)
        self.assertEqual(d2.read_release('pub-train'), train_manifest)
        self.assertEqual(d2.read_release('pub-blind'), blind_manifest)
        d2.close()

    def test_acceptance_four_crash_atomicity_idempotency_and_conflicts(self):
        d = create_review(self.path, chain_input(), chain_clock())
        archive_chain_events(d)
        preview = d.preview_release('v1', ['e4', 'e5'], [])
        keys = [g['group_key'] for g in preview['groups']]
        self.assertEqual(len(keys), 2)

        d._fault = 'crash_before_commit'
        with self.assertRaises(RuntimeError):
            d.publish_release(preview, keys, 'pub-crash-before')
        d.close()

        d2 = open_review(self.path)
        with self.assertRaises(ValueError):
            d2.read_release('pub-crash-before')
        self.assertIsNone(d2.find_release('pub-crash-before'))
        self.assertEqual(d2._releases, {})
        self.assertEqual(d2._release_usage['train'], {})
        retried = d2.publish_release(preview, keys, 'pub-crash-before')
        self.assertEqual(len(retried['groups']), 2)
        d2.close()

        other = Path(self.tmp) / 'review-committed-lost'
        d = create_review(other, chain_input(), chain_clock())
        archive_chain_events(d)
        preview = d.preview_release('v1', ['e4', 'e5'], [])
        keys = [g['group_key'] for g in preview['groups']]
        d._fault = 'crash_after_commit'
        with self.assertRaises(RuntimeError):
            d.publish_release(preview, keys, 'pub-lost')
        d.close()

        d2 = open_review(other)
        committed = d2.read_release('pub-lost')
        retried = d2.publish_release(preview, keys, 'pub-lost')
        self.assertEqual(retried, committed)
        self.assertEqual(len(journal_release_entries(other, 'pub-lost')), 1)

        # 同 ID 不同语义请求必须拒绝，且不产生第二条。
        other_preview = d2.preview_release('v1', ['e4'], [])
        with self.assertRaises(ValueError):
            d2.publish_release(other_preview,
                               [other_preview['groups'][0]['group_key']],
                               'pub-lost')
        self.assertEqual(d2.read_release('pub-lost'), committed)
        self.assertEqual(len(journal_release_entries(other, 'pub-lost')), 1)
        d2.close()

    def test_different_directory_preview_and_existing_review_apis(self):
        d1 = create_review(self.path, chain_input(), chain_clock())
        archive_chain_events(d1)
        preview1 = d1.preview_release('v1', ['e4'], [])

        other = Path(self.tmp) / 'other-review'
        d2 = create_review(other, chain_input(), chain_clock())
        empty_dir = Path(self.tmp) / 'not-review'
        empty_dir.mkdir()
        with self.assertRaises(ValueError):
            open_review(empty_dir)
        with self.assertRaises(ValueError):
            d2.publish_release(preview1,
                               [preview1['groups'][0]['group_key']],
                               'foreign-preview')
        self.assertIsNone(d2.find_release('foreign-preview'))

        # 既有复核入口在发布能力使用后仍正常。
        plan = d1.read_plan('v1')
        self.assertEqual(plan['version_id'], 'v1')
        sources = d1.query_sources('v1')
        self.assertTrue(sources['events'])
        before = d1.block_status('A0')
        d1.register_receipt({'receipt_id': 'r-extra', 'version_id': 'v1',
                             'event_id': 'e1', 'block_id': 'A0',
                             'interval': [10, 11]})
        self.assertEqual(d1.block_status('A0'), before)
        d1.close()
        d2.close()

    def test_uncommitted_tail_is_not_reapplied_after_later_release(self):
        d = create_review(self.path, chain_input(), chain_clock())
        archive_chain_events(d)
        bad_preview = d.preview_release('v1', ['e5'], {'e5'})
        bad_key = bad_preview['groups'][0]['group_key']
        d._fault = 'crash_before_commit'
        with self.assertRaises(RuntimeError):
            d.publish_release(bad_preview, [bad_key], 'pub-bad-tail')
        d.close()

        d2 = open_review(self.path)
        self.assertIsNone(d2.get_release('pub-bad-tail'))
        good_preview = d2.preview_release('v1', ['e4'], [])
        d2.publish_release(good_preview,
                           [good_preview['groups'][0]['group_key']],
                           'pub-good-after-tail')
        d2.close()

        d3 = open_review(self.path)
        self.assertIsNone(d3.get_release('pub-bad-tail'))
        self.assertIsNotNone(d3.get_release('pub-good-after-tail'))
        e5 = d3.preview_release('v1', ['e5'], [])['groups'][0]
        self.assertEqual(e5['usage'], 'train')
        self.assertFalse(e5['history_blind'])
        d3.close()
        self.assertEqual(journal_release_entries(self.path, 'pub-bad-tail'), [])

    def test_api_shapes_and_ledger_wrapper(self):
        from seismic_assoc import ReleaseLedger

        d = create_review(self.path, chain_input(), chain_clock())
        archive_chain_events(d)
        preview = d.preview_release({
            'version': 'v1',
            'candidates': ['e4'],
            'blind': [],
        })
        key = preview['groups'][0]['id']
        manifest = d.publish_release({
            'publish_request_id': 'req-dict',
            'preview': preview,
            'approved': {(key,): 'train'},
        })
        self.assertEqual(manifest['release_request_id'], 'req-dict')
        self.assertIsNone(d.get_release('missing'))
        self.assertEqual(d.get_release('req-dict')['request_id'], 'req-dict')

        ledger = ReleaseLedger(d)
        preview5 = ledger.preview('v1', ['e5'], [])
        manifest5 = ledger.publish(
            preview5, [frozenset({'e5'})], 'req-ledger')
        self.assertEqual(manifest5['groups'][0]['members'], ['e5'])
        self.assertEqual(ledger.get('req-ledger')['release_request_id'],
                         'req-ledger')
        d.close()

    def test_preview_order_independent_and_missing_source_distinguished(self):
        data = chain_input()
        # 去掉 A0 中 [14,18) 的归档场景由调用处控制；这里使用块空洞。
        d = create_review(self.path, data, chain_clock())
        preview_a = d.preview_release(
            version_id='v1',
            event_ids=['e4', 'e1', 'e3', 'e2', 'e5'],
            blind_event_ids=['e3', 'e3'],
        )
        preview_b = d.preview_release(
            blind_event_ids=('e3',),
            event_ids=('e2', 'e5', 'e1', 'e4', 'e3'),
            version_id='v1',
        )
        self.assertEqual(
            [(g['group_key'], g['usage'], g['status']) for g in preview_a['groups']],
            [(g['group_key'], g['usage'], g['status']) for g in preview_b['groups']],
        )
        self.assertEqual(
            json.dumps(preview_a, sort_keys=True, ensure_ascii=False),
            json.dumps(preview_b, sort_keys=True, ensure_ascii=False),
        )
        g123 = group_by_members(preview_a)[('e1', 'e2', 'e3')]
        gap_kinds = {(tuple(x['interval']), x['kind']) for x in g123['archive_gaps']}
        # A0 [0,20) 与 A1 [20,30) 覆盖全部所需域，因此未归档是 online；
        # D 站组在 D0 中，同样 online。
        self.assertTrue(all(kind == 'online' for _, kind in gap_kinds))
        d.close()

        # 制造采样域块空洞：新目录只放 [0,12) 与 [16,30)，[12,16) 为 missing。
        hollow = Path(self.tmp) / 'hollow'
        hollow_data = chain_input()
        hollow_data['blocks'] = [
            {'id': 'Ashort', 'station': 'A', 'interval': [0, 12]},
            {'id': 'Alate', 'station': 'A', 'interval': [16, 30]},
            {'id': 'D0', 'station': 'D', 'interval': [0, 30]},
        ]
        dh = create_review(hollow, hollow_data, chain_clock())
        hp = dh.preview_release('v1', ['e1'], [])
        gaps = group_by_members(hp)[('e1',)]['archive_gaps']
        self.assertIn(((12, 15), 'missing'),
                      {(tuple(g['interval']), g['kind']) for g in gaps})
        dh.close()


if __name__ == '__main__':
    unittest.main()
