"""本地波形复核目录的最小可运行示例（全部为合成数据，不触碰真实波形）。"""
import json
import tempfile
from pathlib import Path

from seismic_assoc import create_review, open_review

data = {
    'stations': [{'id': 'A', 'lat': '0', 'lon': '0'},
                 {'id': 'B', 'lat': '1', 'lon': '1'}],
    # time 是台站本地采样刻度，必须为整数
    'events': [{'id': 'aP', 'station': 'A', 'phase': 'P', 'time': 10},
               {'id': 'bP', 'station': 'B', 'phase': 'P', 'time': 10}],
    'window': '1',
    'domains': {'A': [0, 20], 'B': [0, 20]},
    'blocks': [{'id': 'X', 'station': 'A', 'interval': [0, 15]},
               {'id': 'Y', 'station': 'A', 'interval': [15, 20]},
               {'id': 'Z', 'station': 'B', 'interval': [0, 20]}],
    'pre': '1', 'post': '2',
}
v1 = {'version_id': 'v1', 'stations': {
    'A': [{'interval': [0, 20], 'offset': '0'}],
    'B': [{'interval': [0, 20], 'offset': '0'}]}}
v2 = {'version_id': 'v2', 'stations': {
    'A': [{'interval': [0, 15], 'offset': '0'},
          {'interval': [15, 20], 'offset': '-6'}],
    'B': [{'interval': [0, 20], 'offset': '0'}]}}

if __name__ == '__main__':
    root = Path(tempfile.mkdtemp(prefix='review-example-'))
    d = create_review(root / 'review', data, v1)
    d.register_receipt({'receipt_id': 'r1', 'version_id': 'v1',
                        'event_id': 'e1', 'block_id': 'X', 'interval': [9, 12]})
    d.submit_clock(v2)
    print('current version:', d.current_version())
    print('v2 plan:        ', json.dumps(d.read_plan()['events'][0]['ranges']))
    print('v2 sources:     ', json.dumps(d.query_sources()['events'][0]['stations']))
    d.close()

    d2 = open_review(root / 'review')  # 另一位值班员重开
    print('reopened state: ', d2.current_version(), d2.block_status('Y'))
    d2.close()
