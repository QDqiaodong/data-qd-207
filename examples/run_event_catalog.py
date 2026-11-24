"""稳定事件目录身份 API 的合成数据示例：初始化、拆分、合并与谱系。

全部数据原创合成，不触碰真实波形。直接运行：

    python3 examples/run_event_catalog.py

（未安装包时脚本会自动把 ../src 加入 sys.path。）
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from seismic_assoc import create_review, reconcile_event_catalog  # noqa: E402


def make_input(arrival_ids):
    """每个到时一个台站；校时 offset 可直接控制统一时间分组。"""
    n = len(arrival_ids)
    return {
        'stations': [{'id': f'S{i}', 'lat': '0', 'lon': '0'} for i in range(n)],
        'events': [{'id': aid, 'station': f'S{i}', 'phase': 'P', 'time': 100}
                   for i, aid in enumerate(arrival_ids)],
        'window': '10',
        'domains': {f'S{i}': [0, 300] for i in range(n)},
        'blocks': [{'id': f'B{i}', 'station': f'S{i}', 'interval': [0, 300]}
                   for i in range(n)],
        'pre': '1', 'post': '2',
    }


def clock(version_id, arrival_ids, high):
    return {'version_id': version_id, 'stations': {
        f'S{i}': [{'interval': [0, 300],
                   'offset': '100' if high(aid) else '0'}]
        for i, aid in enumerate(arrival_ids)}}


def compact_state(cat):
    """只展示文档中的规范字段（返回值还带兼容性别名键）。"""
    return {
        'last_version': cat['last_version'],
        'next_sequence': cat['next_sequence'],
        'events': [{'catalog_id': e['catalog_id'],
                    'plan_event_id': e['event_id'],
                    'members': e['canonical_key']} for e in cat['events']],
        'identities': [{'catalog_id': i['catalog_id'], 'status': i['status'],
                        'created': i['created_version'],
                        'ended': i['ended_version']}
                       for i in cat['identities']],
    }


def compact_lineage(turn):
    return {
        'from': turn['from_version'], 'to': turn['to_version'],
        'associations': [
            {'old': a['old_catalog_id'], 'new_event': a['new_event_id'],
             'shared_count': a['shared_count'],
             'shared_ids': a['shared_arrival_ids'],
             'inherited': a['inherited']}
            for a in turn['associations']],
        'new_events': [
            {'event': e['event_id'], 'catalog_id': e['catalog_id'],
             'inherited_from': e['inherited_from'], 'overlaps': e['overlaps']}
            for e in turn['events']],
        'ended': turn['ended'], 'created': turn['created'],
    }


def main():
    root = Path(tempfile.mkdtemp(prefix='event-catalog-example-'))
    ids = ['a', 'b', 'c']

    # v1：三个到时同属一个计划事件
    review = create_review(
        root / 'review', make_input(ids),
        clock('v1', ids, lambda aid: False))

    # 1) 首版：按规范键 [a,b,c] 分配 ce1
    cat_v1 = reconcile_event_catalog(None, review)
    print('== v1 初始化 ==')
    print(json.dumps(compact_state(cat_v1), ensure_ascii=False, indent=2))

    # v2：a 拆出，b/c 仍同组 -> ce1 按最大共享继承 b/c，a 得到新 ce2
    review.submit_clock(clock('v2', ids, lambda aid: aid in ('b', 'c')))
    cat_v2 = reconcile_event_catalog(cat_v1, review)
    print('== v1 -> v2 拆分谱系 ==')
    print(json.dumps(compact_lineage(cat_v2['lineage'][-1]),
                     ensure_ascii=False, indent=2))

    # v3：重新合并为一个事件 -> ce1 继承，ce2 结束但永不复用
    review.submit_clock(clock('v3', ids, lambda aid: False))
    cat_v3 = reconcile_event_catalog(cat_v2, review)
    print('== v2 -> v3 合并后的状态 ==')
    print(json.dumps(compact_state(cat_v3), ensure_ascii=False, indent=2))
    print('== v2 -> v3 谱系 ==')
    print(json.dumps(compact_lineage(cat_v3['lineage'][-1]),
                     ensure_ascii=False, indent=2))

    # 重复计算确定一致；已处理版本/跳版被拒绝
    again = reconcile_event_catalog(cat_v2, review, 'v3')
    assert json.dumps(again, sort_keys=True) == json.dumps(cat_v3, sort_keys=True)
    try:
        reconcile_event_catalog(cat_v1, review, 'v3')  # 跳过了 v2
    except ValueError as exc:
        print('跳版被拒绝:', exc)

    # API 不关闭句柄，复核目录当前版本仍由 submit_clock 决定
    print('review still open, current_version =', review.current_version())
    review.close()


if __name__ == '__main__':
    main()
