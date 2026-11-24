"""样本级特征血缘与交付凭证的最小可运行示例（全部为合成引用，不触碰波形）。

运行：.venv/bin/python -I examples/run_feature_lineage.py
"""
import json
import tempfile
from pathlib import Path

from seismic_assoc import create_review, open_review

data = {
    'stations': [{'id': 'A', 'lat': '0', 'lon': '0'}],
    # time 是台站本地采样刻度；pre=10/post=10 使事件 e1 需要 A 的 [100,120)
    'events': [{'id': 'a1', 'station': 'A', 'phase': 'P', 'time': 110}],
    'window': '0',
    'domains': {'A': [0, 200]},
    'blocks': [{'id': 'A0', 'station': 'A', 'interval': [0, 200]}],
    'pre': '10',
    'post': '10',
}
v1 = {'version_id': 'v1', 'stations': {
    'A': [{'interval': [0, 200], 'offset': '0'}]}}

if __name__ == '__main__':
    root = Path(tempfile.mkdtemp(prefix='feature-lineage-example-'))
    d = create_review(root / 'review', data, v1)
    d.register_receipt({'receipt_id': 'r0', 'version_id': 'v1',
                        'event_id': 'e1', 'block_id': 'A0',
                        'interval': [100, 112]})
    d.register_receipt({'receipt_id': 'r1', 'version_id': 'v1',
                        'event_id': 'e1', 'block_id': 'A0',
                        'interval': [112, 120]})
    preview = d.preview_release('v1', ['e1'], [])
    group_key = preview['groups'][0]['group_key']
    d.publish_release(preview, [group_key], 'pub1')

    # 重新实际取数：固定使用 r0、r1 两份完好归档副本。
    d.capture_waveform_input({
        'node_id': 'R', 'release_request_id': 'pub1',
        'group_key': group_key, 'station': 'A', 'interval': [100, 120]})
    # F[i] 依赖 R 的窗口 [i, i+5)；G[i] 依赖 F 的窗口 [i, i+3)。
    d.register_window_feature({
        'node_id': 'F', 'length': 16,
        'parents': [{'node_id': 'R', 'offset': 0, 'width': 5}]})
    d.register_window_feature({
        'node_id': 'G', 'length': 14,
        'parents': [{'node_id': 'F', 'offset': 0, 'width': 3}]})
    d.issue_feature_certificate({
        'request_id': 'cert-g', 'usage': 'train',
        'selections': [{'node_id': 'G', 'intervals': [[0, 4]]}]})

    # r0 在本地刻度 [108,110) 的副本损坏：R 输出 [8,10) 失效，
    # 按窗口规则传播为 F [4,10)、G [2,10)，旧凭证当前不可用但原文不变。
    d.record_sample_defect({'request_id': 'def-r0', 'scope': 'receipt',
                            'target': 'r0', 'interval': [108, 110]})
    print('R health:', json.dumps(d.feature_health('R')['invalid_intervals']))
    print('F health:', json.dumps(d.feature_health('F')['invalid_intervals']))
    print('G health:', json.dumps(d.feature_health('G')['invalid_intervals']))
    print('certificate currently usable:',
          d.check_feature_certificate('cert-g')['currently_usable'])
    print('source query now:',
          json.dumps(d.query_sources()['events'][0]['stations'][0]['sources']))
    d.close()

    d2 = open_review(root / 'review')  # 重开后同一事实
    print('reopened G health:',
          json.dumps(d2.feature_health('G')['invalid_intervals']))
    d2.close()
