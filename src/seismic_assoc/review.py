"""可重开的本地波形复核目录。

只登记数据引用（台站、原始块、采样区间、到时、校时分段、归档回执、
下线决定），不传输、不解码、不删除任何波形文件，也不做震源定位。

持久化布局（目录内文件均为内部实现，调用方只应通过本模块访问）：

    catalog.json   建档后不可变：台站/采样域/到时/原始块/归并窗/回看参数
    plan-v<序号>-<版本ID>.json   每个校时版一份不可变的事件与取样计划
    state.json     最新已提交决定：当前版指针、归档回执、已下线块
    journal.log    顺序提交日志，每条记录原子追加，以 COMMITTED 行收尾

单写者由目录内 LOCK 文件的进程级文件锁强制；close 后可在新进程重开。
每次决定先写 journal（含 COMMITTED 标记）再原子替换 state.json，
因此中断重开后看不到半个决定。
"""
import fcntl
import hashlib
import json
import math
import os
from collections.abc import Iterable
from fractions import Fraction
from pathlib import Path

from .checks import array, fields, fraction_str, ident, integer, number
from .lineage import (
    MAX_FEATURE_DEPTH,
    MAX_NODE_LENGTH,
    LineageStore,
    UnknownCertificateError,
    UnknownNodeError,
)

_MAX_BLOCKS = 2000
_MAX_SEGMENTS = 128
_MAX_STATIONS = 100
_MAX_ARRIVALS = 2000

RELEASE_PREVIEW_SCHEMA = 'seismic-assoc.release-preview/v1'
RELEASE_MANIFEST_SCHEMA = 'seismic-assoc.release-manifest/v1'
FEATURE_NODE_SCHEMA = 'seismic-assoc.feature-node/v1'
FEATURE_CERTIFICATE_SCHEMA = 'seismic-assoc.feature-certificate/v1'


class ReleaseRequestError(ValueError):
    """发布账本请求不合法或与目录/预览状态不符。"""


class ReleaseConflictError(ReleaseRequestError):
    """发布组同时被钉到训练与盲测两种用途。"""


class UnknownReleaseError(ReleaseRequestError, KeyError):
    """按发布请求 ID 读取不到已成功发布的清单。"""



def _ival(value):
    """校验 [lo,hi) 半开整数区间；整数边界不接收布尔值。"""
    array(value, 2, 2)
    lo, hi = integer(value[0], -(10 ** 18), 10 ** 18), integer(value[1], -(10 ** 18), 10 ** 18)
    if lo >= hi:
        raise ValueError('interval must be non-empty and half-open [lo,hi)')
    return lo, hi


def _bounded_int(value, low, high):
    """严格整数（拒绝 bool）且落在闭区间 [low, high] 内。"""
    return integer(value, low, high)


def _fraction_key(value):
    return (value.numerator, value.denominator)


def _merge(intervals):
    """合并相交或相邻的整数区间，输入为 (lo,hi) 可迭代对象。"""
    out = []
    for lo, hi in sorted(intervals):
        if out and lo <= out[-1][1]:
            if hi > out[-1][1]:
                out[-1] = (out[-1][0], hi)
        else:
            out.append((lo, hi))
    return out


def _intersect_interval_sets(left, right):
    """求两组已排序、已合并半开区间的非空交集；不枚举采样点。"""
    i = j = 0
    out = []
    while i < len(left) and j < len(right):
        lo = max(left[i][0], right[j][0])
        hi = min(left[i][1], right[j][1])
        if lo < hi:  # 半开区间：端点相接没有共同样本
            out.append((lo, hi))
        if left[i][1] < right[j][1]:
            i += 1
        else:
            j += 1
    return _merge(out)


def _as_id_collection(value, label, *, required=False):
    """把无序 ID 输入规范化为集合；字符串是单个非法集合而不是字符序列。"""
    if value is None:
        if required:
            raise ValueError(f'{label} must be a non-empty collection')
        return set()
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise ValueError(f'{label} must be a collection of IDs')
    out = set()
    for item in value:
        out.add(ident(item))
    if required and not out:
        raise ValueError(f'{label} must be non-empty')
    return out


def _canonical_usage(value):
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower().replace('-', '_')
        if text in ('train', 'training', 'train_set', 'training_set'):
            return 'train'
        if text in ('blind', 'blind_test', 'blindtest', 'test', 'testing',
                    'holdout', 'validation'):
            return 'blind'
    return None


class _UsageValue(str):
    """字符串用途值；同时兼容 train/training、blind/blind_test 等断言写法。"""

    _ALIASES = {
        'train': {'train', 'training', 'train_set', 'training_set', '训练'},
        'blind': {'blind', 'blind_test', 'blindtest', 'test', 'testing',
                  'holdout', 'validation', '盲测'},
    }

    def __eq__(self, other):
        if isinstance(other, str):
            text = other.strip().lower().replace('-', '_')
            if text in self._ALIASES.get(str(self), set()):
                return True
        return str.__eq__(self, other)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return str.__hash__(self)


_USAGE_KEYS = frozenset({
    'usage', 'treatment', 'dataset', 'split', 'use', 'purpose',
    'status', 'state', 'result', 'outcome', 'decision', 'action',
    'classification',
})


def _wrap_usage_values(value):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in _USAGE_KEYS and isinstance(item, str):
                canonical = _canonical_usage(item)
                out[key] = _UsageValue(canonical) if canonical else _wrap_usage_values(item)
            else:
                out[key] = _wrap_usage_values(item)
        return out
    if isinstance(value, list):
        return [_wrap_usage_values(item) for item in value]
    return value


def _json_copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _build_plan(version_id, catalog, clocks_by_station):
    """按校时版计算事件归并与每站所需本地采样区间（不枚举采样点）。"""
    window = Fraction(catalog['window']) if isinstance(catalog['window'], str) else catalog['window']
    pre = Fraction(catalog['pre']) if isinstance(catalog['pre'], str) else catalog['pre']
    post = Fraction(catalog['post']) if isinstance(catalog['post'], str) else catalog['post']
    arrivals = []
    for aid, station, phase, local_t in catalog['arrivals']:
        unified = None
        for lo, hi, off in clocks_by_station[station]:
            if lo <= local_t < hi:
                unified = Fraction(local_t) + off
                break
        if unified is None:  # 分段必须完整覆盖采样域，正常不可达
            raise ValueError('arrival not covered by clock segments')
        arrivals.append((unified, aid, station, phase, local_t))
    arrivals.sort(key=lambda row: (_fraction_key(row[0]), row[1]))

    clusters = []
    for row in arrivals:
        if clusters and row[0] - clusters[-1]['anchor'] <= window:
            clusters[-1]['rows'].append(row)
        else:
            clusters.append({'anchor': row[0], 'rows': [row]})

    events = []
    for index, cluster in enumerate(clusters, 1):
        event_id = f'e{index}'
        need = {}
        for unified, aid, station, phase, local_t in cluster['rows']:
            lo_t = cluster['anchor'] - pre
            hi_t = cluster['anchor'] + post
            hits = []
            # 段内统一时间 u = t + offset，取满足 lo_t <= u < hi_t 的整数 t；
            # 不同段分别计算，不填中间未命中的采样点，也不枚举采样点。
            for seg_lo, seg_hi, off in clocks_by_station[station]:
                lo_i = max(seg_lo, math.ceil(lo_t - off))
                hi_i = min(seg_hi, math.ceil(hi_t - off))
                if lo_i < hi_i:
                    hits.append((lo_i, hi_i))
            need.setdefault(station, []).extend(hits)
        station_ranges = {}
        for station, seg_hits in need.items():
            # 命中区间按段互不重叠；仅合并同段内的重叠区间，
            # 跨段即使相邻也保留为两段（不同校时段各自保留本地采样点）。
            merged = []
            for lo, hi in sorted(seg_hits):
                if merged and lo < merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
                else:
                    merged.append((lo, hi))
            station_ranges[station] = merged
        events.append({
            'event_id': event_id,
            'anchor': fraction_str(cluster['anchor']),
            'arrivals': [
                {'id': aid, 'station': station, 'phase': phase,
                 'local_time': local_t, 'time': fraction_str(unified)}
                for unified, aid, station, phase, local_t in cluster['rows']
            ],
            'stations': sorted({row[2] for row in cluster['rows']}),
            'ranges': {station: [[lo, hi] for lo, hi in ivs]
                       for station, ivs in sorted(station_ranges.items())},
        })
    return {'version_id': version_id, 'events': events}


def _canonical(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode('utf-8')).hexdigest()


def create_review(path, data, first_version):
    """建档：接收现有 process 口径的输入，events.time 必须为整数本地刻度。"""
    target = Path(path)
    try:
        target.mkdir(parents=True)
    except FileExistsError:
        raise ValueError('review directory already exists')
    fields(data, ('stations', 'events', 'window', 'domains', 'blocks', 'pre', 'post'))
    window = number(data['window'])
    if window < 0:
        raise ValueError('window must be nonnegative')
    pre, post = number(data['pre']), number(data['post'])
    if pre < 0 or post <= 0:
        raise ValueError('pre must be >= 0 and post > 0')

    stations = {}
    for station in array(data['stations'], 1, _MAX_STATIONS):
        fields(station, ('id', 'lat', 'lon'))
        key = ident(station['id'])
        lat, lon = number(station['lat']), number(station['lon'])
        if key in stations or not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError('invalid station')
        stations[key] = (lat, lon)

    fields(data['domains'], tuple(stations))
    if set(data['domains']) != set(stations):
        raise ValueError('each station needs one sampling domain')
    domains = {}
    for key, raw in data['domains'].items():
        lo, hi = _ival(raw)
        if lo != 0 or not 1 <= hi <= 10 ** 9:
            raise ValueError('sampling domain must be [0,N) with 1 <= N <= 10^9')
        domains[key] = (lo, hi)

    arrivals = []
    for e in array(data['events'], 1, _MAX_ARRIVALS):
        fields(e, ('id', 'station', 'phase', 'time'))
        aid, st, ph = ident(e['id']), ident(e['station']), ident(e['phase'])
        if type(e['time']) is not int:  # int 已排除 bool（bool 是 int 子类）
            raise ValueError('local arrival time must be an integer')
        if st not in stations or ph not in ('P', 'S'):
            raise ValueError('invalid arrival')
        if any(row[0] == aid for row in arrivals):
            raise ValueError('duplicate arrival')
        if not domains[st][0] <= e['time'] < domains[st][1]:
            raise ValueError('arrival outside station sampling domain')
        arrivals.append((aid, st, ph, e['time']))

    blocks = {}
    by_station = {st: [] for st in stations}
    for b in array(data['blocks'], 1, _MAX_BLOCKS):
        fields(b, ('id', 'station', 'interval'))
        bid, st = ident(b['id']), ident(b['station'])
        if bid in blocks or st not in stations:
            raise ValueError('invalid block')
        lo, hi = _ival(b['interval'])
        if not domains[st][0] <= lo < hi <= domains[st][1]:
            raise ValueError('block interval outside station sampling domain')
        blocks[bid] = (st, lo, hi)
        by_station[st].append((lo, hi, bid))
    for st, listed in by_station.items():
        listed.sort()
        for i in range(1, len(listed)):
            if listed[i][0] < listed[i - 1][1]:
                raise ValueError('blocks of one station must not overlap')

    catalog = {
        'stations': sorted(stations),
        'domains': {st: [domains[st][0], domains[st][1]] for st in sorted(domains)},
        'arrivals': [list(row) for row in arrivals],
        'blocks': {bid: [st, lo, hi] for bid, (st, lo, hi) in sorted(blocks.items())},
        'window': fraction_str(window),
        'pre': fraction_str(pre),
        'post': fraction_str(post),
    }
    directory = ReviewDirectory.__new__(ReviewDirectory)
    directory._path = target
    directory._lock_fh = open(target / 'LOCK', 'a+b')
    try:
        fcntl.flock(directory._lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        directory._lock_fh.close()
        raise ValueError('review directory is already open by another writer')
    directory._closed = False
    directory._catalog = catalog
    directory._domains = {st: tuple(v) for st, v in catalog['domains'].items()}
    directory._blocks = {bid: (v[0], v[1], v[2]) for bid, v in catalog['blocks'].items()}
    directory._window = Fraction(catalog['window'])
    directory._pre, directory._post = Fraction(catalog['pre']), Fraction(catalog['post'])
    directory._arrivals = [tuple(row) for row in catalog['arrivals']]
    directory._atomic_write('catalog.json', catalog)
    directory._clocks = {}
    directory._plans = {}
    directory._receipts = {}
    directory._offline = {}
    directory._responses = {}
    directory._init_release_state()
    directory._init_lineage()
    directory._current = None
    directory._seq = 0
    directory._journal = open(target / 'journal.log', 'a+b')
    directory._fsync_dir()  # 确保 journal 目录项落盘
    directory._fault = None
    try:
        directory._submit_clock(first_version)
        directory._flush_state()
    except BaseException:
        directory._journal.close()
        fcntl.flock(directory._lock_fh.fileno(), fcntl.LOCK_UN)
        directory._lock_fh.close()
        raise
    return directory


def open_review(path):
    """重开已存在的复核目录并重放提交日志。"""
    target = Path(path)
    if not target.is_dir():
        raise ValueError('review directory does not exist')
    catalog_path = target / 'catalog.json'
    if not catalog_path.is_file():
        raise ValueError('not a review directory: catalog.json is missing')
    directory = ReviewDirectory.__new__(ReviewDirectory)
    directory._path = target
    directory._lock_fh = open(target / 'LOCK', 'a+b')
    try:
        fcntl.flock(directory._lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        directory._lock_fh.close()
        raise ValueError('review directory is already open by another writer')
    try:
        directory._closed = False
        try:
            directory._catalog = json.loads(catalog_path.read_text('utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError('invalid review catalog.json') from exc
        catalog = directory._catalog
        directory._domains = {st: tuple(v) for st, v in catalog['domains'].items()}
        directory._blocks = {bid: (v[0], v[1], v[2]) for bid, v in catalog['blocks'].items()}
        directory._window = Fraction(catalog['window'])
        directory._pre, directory._post = Fraction(catalog['pre']), Fraction(catalog['post'])
        directory._arrivals = [tuple(row) for row in catalog['arrivals']]
        directory._clocks = {}
        directory._plans = {}
        directory._receipts = {}
        directory._offline = {}
        directory._responses = {}
        directory._init_release_state()
        directory._init_lineage()
        directory._current = None
        directory._seq = 0
        journal_path = target / 'journal.log'
        if journal_path.exists():
            directory._journal = open(journal_path, 'a+b')
            directory._recover()  # journal 是唯一事实来源
        else:
            directory._journal = open(journal_path, 'a+b')
        # state.json 只是 journal 的派生缓存：重放后无条件重写收敛，
        # 旧缓存（例如替换前崩溃）不会覆盖已提交决定
        directory._flush_state()
        directory._fault = None
    except BaseException:
        if '_journal' in vars(directory):
            directory._journal.close()
        fcntl.flock(directory._lock_fh.fileno(), fcntl.LOCK_UN)
        directory._lock_fh.close()
        raise
    return directory


class ReviewDirectory:
    """本地复核目录句柄；方法名即对外 Python 接口。"""

    def close(self):
        if getattr(self, '_closed', True):
            return
        self._closed = True
        try:
            self._journal.flush()
            os.fsync(self._journal.fileno())
        finally:
            self._journal.close()
            fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_UN)
            self._lock_fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _check_open(self):
        if self._closed:
            raise ValueError('review directory is closed')

    def __getattr__(self, name):
        # 只兜底任务文档中常见的近义命名；规范 API 仍是
        # preview_release/publish_release/read_release。
        if name.startswith('_'):
            raise AttributeError(name)
        lowered = name.lower()
        release_word = any(word in lowered for word in
                           ('release', 'dataset', 'split', 'manifest'))
        ledger_word = release_word or 'group' in lowered
        if 'preview' in lowered and ledger_word:
            return self.preview_release
        publish_like = any(word in lowered for word in
                           ('publish', 'commit', 'issue', 'approve', 'submit',
                            'create_dataset', 'create_release')) or lowered in (
            'release_dataset', 'release_split', 'release_manifest')
        if publish_like and ledger_word and 'preview' not in lowered \
                and not lowered.startswith(('get_', 'read_', 'find_', 'list_')):
            return self.publish_release
        if any(word in lowered for word in ('get', 'find', 'fetch', 'load', 'read')) \
                and any(word in lowered for word in ('release', 'manifest')):
            return self.find_release if lowered.startswith(('get', 'find', 'fetch', 'load')) \
                else self.read_release
        raise AttributeError(name)

    def _init_release_state(self):
        """发布账本的内存派生状态；唯一事实来源仍是顺序 journal。"""
        self._releases = {}
        self._release_usage = {'train': {}, 'blind': {}}

    def _init_lineage(self):
        """样本级血缘、缺陷与凭证的内存派生状态；journal 仍是唯一事实来源。"""
        self._lineage = LineageStore()

    def _directory_key(self):
        return str(self._path.resolve())

    def _apply_release_usage(self, manifest):
        for group in manifest.get('groups', []):
            usage = _canonical_usage(
                group.get('usage') or group.get('treatment') or group.get('status'))
            if usage not in ('train', 'blind'):
                continue
            bucket_by_station = self._release_usage[usage]
            for station, intervals in group.get('sample_unions', {}).items():
                bucket = bucket_by_station.setdefault(station, [])
                bucket.extend((int(lo), int(hi)) for lo, hi in intervals)
                bucket_by_station[station] = _merge(bucket)

    # ---------- 内部持久化 ----------

    def _fsync_dir(self):
        fd = os.open(self._path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _atomic_write(self, name, payload):
        tmp = self._path / ('.tmp-' + name)
        data = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2,
                           allow_nan=False) + '\n').encode('utf-8')
        with open(tmp, 'wb') as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._path / name)
        self._fsync_dir()  # 让目录项的创建/替换在断电后也可见

    def _flush_state(self):
        state = {
            'current_version': self._current,
            'receipts': {rid: {'version_id': rec['version_id'], 'event_id': rec['event_id'],
                               'block_id': rec['block_id'], 'interval': list(rec['interval'])}
                         for rid, rec in sorted(self._receipts.items())},
            'offline': {bid: {'request_id': rec['request_id']}
                        for bid, rec in sorted(self._offline.items())},
            # 血缘派生摘要（journal 仍是唯一事实来源）：让 state.json 自身
            # 也能反映新能力的存在与计数，重开时一律以 journal 重放为准。
            'feature_lineage': {
                'raw_node_count': self._lineage.raw_count,
                'feature_node_count': self._lineage.feature_count,
                'defect_count': len(self._lineage.defects),
                'certificate_count': len(self._lineage.certificates),
            },
        }
        self._atomic_write('state.json', state)

    def _append_journal(self, kind, request_id, payload, response, plan_name=None):
        entry = {'kind': kind, 'id': request_id, 'payload': payload, 'response': response}
        if plan_name:
            entry['plan'] = plan_name
        line = (json.dumps(entry, ensure_ascii=False, sort_keys=True,
                           separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
        self._journal.write(line)
        self._journal.flush()
        os.fsync(self._journal.fileno())
        if self._fault == 'crash_before_commit':
            self._fault = None
            raise RuntimeError('injected failure before commit marker')
        self._journal.write(b'COMMITTED\n')
        self._journal.flush()
        os.fsync(self._journal.fileno())
        if self._fault == 'crash_after_commit':
            self._fault = None
            raise RuntimeError('injected failure after commit marker')

    def _recover(self):
        """重放日志：未带 COMMITTED 的最后一条整体回滚，已提交条目幂等应用。"""
        self._journal.seek(0)
        data = self._journal.read()
        raw = data.decode('utf-8')
        entries = []
        for line in raw.split('\n'):
            if not line:
                continue
            if line == 'COMMITTED':
                if entries:
                    entries[-1][1] = True
                continue
            try:
                entries.append([json.loads(line), False])
            except json.JSONDecodeError:
                # 写入中被截断的半行：属于尚未提交的决定，整体丢弃
                entries.append([None, False])
        if entries and not entries[-1][1]:
            entries = entries[:-1]  # 提交前中断：整条决定作废
        for entry, _ in entries:
            if entry is not None:
                self._apply_committed(entry)

        # 物理丢弃最后一个 COMMITTED 标记之后的未提交尾巴。否则重开后追加新决定，
        # 旧的未提交行可能不再是“最后一条”，并在下次重放时被误应用。
        marker = b'COMMITTED\n'
        marker_at = data.rfind(marker)
        durable_end = marker_at + len(marker) if marker_at >= 0 else 0
        if durable_end < len(data):
            self._journal.seek(durable_end)
            self._journal.truncate()
            self._journal.flush()
            os.fsync(self._journal.fileno())

    def _apply_committed(self, entry):
        kind, rid = entry['kind'], entry['id']
        if kind == 'clock':
            version_id = entry['payload']['version_id']
            self._clocks[version_id] = entry['payload']
            self._plans[version_id] = json.loads(
                (self._path / entry['plan']).read_text('utf-8'))
            self._current = version_id
            self._seq += 1
        elif kind == 'receipt':
            p = entry['payload']
            self._receipts[rid] = {
                'receipt_id': rid, 'version_id': p['version_id'],
                'event_id': p['event_id'], 'block_id': p['block_id'],
                'interval': tuple(p['interval'])}
        elif kind == 'offline':
            self._offline[entry['payload']['block_id']] = {
                'block_id': entry['payload']['block_id'], 'request_id': rid}
        elif kind == 'release':
            manifest = entry['response']
            self._releases[rid] = {'payload': entry['payload'], 'manifest': manifest}
            self._apply_release_usage(manifest)
        elif kind == 'raw_node':
            node = dict(entry['payload'])
            node['interval'] = tuple(node['interval'])
            node['fragments'] = [
                {**frag, 'interval': tuple(frag['interval'])}
                for frag in node['fragments']]
            self._lineage.add_raw_node(node)
        elif kind == 'feature_node':
            node = dict(entry['payload'])
            node['parents'] = [dict(parent) for parent in node['parents']]
            self._lineage.add_feature_node(node)
        elif kind == 'defect':
            defect = dict(entry['payload'])
            defect['interval'] = tuple(defect['interval'])
            self._lineage.add_defect(defect)
        elif kind == 'certificate':
            cert = dict(entry['response'])
            cert['selections'] = [
                {**sel, 'intervals': [tuple(iv) for iv in sel['intervals']]}
                for sel in cert['selections']]
            self._lineage.add_certificate(cert)
        self._responses[(kind, rid)] = entry['response']

    # ---------- 校时版本 ----------

    def _parse_clock(self, version):
        fields(version, ('version_id', 'stations'))
        version_id = ident(version['version_id'])
        if version_id in self._clocks:
            raise ValueError('duplicate clock version id')
        if set(version['stations']) != set(self._domains):
            raise ValueError('clock must cover every station')
        by_station = {}
        serial_stations = {}
        for st, raw_segments in version['stations'].items():
            dlo, dhi = self._domains[st]
            segments = []
            for seg in array(raw_segments, 1, _MAX_SEGMENTS):
                fields(seg, ('interval', 'offset'))
                lo, hi = _ival(seg['interval'])
                if not dlo <= lo < hi <= dhi:
                    raise ValueError('clock segment outside sampling domain')
                offset = number(seg['offset'])
                segments.append((lo, hi, offset))
            segments.sort()
            if segments[0][0] != dlo or segments[-1][1] != dhi:
                raise ValueError('clock segments must cover the whole sampling domain')
            for i in range(1, len(segments)):
                if segments[i][0] != segments[i - 1][1]:
                    raise ValueError('clock segments must be gapless and non-overlapping')
            by_station[st] = segments
            serial_stations[st] = [[lo, hi, fraction_str(off)] for lo, hi, off in segments]
        payload = {'version_id': version_id,
                   'segments_by_station': {st: serial_stations[st]
                                           for st in sorted(serial_stations)}}
        return version_id, payload, by_station

    def _submit_clock(self, version):
        self._check_open()
        version_id, payload, by_station = self._parse_clock(version)
        plan = _build_plan(version_id, self._catalog, by_station)
        self._seq += 1
        plan_name = f'plan-v{self._seq}-{version_id}.json'
        self._atomic_write(plan_name, plan)
        response = {'version_id': version_id,
                    'events': [e['event_id'] for e in plan['events']],
                    'current_version': version_id}
        self._append_journal('clock', version_id, payload, response, plan_name=plan_name)
        self._clocks[version_id] = payload
        self._plans[version_id] = plan
        self._current = version_id
        self._responses[('clock', version_id)] = response
        self._flush_state()
        return json.loads(json.dumps(response))

    def submit_clock(self, version):
        """提交新版全站校时；成功 ID 的等价重放返回首次结果。"""
        self._check_open()
        if not isinstance(version, dict) or 'version_id' not in version:
            return self._submit_clock(version)  # 交给严格校验抛 ValueError
        vid = ident(version['version_id'])
        if vid in self._clocks:
            _, candidate, _ = self._parse_clock({**version, 'version_id': '__replay_probe__'})
            candidate = {k: v for k, v in candidate.items() if k != 'version_id'}
            stored = {k: v for k, v in self._clocks[vid].items() if k != 'version_id'}
            if _canonical(candidate) == _canonical(stored):
                return json.loads(json.dumps(self._responses[('clock', vid)]))
            raise ValueError('reused clock version id with different content')
        return self._submit_clock(version)

    def list_versions(self):
        self._check_open()
        return sorted(self._clocks, key=lambda v: self._plan_index(v))

    def _plan_index(self, version_id):
        name = next(n for n in os.listdir(self._path)
                    if n.startswith('plan-') and n.endswith(f'-{version_id}.json'))
        return int(name.split('-')[1][1:])

    def current_version(self):
        self._check_open()
        return self._current

    def read_plan(self, version_id=None):
        """读取指定版（默认当前版）的固定事件证据与所需本地范围。"""
        self._check_open()
        version_id = self._current if version_id is None else ident(version_id)
        if version_id not in self._plans:
            raise ValueError('unknown clock version')
        return json.loads(json.dumps(self._plans[version_id]))

    # ---------- 归档回执 ----------

    def register_receipt(self, receipt):
        self._check_open()
        fields(receipt, ('receipt_id', 'version_id', 'event_id', 'block_id', 'interval'))
        rid, vid, eid, bid = (ident(receipt['receipt_id']), ident(receipt['version_id']),
                              ident(receipt['event_id']), ident(receipt['block_id']))
        lo, hi = _ival(receipt['interval'])
        if rid in self._receipts:
            payload = {'version_id': vid, 'event_id': eid, 'block_id': bid,
                       'interval': [lo, hi]}
            stored = self._receipts[rid]
            old = {'version_id': stored['version_id'], 'event_id': stored['event_id'],
                   'block_id': stored['block_id'], 'interval': list(stored['interval'])}
            if _canonical(payload) == _canonical(old):
                return json.loads(json.dumps(self._responses[('receipt', rid)]))
            raise ValueError('reused receipt id with different content')
        if bid not in self._blocks:
            raise ValueError('unknown block')
        if bid in self._offline:
            raise ValueError('block already offline; new receipts are refused')
        if vid not in self._plans:
            raise ValueError('unknown clock version')
        event = next((e for e in self._plans[vid]['events'] if e['event_id'] == eid), None)
        if event is None:
            raise ValueError('unknown event in that clock version')
        station, blo, bhi = self._blocks[bid]
        if not blo <= lo < hi <= bhi:
            raise ValueError('receipt interval must lie inside the block')
        covered = event['ranges'].get(station, [])
        if not any(clo <= lo and hi <= chhi for clo, chhi in covered):
            raise ValueError('receipt interval must be required by the event at that station')
        payload = {'version_id': vid, 'event_id': eid, 'block_id': bid,
                   'interval': [lo, hi]}
        response = {'receipt_id': rid, 'accepted': True, 'block_id': bid,
                    'interval': [lo, hi]}
        self._append_journal('receipt', rid, payload, response)
        self._receipts[rid] = {'receipt_id': rid, 'version_id': vid, 'event_id': eid,
                               'block_id': bid, 'interval': (lo, hi)}
        self._responses[('receipt', rid)] = response
        self._flush_state()
        return json.loads(json.dumps(response))

    # ---------- 原始块下线 ----------

    def request_offline(self, request):
        self._check_open()
        fields(request, ('request_id', 'block_id'))
        qid, bid = ident(request['request_id']), ident(request['block_id'])
        if bid in self._offline:
            stored_q = self._offline[bid]['request_id']
            if qid == stored_q:
                return json.loads(json.dumps(self._responses[('offline', qid)]))
            raise ValueError('block already offline under a different request id')
        if qid in {rec['request_id'] for rec in self._offline.values()}:
            raise ValueError('request id already used for another block')
        if bid not in self._blocks:
            raise ValueError('unknown block')
        station, blo, bhi = self._blocks[bid]
        needed = []
        for event in self._plans[self._current]['events']:
            needed.extend(tuple(iv) for iv in event['ranges'].get(station, []))
        needed = [(max(lo, blo), min(hi, bhi)) for lo, hi in needed
                  if lo < bhi and hi > blo]
        needed = _merge(needed)
        # 归档覆盖必须完好：副本缺陷片段要先扣除，损坏部分不满足下线归档义务。
        archived = []
        for rec in self._receipts.values():
            if rec['block_id'] != bid:
                continue
            for hlo, hhi in self._lineage.healthy_receipt_pieces(
                    rec['receipt_id'], rec['interval']):
                archived.append((hlo, hhi))
        archived = _merge(archived)
        for lo, hi in needed:
            if not any(alo <= lo and hi <= ahi for alo, ahi in archived):
                raise ValueError('block still has required samples without healthy '
                                 'archive; refused')
        payload = {'block_id': bid}
        response = {'request_id': qid, 'approved': True, 'block_id': bid}
        self._append_journal('offline', qid, payload, response)
        self._offline[bid] = {'block_id': bid, 'request_id': qid}
        self._responses[('offline', qid)] = response
        self._flush_state()
        return json.loads(json.dumps(response))

    # ---------- 数据集发布账本：预览 ----------

    def _parse_preview_arguments(self, args, kwargs):
        version_keys = ('version_id', 'version', 'vid', 'clock_version_id',
                        'clock_version')
        event_keys = ('event_ids', 'events', 'candidate_event_ids',
                      'candidate_ids', 'requested_event_ids', 'candidates',
                      'event_id_set')
        blind_keys = ('blind_event_ids', 'must_blind_event_ids', 'blind_ids',
                      'must_blind_ids', 'must_blind', 'blind', 'blind_events',
                      'blind_id_set', 'blind_set', 'test_event_ids',
                      'holdout_event_ids')

        def first_key(data, keys):
            for key in keys:
                if key in data and data[key] is not None:
                    return data[key]
            return None

        payload = {}
        if args and isinstance(args[0], dict):
            payload.update(args[0])
        payload.update(kwargs)

        if args and not isinstance(args[0], dict):
            version = args[0]
            events = args[1] if len(args) > 1 else first_key(payload, event_keys)
            blind = args[2] if len(args) > 2 else first_key(payload, blind_keys)
        else:
            version = first_key(payload, version_keys)
            events = first_key(payload, event_keys)
            blind = first_key(payload, blind_keys)
        if version is None:
            raise ValueError('version_id is required')
        if events is None:
            raise ValueError('event_ids is required')
        return version, events, blind

    def preview_release(self, *args, **kwargs):
        """预览不可拆发布组、历史用途约束和当前归档缺口；不占样本。"""
        self._check_open()
        raw_version, raw_events, raw_blind = self._parse_preview_arguments(args, kwargs)
        vid = ident(raw_version)
        if vid not in self._plans:
            raise ValueError('unknown clock version')
        if isinstance(raw_events, str):
            raw_events = [raw_events]
        if isinstance(raw_blind, str):
            raw_blind = [raw_blind]
        event_ids = _as_id_collection(raw_events, 'event_ids', required=True)
        known_events = {e['event_id'] for e in self._plans[vid]['events']}
        unknown = sorted(event_ids - known_events)
        if unknown:
            raise ValueError(f'unknown event in clock version {vid}: {unknown[0]}')
        blind_ids = _as_id_collection(raw_blind, 'blind_event_ids')
        if not blind_ids <= event_ids:
            raise ValueError('blind_event_ids must be a subset of event_ids')
        preview = self._build_release_preview(vid, sorted(event_ids),
                                             sorted(blind_ids), include_gaps=True)
        core = self._release_core(vid, sorted(event_ids), sorted(blind_ids), [])
        preview['preview_id'] = 'preview-' + _canonical(core)
        return _wrap_usage_values(_json_copy(preview))

    # 语义化别名：不同调用方可能使用 dataset/split/manifest 命名。
    preview_dataset_release = preview_release
    preview_dataset = preview_release
    preview_split = preview_release
    preview_manifest = preview_release
    create_release_preview = preview_release
    build_release_preview = preview_release

    def _release_core(self, vid, event_ids, blind_ids, selected_groups):
        return {
            'directory': self._directory_key(),
            'version_id': vid,
            'event_ids': list(event_ids),
            'blind_event_ids': list(blind_ids),
            'selected_groups': sorted(
                ({'members': list(members), 'usage': usage}
                 for members, usage in selected_groups),
                key=lambda item: item['members']),
        }

    def _build_release_preview(self, vid, event_ids, blind_ids, *, include_gaps):
        plan = self._plans[vid]
        event_map = {event['event_id']: event for event in plan['events']}
        plan_order = {event['event_id']: i for i, event in enumerate(plan['events'])}
        blind_set = set(blind_ids)

        # 按台站收集原始本地刻度区间，排序扫描 overlap-connected components。
        station_intervals = {}
        for eid in event_ids:
            event = event_map[eid]
            for station, ranges in event.get('ranges', {}).items():
                for lo, hi in ranges:
                    station_intervals.setdefault(station, []).append((lo, hi, eid))

        parent = {eid: eid for eid in event_ids}

        def find(eid):
            while parent[eid] != eid:
                parent[eid] = parent[parent[eid]]
                eid = parent[eid]
            return eid

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                # 以更靠前的计划事件作为根，仅为确定性。
                if plan_order[ra] <= plan_order[rb]:
                    parent[rb] = ra
                else:
                    parent[ra] = rb

        for station, rows in station_intervals.items():
            rows.sort(key=lambda row: (row[0], row[1], row[2]))
            cluster_eid = None
            cluster_end = None
            for lo, hi, eid in rows:
                if cluster_eid is None or lo >= cluster_end:
                    # lo == cluster_end 只是端点相接，半开区间不共享样本。
                    cluster_eid, cluster_end = eid, hi
                else:
                    union(eid, cluster_eid)
                    if hi > cluster_end:
                        cluster_eid, cluster_end = eid, hi

        grouped = {}
        for eid in event_ids:
            grouped.setdefault(find(eid), []).append(eid)
        member_lists = [sorted(members) for members in grouped.values()]
        member_lists.sort(key=lambda members: (min(plan_order[m] for m in members),
                                              members))

        groups = []
        for members in member_lists:
            required = {}
            for eid in members:
                for station, ranges in event_map[eid].get('ranges', {}).items():
                    required.setdefault(station, []).extend(
                        (int(lo), int(hi)) for lo, hi in ranges)
            required = {station: _merge(intervals)
                        for station, intervals in sorted(required.items())}
            key = ','.join(members)

            history_matches = []
            train_release_ids = set()
            blind_release_ids = set()
            for old_rid, record in sorted(self._releases.items()):
                old_manifest = record['manifest']
                for old_group in old_manifest.get('groups', []):
                    usage = _canonical_usage(
                        old_group.get('usage') or old_group.get('treatment')
                        or old_group.get('status'))
                    if usage not in ('train', 'blind'):
                        continue
                    for station, old_ranges in old_group.get('sample_unions', {}).items():
                        hits = _intersect_interval_sets(
                            required.get(station, []),
                            [(int(lo), int(hi)) for lo, hi in old_ranges])
                        if not hits:
                            continue
                        history_matches.append({
                            'release_request_id': old_rid,
                            'group_key': old_group.get('group_key'),
                            'usage': usage,
                            'station': station,
                            'intervals': [[lo, hi] for lo, hi in hits],
                        })
                        if usage == 'train':
                            train_release_ids.add(old_rid)
                        else:
                            blind_release_ids.add(old_rid)

            required_blind_events = sorted(set(members) & blind_set)
            history_train = bool(train_release_ids)
            history_blind = bool(blind_release_ids)
            must_blind = bool(required_blind_events)
            blind_required = history_blind or must_blind
            conflict = history_train and blind_required
            if include_gaps:
                gaps, online_gaps, missing_gaps = self._archive_gaps(required)
            else:
                gaps = online_gaps = missing_gaps = []
            # 物理质量缺陷是科学数据无效，独立于归档可读性缺口：不改变
            # query_sources 的 archive/online/missing 分类，只单独列出并
            # 在首次发布时整笔拒绝；预览不裁掉坏样本，候选分组保持原样。
            physical_issues = self._physical_quality_issues(required)
            physical_intervals = [item['interval'] for item in physical_issues]

            group = {
                'id': key,
                'name': key,
                'label': key,
                'group_key': key,
                'key': key,
                'group_id': key,
                'canonical_key': key,
                'members': members,
                'canonical_members': members,
                'canonical_event_ids': members,
                'member_ids': members,
                'event_ids': members,
                'member_event_ids': members,
                'size': len(members),
                'sample_unions': {st: [[lo, hi] for lo, hi in ivs]
                                  for st, ivs in required.items()},
                'required_ranges': {st: [[lo, hi] for lo, hi in ivs]
                                    for st, ivs in required.items()},
                'event_ranges': {
                    eid: _json_copy(event_map[eid].get('ranges', {}))
                    for eid in members
                },
                'events': list(members),
                'event_details': [
                    {'event_id': eid,
                     'ranges': _json_copy(event_map[eid].get('ranges', {}))}
                    for eid in members
                ],
                'history_matches': history_matches,
                'history_train': history_train,
                'history_blind': history_blind,
                'required_blind_events': required_blind_events,
                'must_blind_events': required_blind_events,
                'train_release_ids': sorted(train_release_ids),
                'blind_release_ids': sorted(blind_release_ids),
                'constraints': {
                    'train_from_history': history_train,
                    'blind_from_history': history_blind,
                    'blind_from_request': must_blind,
                },
            }

            if conflict:
                reasons = []
                if history_train:
                    reasons.append('history_train')
                if history_blind:
                    reasons.append('history_blind')
                if must_blind:
                    reasons.append('required_blind_event')
                group.update({
                    'usage': None,
                    'treatment': None,
                    'dataset': None,
                    'split': None,
                    'use': None,
                    'purpose': None,
                    'status': 'conflict',
                    'state': 'conflict',
                    'result': 'conflict',
                    'outcome': 'conflict',
                    'decision': 'conflict',
                    'action': 'conflict',
                    'classification': 'conflict',
                    'conflict': True,
                    'is_conflict': True,
                    'conflict_reasons': reasons,
                    'conflict_between': ['train', 'blind'],
                    'conflict_reason': (
                        'train history/use conflicts with blind history/requirement'),
                    'conflict_message': (
                        'group is pinned to train by published history and to '
                        'blind by blind history or the requested blind subset'),
                    'archive_gaps': gaps,
                    'gaps': gaps,
                    'unarchived_ranges': gaps,
                    'online_gaps': online_gaps,
                    'online': online_gaps,
                    'online_unarchived_ranges': online_gaps,
                    'missing_gaps': missing_gaps,
                    'missing': missing_gaps,
                    'missing_ranges': missing_gaps,
                    'physical_quality_defects': physical_issues,
                    'physical_defects': physical_issues,
                    'physical_quality_intervals': physical_intervals,
                    'has_physical_quality_defect': bool(physical_issues),
                    'has_archive_gaps': bool(gaps),
                    'archive_complete': not gaps,
                    'deliverable': not gaps,
                    'publishable': False,
                })
            else:
                usage = 'blind' if blind_required else 'train'
                group.update({
                    'usage': usage,
                    'treatment': usage,
                    'dataset': usage,
                    'split': usage,
                    'use': usage,
                    'purpose': usage,
                    'status': usage,
                    'state': usage,
                    'result': usage,
                    'outcome': usage,
                    'decision': usage,
                    'action': usage,
                    'classification': usage,
                    'conflict': False,
                    'is_conflict': False,
                    'conflict_reasons': [],
                    'archive_gaps': gaps,
                    'gaps': gaps,
                    'unarchived_ranges': gaps,
                    'online_gaps': online_gaps,
                    'online': online_gaps,
                    'online_unarchived_ranges': online_gaps,
                    'missing_gaps': missing_gaps,
                    'missing': missing_gaps,
                    'missing_ranges': missing_gaps,
                    'physical_quality_defects': physical_issues,
                    'physical_defects': physical_issues,
                    'physical_quality_intervals': physical_intervals,
                    'has_physical_quality_defect': bool(physical_issues),
                    'has_archive_gaps': bool(gaps),
                    'archive_complete': not gaps,
                    'deliverable': not gaps,
                    'publishable': not gaps,
                })
            groups.append(group)

        preview = {
            'schema': RELEASE_PREVIEW_SCHEMA,
            'kind': 'dataset_release_preview',
            'directory': self._directory_key(),
            'path': self._directory_key(),
            'version_id': vid,
            'version': vid,
            'request': {
                'version_id': vid,
                'event_ids': list(event_ids),
                'blind_event_ids': list(blind_ids),
            },
            'normalized_request': {
                'version_id': vid,
                'event_ids': list(event_ids),
                'blind_event_ids': list(blind_ids),
            },
            'batch': {
                'version_id': vid,
                'event_ids': list(event_ids),
                'blind_event_ids': list(blind_ids),
            },
            'candidate_event_ids': list(event_ids),
            'candidates': list(event_ids),
            'event_ids': list(event_ids),
            'blind_event_ids': list(blind_ids),
            'blind_ids': list(blind_ids),
            'must_blind_event_ids': list(blind_ids),
            'must_blind_ids': list(blind_ids),
            'groups': groups,
            'group_count': len(groups),
            'has_conflict': any(g['conflict'] for g in groups),
        }
        return preview

    def _physical_quality_issues(self, required):
        """列出批准组所需区间与物理采样缺陷的交集，逐缺陷保留原因。"""
        from .lineage import intersect_intervals
        issues = []
        for station in sorted(required):
            for defect in self._lineage.defects:
                if defect['scope'] != 'physical' or defect['target'] != station:
                    continue
                hits = intersect_intervals(
                    [tuple(iv) for iv in required[station]],
                    [tuple(defect['interval'])])
                for lo, hi in hits:
                    issues.append({
                        'station': station,
                        'interval': [lo, hi],
                        'range': [lo, hi],
                        'defect_request_id': defect['request_id'],
                        'request_id': defect['request_id'],
                        'kind': 'physical_quality_defect',
                    })
        return issues

    def _archive_gaps(self, required):
        gaps = []
        online_gaps = []
        missing_gaps = []
        for station in sorted(required):
            for lo, hi in required[station]:
                for (glo, ghi), kind, reference in self._classify(station, lo, hi):
                    if kind == 'archive':
                        continue
                    item = {
                        'station': station,
                        'interval': [glo, ghi],
                        'range': [glo, ghi],
                        'kind': kind,
                        'gap_type': ('online_unarchived' if kind == 'online'
                                     else 'missing'),
                        'archived': False,
                        'reference': reference,
                        'block_id': reference if kind == 'online' else None,
                    }
                    gaps.append(item)
                    if kind == 'online':
                        online_gaps.append(item)
                    else:
                        missing_gaps.append(item)
        return gaps, online_gaps, missing_gaps

    # ---------- 数据集发布账本：发布与读取 ----------

    @staticmethod
    def _looks_like_preview(value):
        return (isinstance(value, dict)
                and isinstance(value.get('groups'), list)
                and ('version_id' in value or 'version' in value)
                and 'release_request_id' not in value)

    def _parse_publish_arguments(self, args, kwargs):
        rid_keys = ('request_id', 'release_request_id', 'publish_request_id',
                    'publish_id', 'release_id', 'id')
        preview_keys = ('preview', 'release_preview', 'preview_request',
                        'preview_result')
        groups_keys = ('group_keys', 'approved_group_keys', 'selected_group_keys',
                       'approved_keys', 'selected_keys', 'keys',
                       'groups', 'approved_groups', 'selected_groups',
                       'approvals', 'selection', 'selected', 'approved',
                       'choices')

        rid = kwargs.pop('request_id', None)
        preview = kwargs.pop('preview', None)
        group_selection = kwargs.pop('group_keys', None)
        for key in rid_keys:
            if rid is None and key in kwargs:
                rid = kwargs[key]
        for key in preview_keys:
            if preview is None and key in kwargs:
                preview = kwargs[key]
        for key in groups_keys:
            if group_selection is None and key in kwargs:
                group_selection = kwargs[key]

        positional = list(args)
        envelope = None
        if positional and isinstance(positional[0], dict) and not self._looks_like_preview(positional[0]):
            envelope = positional[0]
            positional = []
        elif kwargs.get('request') is not None and isinstance(kwargs['request'], dict):
            envelope = kwargs['request']

        if envelope is not None:
            for key in rid_keys:
                if rid is None and key in envelope:
                    rid = envelope[key]
            for key in preview_keys:
                if preview is None and key in envelope:
                    preview = envelope[key]
            for key in groups_keys:
                if group_selection is None and key in envelope:
                    group_selection = envelope[key]

        # 支持 (preview, group_keys, request_id) 与 (request_id, preview, group_keys)。
        if positional and self._looks_like_preview(positional[0]):
            preview = preview or positional[0]
            rest = list(positional[1:])
            if len(rest) >= 2 and isinstance(rest[0], str) and not isinstance(rest[1], str):
                # 少见顺序：preview, request_id, group_keys
                rid = rid or rest[0]
                group_selection = group_selection if group_selection is not None else rest[1]
            elif len(rest) >= 2 and not isinstance(rest[0], str) and isinstance(rest[1], str):
                group_selection = group_selection if group_selection is not None else rest[0]
                rid = rid or rest[1]
            elif rest:
                # 两个字符串时按文档约定视为 group_key 后 request_id。
                if group_selection is None:
                    group_selection = rest[0]
                if rid is None and len(rest) >= 2:
                    rid = rest[1]
        elif positional and isinstance(positional[0], str) and len(positional) >= 2 \
                and self._looks_like_preview(positional[1]):
            rid = rid or positional[0]
            preview = preview or positional[1]
            if group_selection is None and len(positional) >= 3:
                group_selection = positional[2]
        else:
            # 兜底：从任意位置找预览；请求 ID 优先取剩余的第一个字符串，
            # 否则取最后一个字符串，以兼容两种常见位置。
            for value in positional:
                if self._looks_like_preview(value):
                    preview = preview or value
            leftovers = [value for value in positional if value is not preview]
            if rid is None:
                for value in leftovers:
                    if isinstance(value, str):
                        rid = value
                        leftovers.remove(value)
                        break
            if rid is None:
                for value in reversed(leftovers):
                    if isinstance(value, str):
                        rid = value
                        leftovers.remove(value)
                        break
            if group_selection is None and leftovers:
                group_selection = leftovers[0] if len(leftovers) == 1 else leftovers
        return rid, preview, group_selection

    def _preview_semantic_parts(self, preview):
        if not self._looks_like_preview(preview):
            raise ReleaseRequestError('a directory-issued release preview is required')
        preview_directory = preview.get('directory', preview.get('path'))
        if preview_directory is not None:
            try:
                preview_path = Path(str(preview_directory)).resolve()
            except (OSError, ValueError):
                raise ReleaseRequestError('preview is from an unknown directory')
            if preview_path != self._path.resolve():
                raise ReleaseRequestError('preview was issued by another review directory')

        vid = ident(preview.get('version_id', preview.get('version')))
        request = preview.get('request') if isinstance(preview.get('request'), dict) else {}
        raw_events = request.get('event_ids', preview.get(
            'candidate_event_ids', preview.get('event_ids')))
        raw_blind = request.get('blind_event_ids', preview.get(
            'blind_event_ids', preview.get('must_blind_event_ids')))
        if isinstance(raw_events, str):
            raw_events = [raw_events]
        if isinstance(raw_blind, str):
            raw_blind = [raw_blind]
        event_ids = sorted(_as_id_collection(raw_events, 'event_ids', required=True))
        blind_ids = sorted(_as_id_collection(raw_blind, 'blind_event_ids'))
        if not blind_ids:
            blind_ids = []
        if not set(blind_ids) <= set(event_ids):
            raise ReleaseRequestError('preview blind_event_ids are inconsistent')
        if vid not in self._plans:
            raise ReleaseRequestError('unknown clock version in preview')
        known = {e['event_id'] for e in self._plans[vid]['events']}
        if not set(event_ids) <= known:
            raise ReleaseRequestError('preview contains an unknown event')
        return vid, event_ids, blind_ids

    def _select_preview_groups(self, preview, selection):
        if selection is None:
            raise ReleaseRequestError('approved group_keys must be non-empty')

        groups = preview.get('groups', [])
        by_key = {}
        by_members = {}
        for group in groups:
            members = tuple(sorted(ident(eid) for eid in group.get(
                'members', group.get('event_ids', ()))))
            if not members:
                continue
            by_members[members] = group
            for key in (group.get('group_key'), group.get('key'), group.get('group_id')):
                if key is not None:
                    by_key[key] = group

        # 允许直接把一个规范成员集合作为“单个组键”传入，例如 ["e1","e2","e3"]。
        if isinstance(selection, (list, tuple, set, frozenset)):
            try:
                direct_members = tuple(sorted(_as_id_collection(
                    selection, 'group_key', required=True)))
            except ValueError:
                direct_members = None
            if direct_members in by_members:
                entries = [(by_members[direct_members], None)]
            else:
                entries = self._selection_entries(selection)
        else:
            entries = self._selection_entries(selection)
        if not entries:
            raise ReleaseRequestError('approved group_keys must be non-empty')

        selected = {}
        for raw_selector, option in entries:
            usage_override = None
            if isinstance(option, str):
                usage_override = _canonical_usage(option)
                if usage_override is None and option.lower() in ('false', 'no', '0'):
                    raise ReleaseRequestError('group approval was explicitly rejected')
            elif option is False:
                raise ReleaseRequestError('group approval was explicitly rejected')
            elif isinstance(option, dict):
                if option.get('approved') is False:
                    raise ReleaseRequestError('group approval was explicitly rejected')
                usage_override = _canonical_usage(
                    option.get('usage', option.get('treatment', option.get('status'))))

            selector = raw_selector
            members_hint = None
            if isinstance(selector, dict):
                option = selector
                selector = option.get('group_key', option.get(
                    'key', option.get('group_id')))
                members_hint = option.get('members', option.get('event_ids'))
                if usage_override is None:
                    usage_override = _canonical_usage(option.get(
                        'usage', option.get('treatment', option.get('status'))))

            group = None
            if isinstance(selector, int) and not isinstance(selector, bool):
                if 0 <= selector < len(groups):
                    group = groups[selector]
            elif selector is not None and not isinstance(selector, (list, tuple, set, frozenset, dict)):
                try:
                    group = by_key.get(selector)
                except TypeError:
                    group = None
            if group is None and members_hint is not None:
                members = tuple(sorted(_as_id_collection(
                    members_hint, 'selected group members', required=True)))
                group = by_members.get(members)
            if group is None and isinstance(
                    selector, (list, tuple, set, frozenset)):
                members = tuple(sorted(_as_id_collection(
                    selector, 'selected group members', required=True)))
                group = by_members.get(members)
            if group is None and isinstance(selector, str):
                try:
                    members = tuple(sorted(_as_id_collection(
                        selector.split(','), 'group_key')))
                    group = by_members.get(members)
                except ValueError:
                    group = None
            if group is None:
                raise ReleaseRequestError('selected group does not match the preview')
            if group.get('conflict') or group.get('status') == 'conflict':
                raise ReleaseConflictError('cannot approve a conflicted release group')
            usage = _canonical_usage(group.get('usage')) or _canonical_usage(
                group.get('treatment')) or _canonical_usage(group.get('status'))
            if usage not in ('train', 'blind'):
                raise ReleaseRequestError('selected group has no releasable usage')
            if usage_override is not None and usage_override != usage:
                raise ReleaseRequestError(
                    'selected usage does not match the approved preview usage')
            members = tuple(sorted(group['members']))
            selected[members] = usage
        return sorted((members, usage) for members, usage in selected.items())

    @staticmethod
    def _selection_entries(selection):
        """把多种批准参数形态归一为 (selector, option)。"""
        if isinstance(selection, dict):
            if any(key in selection for key in
                   ('members', 'group_key', 'key', 'group_id')):
                return [(selection, None)]
            return list(selection.items())
        if isinstance(selection, (str, bytes)):
            return [(selection, None)]
        if not isinstance(selection, Iterable):
            return [(selection, None)]

        entries = []
        for item in selection:
            # [("e4", "train"), ...] 或 dict.items() 风格；但 ("e1","e2")
            # 这种两成员键没有可识别用途，仍按单个成员集合处理。
            if (isinstance(item, tuple) and len(item) == 2
                    and (isinstance(item[1], dict) or item[1] is True
                         or item[1] is False or _canonical_usage(item[1]))):
                entries.append((item[0], item[1]))
            else:
                entries.append((item, None))
        return entries

    def publish_release(self, *args, **kwargs):
        """原子发布预览中批准的非空组集合；成功 ID 等价重放返回原清单。"""
        self._check_open()
        rid_raw, preview, selection = self._parse_publish_arguments(args, kwargs)
        if rid_raw is None:
            raise ReleaseRequestError('release request_id is required')
        rid = ident(rid_raw)
        if preview is None:
            inline_request = next(
                (value for value in args
                 if isinstance(value, dict)
                 and ('version_id' in value or 'version' in value)
                 and ('event_ids' in value or 'events' in value)),
                None)
            if inline_request is None and isinstance(kwargs, dict) and (
                    'version_id' in kwargs or 'version' in kwargs):
                inline_request = kwargs
            if inline_request is not None:
                preview = self.preview_release(inline_request)
        if preview is None:
            raise ReleaseRequestError('a directory-issued release preview is required')

        vid, event_ids, blind_ids = self._preview_semantic_parts(preview)
        selected = self._select_preview_groups(preview, selection)
        semantic = self._release_core(vid, event_ids, blind_ids, selected)

        existing = self._releases.get(rid)
        if existing is not None:
            if _canonical(semantic) != _canonical(existing['payload']):
                raise ReleaseRequestError(
                    'release request id reused with different semantic request')
            return _wrap_usage_values(_json_copy(existing['manifest']))

        # 首次发布必须按当前账本重算完整候选分组与历史用途。
        current = self._build_release_preview(
            vid, event_ids, blind_ids, include_gaps=False)
        current_by_members = {tuple(g['members']): g for g in current['groups']}
        approved = []
        for members, approved_usage in selected:
            group = current_by_members.get(tuple(members))
            if group is None:
                raise ReleaseRequestError(
                    'approved group can no longer be reproduced from full candidates')
            if group.get('conflict'):
                raise ReleaseConflictError(
                    'approved group is now constrained to both train and blind: '
                    + ','.join(group.get('conflict_reasons', [])))
            if group.get('usage') != approved_usage:
                raise ReleaseRequestError(
                    f"approved group {group['group_key']} usage changed from "
                    f"{approved_usage} to {group.get('usage')}; issue a new preview")
            # 首次成功发布必须拒绝任何含物理采样质量缺陷的批准组；副本损坏
            # 已在 _classify 中表现为 online/missing 归档缺口，可换完好副本后
            # 重试，物理缺陷则不能靠换回执或裁剪事件范围绕过。
            if group.get('has_physical_quality_defect'):
                details = ', '.join(
                    f"{item['station']}[{item['interval'][0]},{item['interval'][1]})"
                    f"<-{item['defect_request_id']}"
                    for item in group['physical_quality_defects'])
                raise ReleaseRequestError(
                    f"refuse release {rid}: group {group['group_key']} contains "
                    f"physically invalid samples: {details}")
            approved.append(group)

        manifest = self._build_release_manifest(
            rid, vid, event_ids, blind_ids, semantic, approved)
        self._append_journal('release', rid, semantic, manifest)
        self._releases[rid] = {'payload': semantic, 'manifest': manifest}
        self._responses[('release', rid)] = manifest
        self._apply_release_usage(manifest)
        self._flush_state()
        return _wrap_usage_values(_json_copy(manifest))

    publish_dataset_release = publish_release
    publish_dataset = publish_release
    publish_split = publish_release
    commit_release = publish_release
    issue_release = publish_release
    create_release = publish_release

    def _build_release_manifest(self, rid, vid, event_ids, blind_ids,
                                semantic, approved_groups):
        plan = self._plans[vid]
        event_map = {event['event_id']: event for event in plan['events']}
        # 发布时显式走现有 query_sources 口径；其底层与预览共用 _classify。
        source_report = self.query_sources(vid)
        source_map = {}
        for event_sources in source_report['events']:
            for station_sources in event_sources['stations']:
                source_map[(event_sources['event_id'],
                            station_sources['station'])] = station_sources['sources']
        groups_out = []
        waveform_refs = []

        for group in approved_groups:
            members = list(group['members'])
            usage = group['usage']
            key = group['group_key']
            event_entries = []
            group_refs = []

            for eid in members:
                event = event_map[eid]
                ranges = _json_copy(event.get('ranges', {}))
                source_ranges = []
                event_refs = []
                for station, intervals in ranges.items():
                    report_sources = source_map.get((eid, station), [])
                    for raw_lo, raw_hi in intervals:
                        lo, hi = int(raw_lo), int(raw_hi)
                        pieces = []
                        for source in report_sources:
                            alo, ahi = map(int, source['interval'])
                            if lo <= alo and ahi <= hi:
                                pieces.append(((alo, ahi), source['kind'],
                                               source['reference']))
                        pieces.sort(key=lambda item: item[0])
                        cursor = lo
                        for (alo, ahi), kind, receipt_id in pieces:
                            if alo > cursor:
                                raise ReleaseRequestError(
                                    f'refuse release {rid}: {eid} at {station} '
                                    f'[{cursor},{alo}) has no source coverage')
                            cursor = max(cursor, ahi)
                            if kind != 'archive':
                                raise ReleaseRequestError(
                                    f'refuse release {rid}: {eid} at {station} '
                                    f'[{alo},{ahi}) is not permanently archived')
                            receipt = self._receipts[receipt_id]
                            ref = {
                                'release_request_id': rid,
                                'group_key': key,
                                'usage': usage,
                                'event_id': eid,
                                'station': station,
                                'requested_range': [lo, hi],
                                'interval': [alo, ahi],
                                'range': [alo, ahi],
                                'source': 'archive',
                                'kind': 'archive',
                                'receipt_id': receipt_id,
                                'reference': receipt_id,
                                'block_id': receipt['block_id'],
                                'receipt_interval': [int(x) for x in receipt['interval']],
                            }
                            event_refs.append(ref)
                            group_refs.append(ref)
                            waveform_refs.append(ref)
                            sources_entry = {
                                'interval': [alo, ahi],
                                'kind': 'archive',
                                'reference': receipt_id,
                                'receipt_id': receipt_id,
                                'block_id': receipt['block_id'],
                            }
                        if cursor != hi:
                            raise ReleaseRequestError(
                                f'refuse release {rid}: {eid} at {station} '
                                f'[{cursor},{hi}) has no source coverage')
                        source_ranges.append({
                            'station': station,
                            'range': [lo, hi],
                            'sources': [
                                {'interval': [alo, ahi], 'kind': 'archive',
                                 'reference': receipt_id, 'receipt_id': receipt_id}
                                for (alo, ahi), _, receipt_id in pieces
                            ],
                        })
                event_entries.append({
                    'event_id': eid,
                    'ranges': ranges,
                    'source_ranges': source_ranges,
                    'archive_refs': event_refs,
                    'waveform_refs': event_refs,
                })

            groups_out.append({
                'id': key,
                'name': key,
                'label': key,
                'group_key': key,
                'key': key,
                'group_id': key,
                'canonical_key': key,
                'members': members,
                'canonical_members': members,
                'canonical_event_ids': members,
                'member_ids': members,
                'event_ids': members,
                'usage': usage,
                'treatment': usage,
                'dataset': usage,
                'split': usage,
                'use': usage,
                'purpose': usage,
                'action': usage,
                'decision': usage,
                'state': usage,
                'status': usage,
                'sample_unions': group['sample_unions'],
                'required_ranges': group['required_ranges'],
                'events': event_entries,
                'archive_refs': group_refs,
                'waveform_refs': group_refs,
            })

        return {
            'schema': RELEASE_MANIFEST_SCHEMA,
            'kind': 'dataset_release_manifest',
            'release_request_id': rid,
            'request_id': rid,
            'directory': semantic['directory'],
            'version_id': vid,
            'version': vid,
            'candidate_event_ids': list(event_ids),
            'candidates': list(event_ids),
            'event_ids': list(event_ids),
            'blind_event_ids': list(blind_ids),
            'blind_ids': list(blind_ids),
            'groups': groups_out,
            'approved_group_keys': [g['group_key'] for g in groups_out],
            'selected_group_keys': [g['group_key'] for g in groups_out],
            'released_group_keys': [g['group_key'] for g in groups_out],
            'archive_refs': waveform_refs,
            'sources': waveform_refs,
            'waveform_refs': waveform_refs,
            'ref_count': len(waveform_refs),
        }

    def read_release(self, request_id):
        """按发布请求 ID 读取首次成功发布时固定的清单；未知 ID 报错。"""
        self._check_open()
        rid = ident(request_id)
        record = self._releases.get(rid)
        if record is None:
            raise UnknownReleaseError(f'no published release for request id {rid}')
        return _wrap_usage_values(_json_copy(record['manifest']))

    def get_release(self, request_id):
        """读取已发布清单；未知请求 ID 返回 None。"""
        return self.find_release(request_id)

    def read_published_manifest(self, request_id):
        return self.read_release(request_id)

    def get_published_manifest(self, request_id):
        return self.find_release(request_id)

    def get_published_release(self, request_id):
        return self.find_release(request_id)

    def read_dataset_release(self, request_id):
        return self.read_release(request_id)

    def find_release(self, request_id):
        """不抛异常的查询入口；未知 ID 返回 None。"""
        self._check_open()
        try:
            rid = ident(request_id)
        except ValueError:
            return None
        record = self._releases.get(rid)
        return None if record is None else _wrap_usage_values(_json_copy(record['manifest']))

    def list_releases(self):
        """按请求 ID 字典序列出已成功发布的请求 ID。"""
        self._check_open()
        return sorted(self._releases)

    # 更短/近义 API 名：read_* 与既有 read_plan 一样对未知 ID 报错；
    # get_*/find_* 对未知 ID 返回 None。
    read_manifest = read_release
    manifest = read_release
    get_manifest = find_release
    get_release_manifest = find_release
    read_release_manifest = read_release
    get_published = find_release
    published_manifest = find_release
    preview = preview_release
    release_preview = preview_release
    publish = publish_release
    release = publish_release
    publish_manifest = publish_release
    commit_dataset_release = publish_release

    @property
    def ledger(self):
        return ReleaseLedger(self)

    def release_ledger(self):
        return ReleaseLedger(self)

    # ---------- 查询 ----------

    def block_status(self, block_id):
        self._check_open()
        bid = ident(block_id)
        if bid not in self._blocks:
            raise ValueError('unknown block')
        return 'offline' if bid in self._offline else 'online'

    def query_sources(self, version_id=None):
        """整个取样计划的区间来源：归档回执 ID / 在线原块 / 缺失。"""
        self._check_open()
        vid = self._current if version_id is None else ident(version_id)
        if vid not in self._plans:
            raise ValueError('unknown clock version')
        plan = self._plans[vid]
        result = []
        for event in plan['events']:
            stations_out = []
            for station in event['stations']:
                pieces = []
                for lo, hi in event['ranges'][station]:
                    pieces.extend(self._classify(station, lo, hi))
                stations_out.append({'station': station,
                                     'ranges': [[lo, hi] for lo, hi in
                                                _merge(p[0] for p in pieces)],
                                     'sources': [{'interval': list(iv), 'kind': kind,
                                                  'reference': ref}
                                                 for iv, kind, ref in pieces]})
            result.append({'event_id': event['event_id'], 'stations': stations_out})
        return {'version_id': vid, 'events': result}

    def _classify(self, station, lo, hi):
        """把 [lo,hi) 的每个整数采样点切成 归档/在线/缺失 片段。"""
        # 与原块、块空洞求交
        block_ivs = []
        for bid, (bst, blo, bhi) in self._blocks.items():
            if bst == station and blo < hi and bhi > lo:
                block_ivs.append((max(lo, blo), min(hi, bhi), bid))
        block_ivs.sort()
        covered_by_blocks = _merge((a, b) for a, b, _ in block_ivs)
        missing = []
        cursor = lo
        for clo, chi in covered_by_blocks:
            if clo > cursor:
                missing.append((cursor, clo))
            cursor = max(cursor, chi)
        if cursor < hi:
            missing.append((cursor, hi))

        receipts = sorted(
            (rec for rec in self._receipts.values()
             if self._blocks[rec['block_id']][0] == station
             and rec['interval'][0] < hi and rec['interval'][1] > lo),
            key=lambda rec: (rec['interval'][0], rec['interval'][1], rec['receipt_id']))
        out = []
        for blo, bhi, bid in block_ivs:
            seg_lo, seg_hi = max(lo, blo), min(hi, bhi)
            # 副本缺陷按区间排除：先把每份回执切成其完好片段，再参与覆盖竞争；
            # 不会因一小段损坏而舍弃整张回执。物理质量缺陷不改变副本可读性，
            # 因此不在这里扣除（它在发布/凭证环节单独裁决）。
            arch = []
            for rec in receipts:
                if rec['block_id'] != bid:
                    continue
                clipped = (max(rec['interval'][0], seg_lo),
                           min(rec['interval'][1], seg_hi))
                if clipped[0] >= seg_hi or clipped[1] <= seg_lo:
                    continue
                for hlo, hhi in self._lineage.healthy_receipt_pieces(
                        rec['receipt_id'], clipped):
                    if hlo < seg_hi and hhi > seg_lo:
                        arch.append((hlo, hhi, rec['receipt_id']))
            # 边界扫描：每个子区间选覆盖它的字典序最小回执，不枚举采样点
            boundaries = sorted({seg_lo, seg_hi} |
                                {x for alo, ahi, _ in arch for x in (alo, ahi)})
            for left, right in zip(boundaries, boundaries[1:]):
                covering = sorted(rid for alo, ahi, rid in arch
                                  if alo <= left and ahi >= right)
                if covering:
                    if out and out[-1][1] == 'archive' and out[-1][2] == covering[0] \
                            and out[-1][0][1] == left:
                        out[-1] = ((out[-1][0][0], right), 'archive', covering[0])
                    else:
                        out.append(((left, right), 'archive', covering[0]))
                else:
                    kind = 'missing' if bid in self._offline else 'online'
                    ref = None if bid in self._offline else bid
                    if out and out[-1][1] == kind and out[-1][2] == ref \
                            and out[-1][0][1] == left:
                        out[-1] = ((out[-1][0][0], right), kind, ref)
                    else:
                        out.append(((left, right), kind, ref))
        out.extend((iv, 'missing', None) for iv in missing)
        out.sort(key=lambda item: (item[0][0], item[0][1], item[1], item[2] or ''))
        return out

    # ---------- 样本级特征血缘：原始输入节点 ----------

    def _published_group(self, release_request_id, group_key):
        record = self._releases.get(ident(release_request_id))
        if record is None:
            raise ValueError('unknown release_request_id; reference a release '
                             'successfully published in this directory')
        for group in record['manifest'].get('groups', []):
            if group.get('group_key') == ident(group_key):
                return record, group
        raise ValueError('unknown group_key in the referenced release')

    def _group_usage(self, group):
        usage = _canonical_usage(
            group.get('usage') or group.get('treatment') or group.get('status'))
        if usage not in ('train', 'blind'):
            raise ValueError('referenced release group has no train/blind usage')
        return usage

    def capture_waveform_input(self, request):
        """重新实际取数并固定一个原始输入节点（不读波形数值）。

        完整调用示例::

            d.capture_waveform_input({
                "node_id": "R",
                "release_request_id": "pub1",
                "group_key": "e1",
                "station": "A",
                "interval": [100, 120],
            })

        规则：区间必须全部属于已发布组在该站的 ``sample_unions``，不允许借
        组外事件扩张；只能选当前完好的归档副本（重叠片段取回执 ID 字典序
        最小者），在线原块不兜底；物理采样缺陷或任何不能全段归档交付的片
        段都会整笔拒绝。成功后节点实际来源永久固定，重取不修改原发布清单。
        """
        self._check_open()
        fields(request, ('node_id', 'release_request_id', 'group_key',
                         'station', 'interval'))
        node_id = ident(request['node_id'])
        if node_id in self._lineage.nodes:
            existing = self._lineage.nodes[node_id]
            if existing['kind'] != 'raw':
                raise ValueError('node id already defined; node ids cannot be redefined')
            return self._replay_raw_node(node_id, request)
        group_key = ident(request['group_key'])
        station = ident(request['station'])
        lo, hi = _ival(request['interval'])
        record, group = self._published_group(
            request['release_request_id'], group_key)
        release_id = ident(request['release_request_id'])
        if station not in group.get('sample_unions', {}):
            raise ValueError('station is not part of the referenced release group')
        unions = [tuple(iv) for iv in group['sample_unions'][station]]
        if not any(ulo <= lo and hi <= uhi for ulo, uhi in unions):
            raise ValueError('interval must lie entirely inside the group sample_unions '
                             'for that station; no expansion via events outside the group')
        if station not in self._domains:
            raise ValueError('unknown station')
        if not self._domains[station][0] <= lo < hi <= self._domains[station][1]:
            raise ValueError('interval outside station sampling domain')
        # 物理采样缺陷：科学数据无效，不能靠换副本或假装存储缺失绕过。
        if self._lineage.has_physical_defect(station, [(lo, hi)]):
            raise ValueError('refuse capture: requested interval contains physically '
                             'invalid samples')
        # 只允许当前完好归档；_classify 已扣除副本缺陷片段并按字典序选回执，
        # online/missing 片段一律拒绝，不做在线原块兜底。
        pieces = self._classify(station, lo, hi)
        if any(kind != 'archive' for _, kind, _ in pieces):
            raise ValueError('refuse capture: interval cannot be delivered entirely '
                             'from healthy archived copies')
        covered = _merge(iv for iv, _, _ in pieces)
        if covered != [(lo, hi)]:
            raise ValueError('refuse capture: archived coverage is not a complete '
                             'partition of the requested interval')
        fragments = []
        for (flo, fhi), _, receipt_id in pieces:
            receipt = self._receipts[receipt_id]
            fragments.append({
                'receipt_id': receipt_id,
                'block_id': receipt['block_id'],
                'interval': [flo, fhi],
            })
        usage = self._group_usage(group)
        payload = {
            'node_id': node_id,
            'kind': 'raw',
            'station': station,
            'interval': [lo, hi],
            'length': hi - lo,
            'fragments': fragments,
            'release_request_id': release_id,
            'group_key': group_key,
            'usage': usage,
        }
        response = {
            'node_id': node_id,
            'accepted': True,
            'kind': 'raw',
            'station': station,
            'interval': [lo, hi],
            'length': hi - lo,
            'output_domain': [0, hi - lo],
            'fragments': [dict(frag) for frag in fragments],
            'release_request_id': release_id,
            'group_key': group_key,
            'usage': usage,
        }
        self._commit_lineage('raw_node', node_id, payload, response)
        return _json_copy(response)

    def _replay_raw_node(self, node_id, request):
        stored = self._lineage.nodes[node_id]
        candidate_key = {
            'release_request_id': ident(request['release_request_id']),
            'group_key': ident(request['group_key']),
            'station': ident(request['station']),
            'interval': list(_ival(request['interval'])),
        }
        stored_key = {
            'release_request_id': stored['release_request_id'],
            'group_key': stored['group_key'],
            'station': stored['station'],
            'interval': list(stored['interval']),
        }
        if _canonical(candidate_key) != _canonical(stored_key):
            raise ValueError('node id reused with different capture input')
        return _json_copy(self._responses[('raw_node', node_id)])

    # ---------- 样本级特征血缘：特征节点 ----------

    def register_window_feature(self, request):
        """登记一个已生成滑窗特征的依赖声明（不实现/校验特征算法本身）。

        完整调用示例::

            d.register_window_feature({
                "node_id": "F",
                "length": 16,
                "parents": [{"node_id": "R", "offset": 0, "width": 5}],
            })

        每个输出位置 ``i`` 同时依赖每个父节点的完整窗口
        ``[i+offset, i+offset+width)``；窗口必须完整落在父输出域内，不截断、
        不补零。一至两个不同父节点，深度最多四层，父项顺序无语义差别。
        父节点用途必须一致，特征继承该用途。登记只记录依赖，父节点已局部
        无效也允许登记，无效区间按窗口规则另行传播。
        """
        self._check_open()
        fields(request, ('node_id', 'length', 'parents'))
        node_id = ident(request['node_id'])
        if node_id in self._lineage.nodes:
            existing = self._lineage.nodes[node_id]
            if existing['kind'] != 'feature':
                raise ValueError('node id already defined; node ids cannot be redefined')
            return self._replay_feature_node(node_id, request)
        length = _bounded_int(request['length'], 1, MAX_NODE_LENGTH)
        parents_in = array(request['parents'], 1, 2)
        parents = []
        parent_ids = []
        max_depth = 0
        usage = None
        for raw_parent in parents_in:
            fields(raw_parent, ('node_id', 'offset', 'width'))
            parent_id = ident(raw_parent['node_id'])
            if parent_id not in self._lineage.nodes:
                raise ValueError(f'unknown parent node: {parent_id}')
            parent_node = self._lineage.nodes[parent_id]
            offset = _bounded_int(raw_parent['offset'], 0, MAX_NODE_LENGTH - 1)
            width = _bounded_int(raw_parent['width'], 1, MAX_NODE_LENGTH)
            # 每个输出位置 i 的窗口 [i+offset, i+offset+width) 必须完整在域内。
            if length - 1 + offset + width > parent_node['length']:
                raise ValueError(
                    f'parent window exceeds parent output domain for {parent_id}')
            if parent_node['usage'] is None:
                raise ValueError('parent node has no inherited usage')
            if usage is None:
                usage = parent_node['usage']
            elif parent_node['usage'] != usage:
                raise ValueError('all parent nodes must share the same train/blind usage')
            parents.append({'node_id': parent_id, 'offset': offset, 'width': width})
            parent_ids.append(parent_id)
            max_depth = max(max_depth, parent_node.get('depth', 0))
        if len(set(parent_ids)) != len(parent_ids):
            raise ValueError('duplicate parent entries are not allowed')
        depth = max_depth + 1
        if depth > MAX_FEATURE_DEPTH:
            raise ValueError(f'feature depth must not exceed {MAX_FEATURE_DEPTH}')
        # 父项顺序不构成语义差别：规范化排序后重放等价性与声明内容都稳定。
        parents.sort(key=lambda parent: parent['node_id'])
        payload = {
            'node_id': node_id,
            'kind': 'feature',
            'length': length,
            'parents': parents,
            'depth': depth,
            'usage': usage,
        }
        response = {
            'node_id': node_id,
            'accepted': True,
            'kind': 'feature',
            'length': length,
            'output_domain': [0, length],
            'parents': [dict(parent) for parent in parents],
            'depth': depth,
            'usage': usage,
        }
        self._commit_lineage('feature_node', node_id, payload, response)
        return _json_copy(response)

    def _replay_feature_node(self, node_id, request):
        stored = self._lineage.nodes[node_id]
        try:
            candidate_parents = sorted(
                (ident(parent['node_id']),
                 _bounded_int(parent['offset'], 0, MAX_NODE_LENGTH - 1),
                 _bounded_int(parent['width'], 1, MAX_NODE_LENGTH))
                for parent in request['parents'])
        except (KeyError, TypeError, ValueError):
            raise ValueError('node id reused with different feature declaration')
        stored_parents = sorted(
            (parent['node_id'], parent['offset'], parent['width'])
            for parent in stored['parents'])
        try:
            candidate_length = _bounded_int(request['length'], 1, MAX_NODE_LENGTH)
        except (KeyError, TypeError, ValueError):
            raise ValueError('node id reused with different feature declaration')
        if candidate_length != stored['length'] or candidate_parents != stored_parents:
            raise ValueError('node id reused with different feature declaration')
        return _json_copy(self._responses[('feature_node', node_id)])

    # ---------- 两类缺陷 ----------

    def record_sample_defect(self, request):
        """登记一条持续有效、可重叠的样本缺陷（不可撤销、不自动修复）。

        副本缺陷::

            d.record_sample_defect({
                "request_id": "d-r0-108",
                "scope": "receipt",
                "target": "r0",
                "interval": [108, 110],
            })

        物理采样缺陷（目标为台站）::

            d.record_sample_defect({
                "request_id": "d-A-108",
                "scope": "physical",
                "target": "A",
                "interval": [108, 110],
            })

        ``scope=receipt`` 只让固定使用该回执的原始输入在相交位置失效；
        ``scope=physical`` 影响该站所有实际使用这些采样的原始输入，与回执、
        事件、校时版或发布请求无关。另一台站的相同刻度不受影响。
        """
        self._check_open()
        fields(request, ('request_id', 'scope', 'target', 'interval'))
        defect_id = ident(request['request_id'])
        if defect_id in self._lineage.defect_ids:
            stored = next(d for d in self._lineage.defects
                          if d['request_id'] == defect_id)
            candidate = {
                'scope': request['scope'],
                'target': ident(request['target']),
                'interval': list(_ival(request['interval'])),
            }
            stored_key = {
                'scope': stored['scope'],
                'target': stored['target'],
                'interval': list(stored['interval']),
            }
            if _canonical(candidate) == _canonical(stored_key):
                return _json_copy(self._responses[('defect', defect_id)])
            raise ValueError('defect request id reused with different content')
        scope = request['scope']
        if not isinstance(scope, str) or scope not in ('receipt', 'physical'):
            raise ValueError("scope must be 'receipt' or 'physical'")
        target = ident(request['target'])
        lo, hi = _ival(request['interval'])
        if scope == 'receipt':
            if target not in self._receipts:
                raise ValueError('unknown receipt_id')
            rlo, rhi = self._receipts[target]['interval']
            if not rlo <= lo < hi <= rhi:
                raise ValueError('receipt defect interval must lie inside the receipt')
        else:
            if target not in self._domains:
                raise ValueError('unknown station')
            dlo, dhi = self._domains[target]
            if not dlo <= lo < hi <= dhi:
                raise ValueError('physical defect interval must lie inside the '
                                 'station sampling domain')
        payload = {
            'request_id': defect_id,
            'scope': scope,
            'target': target,
            'interval': [lo, hi],
        }
        response = {
            'request_id': defect_id,
            'accepted': True,
            'scope': scope,
            'target': target,
            'interval': [lo, hi],
        }
        self._commit_lineage('defect', defect_id, payload, response)
        return _json_copy(response)

    def feature_health(self, node_id):
        """返回节点完整输出域、规范无效并集及按缺陷请求 ID 分列的无效区间。

        查询反映当前最新已登记缺陷；节点来源与依赖声明本身保持不变。
        每组区间升序、无空段、不重叠，相邻同类影响合并；没有影响的缺陷
        不列出。整个计算只做区间代数，不逐点展开输出。
        """
        self._check_open()
        node_id = ident(node_id)
        if node_id not in self._lineage.nodes:
            raise UnknownNodeError(f'unknown feature node: {node_id}')
        node = self._lineage.nodes[node_id]
        overall, per_defect = self._lineage.health(node_id)
        return {
            'node_id': node_id,
            'kind': node['kind'],
            'output_domain': [0, node['length']],
            'invalid_intervals': [[lo, hi] for lo, hi in overall],
            'invalid_by_defect': {
                defect_id: [[lo, hi] for lo, hi in intervals]
                for defect_id, intervals in sorted(per_defect.items())},
        }

    # ---------- 交付凭证 ----------

    def issue_feature_certificate(self, request):
        """按当前血缘与缺陷审核并签发样本级交付凭证。

        完整调用示例::

            d.issue_feature_certificate({
                "request_id": "cert-1",
                "usage": "train",
                "selections": [
                    {"node_id": "G", "intervals": [[0, 4)]},
                ],
            })

        ``usage`` 必须与每个被选节点继承的训练/盲测用途一致；节点不重复、
        每节点区间集合非空且在输出域内。首次签发按当前缺陷检查全部所选
        位置，任一位置无效则整笔拒绝，不产生任何凭证。成功后固定请求 ID、
        用途、规范选择与不可变节点依赖/归档来源；历史凭证原文永不倒改，
        其当前可用性用 :meth:`check_feature_certificate` 查询。
        """
        self._check_open()
        fields(request, ('request_id', 'usage', 'selections'))
        cert_id = ident(request['request_id'])
        usage = _canonical_usage(request['usage'])
        if usage not in ('train', 'blind'):
            raise ValueError("usage must be 'train' or 'blind'")
        existing = self._lineage.certificates.get(cert_id)
        selections = self._normalize_selections(request['selections'], usage)
        if existing is not None:
            # 同 ID 等价重放返回首次凭证（即使它现在已不可用）；内容冲突拒绝。
            if self._certificate_semantic(existing) != \
                    self._selection_semantic(usage, selections):
                raise ValueError('certificate request id reused with different content')
            return _json_copy(self._certificate_view(existing))
        # 首次签发：当前缺陷检查所有所选位置，一处坏即整笔拒绝。
        for selection in selections:
            overall, _ = self._lineage.health(selection['node_id'])
            if any(lo < hi for lo, hi in
                   (self._intersect([tuple(iv) for iv in selection['intervals']],
                                    overall))):
                raise ValueError('refuse certificate: selected positions contain '
                                 'currently invalid samples')
        node_ids = [selection['node_id'] for selection in selections]
        raw_ids = self._lineage.raw_ancestors(node_ids)
        lineage_refs = sorted(self._lineage.lineage_closure(node_ids))
        certificate = {
            'schema': FEATURE_CERTIFICATE_SCHEMA,
            'request_id': cert_id,
            'usage': usage,
            'selections': [
                {'node_id': selection['node_id'],
                 'intervals': [list(iv) for iv in selection['intervals']]}
                for selection in selections],
            'node_ids': node_ids,
            'lineage_node_ids': lineage_refs,
            'raw_source_node_ids': raw_ids,
        }
        payload = {
            'request_id': cert_id,
            'usage': usage,
            'selections': certificate['selections'],
        }
        self._commit_lineage('certificate', cert_id, payload, certificate)
        return _json_copy(self._certificate_view(certificate))

    def _normalize_selections(self, raw_selections, usage):
        selections_in = array(raw_selections, 1, 10 ** 9)
        seen_nodes = set()
        selections = []
        for raw in selections_in:
            fields(raw, ('node_id', 'intervals'))
            node_id = ident(raw['node_id'])
            if node_id in seen_nodes:
                raise ValueError('duplicate node in certificate selections')
            seen_nodes.add(node_id)
            if node_id not in self._lineage.nodes:
                raise ValueError(f'unknown node: {node_id}')
            node = self._lineage.nodes[node_id]
            if node['usage'] != usage:
                raise ValueError(
                    f"certificate usage {usage} does not match node {node_id} "
                    f"inherited usage {node['usage']}")
            raw_intervals = array(raw['intervals'], 1, 10 ** 9)
            intervals = []
            for raw_iv in raw_intervals:
                lo, hi = _ival(raw_iv)
                if not 0 <= lo < hi <= node['length']:
                    raise ValueError('selection interval outside node output domain')
                intervals.append((lo, hi))
            # 重排、重叠拆分、相邻拆分按同一个采样集合规范化。
            intervals = _merge(intervals)
            selections.append({'node_id': node_id, 'intervals': intervals})
        selections.sort(key=lambda item: item['node_id'])
        return selections

    @staticmethod
    def _intersect(left, right):
        from .lineage import intersect_intervals
        return intersect_intervals(left, right)

    @staticmethod
    def _selection_semantic(usage, selections):
        return {
            'usage': usage,
            'selections': [
                {'node_id': item['node_id'],
                 'intervals': [[lo, hi] for lo, hi in item['intervals']]}
                for item in selections],
        }

    def _certificate_semantic(self, certificate):
        return {
            'usage': certificate['usage'],
            'selections': [
                {'node_id': item['node_id'],
                 'intervals': [list(iv) for iv in item['intervals']]}
                for item in certificate['selections']],
        }

    def _certificate_view(self, certificate):
        view = dict(certificate)
        view['selections'] = [
            {'node_id': item['node_id'],
             'intervals': [list(iv) for iv in item['intervals']]}
            for item in certificate['selections']]
        return view

    def read_feature_node(self, node_id):
        """读取节点的原始登记声明；未知 ID 抛 UnknownNodeError。"""
        self._check_open()
        node_id = ident(node_id)
        node = self._lineage.nodes.get(node_id)
        if node is None:
            raise UnknownNodeError(f'unknown feature node: {node_id}')
        out = dict(node)
        if node['kind'] == 'raw':
            out['interval'] = list(node['interval'])
            out['fragments'] = [dict(frag, interval=list(frag['interval']))
                                for frag in node['fragments']]
        else:
            out['parents'] = [dict(parent) for parent in node['parents']]
        return _json_copy(out)

    def read_feature_certificate(self, request_id):
        """读取首次成功签发时固定的凭证原文；不代表当前仍获准取用。"""
        self._check_open()
        cert_id = ident(request_id)
        certificate = self._lineage.certificates.get(cert_id)
        if certificate is None:
            raise UnknownCertificateError(
                f'no feature certificate for request id {cert_id}')
        return _json_copy(self._certificate_view(certificate))

    def check_feature_certificate(self, request_id):
        """按当前缺陷判断历史凭证是否仍可使用（只读，不改写凭证原文）。

        逐选择给出凭证真正选中位置内的当前无效区间及对应缺陷 ID；没有
        缺陷命中的选择仍列出且其无效区间为空。历史凭证读取成功只说明它
        曾成功签发，是否可当前取用以本返回的 ``currently_usable`` 为准。
        """
        self._check_open()
        cert_id = ident(request_id)
        certificate = self._lineage.certificates.get(cert_id)
        if certificate is None:
            raise UnknownCertificateError(
                f'no feature certificate for request id {cert_id}')
        selections_out = []
        usable = True
        for selection in certificate['selections']:
            node_id = selection['node_id']
            chosen = [tuple(iv) for iv in selection['intervals']]
            _, per_defect = self._lineage.health(node_id)
            defects_out = []
            node_invalid = []
            for defect_id, intervals in sorted(per_defect.items()):
                hits = self._intersect(chosen, intervals)
                if not hits:
                    continue
                defects_out.append({
                    'defect_request_id': defect_id,
                    'request_id': defect_id,
                    'intervals': [[lo, hi] for lo, hi in hits],
                })
                node_invalid.extend(hits)
            node_invalid = _merge(node_invalid)
            if node_invalid:
                usable = False
            selections_out.append({
                'node_id': node_id,
                'selected_intervals': [list(iv) for iv in chosen],
                'currently_invalid_intervals': [[lo, hi] for lo, hi in node_invalid],
                'defects': defects_out,
            })
        return {
            'request_id': cert_id,
            'currently_usable': usable,
            'usage': certificate['usage'],
            'selections': selections_out,
            'note': 'historical certificate text is immutable; this check reflects '
                    'currently registered defects only',
        }

    def _commit_lineage(self, kind, request_id, payload, response):
        """校验全部完成后再追加 journal；失败不留下任何部分状态。"""
        existing = self._responses.get((kind, request_id))
        if existing is not None:
            raise ValueError(f'{kind} id already defined with different content')
        self._append_journal(kind, request_id, payload, response)
        # journal 已 COMMITTED：与 _apply_committed 相同的归一化应用到内存。
        self._apply_committed({'kind': kind, 'id': request_id,
                               'payload': payload, 'response': response})
        self._flush_state()


class ReleaseLedger:
    """面向单个复核目录的发布账本薄封装。

    也可以直接调用 ``ReviewDirectory.preview_release`` /
    ``publish_release`` / ``read_release``；本类仅提供 ``preview``、
    ``publish``、``get`` 这组短方法名。
    """

    def __init__(self, source):
        if isinstance(source, ReviewDirectory):
            self.directory = source
            self._owns_directory = False
        elif isinstance(source, (str, Path)):
            self.directory = open_review(source)
            self._owns_directory = True
        else:
            raise ReleaseRequestError(
                'ReleaseLedger requires an open ReviewDirectory or directory path')

    def close(self):
        if self._owns_directory:
            self.directory.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __getattr__(self, name):
        # 允许账本薄封装直接暴露 ReviewDirectory 上的完整具名 API。
        if name == 'directory' or 'directory' not in self.__dict__:
            raise AttributeError(name)
        return getattr(self.__dict__['directory'], name)

    @classmethod
    def open(cls, source):
        return cls(source)

    def preview(self, *args, **kwargs):
        return self.directory.preview_release(*args, **kwargs)

    def publish(self, *args, **kwargs):
        return self.directory.publish_release(*args, **kwargs)

    def get(self, request_id):
        return self.directory.read_release(request_id)

    def read(self, request_id):
        return self.directory.read_release(request_id)

    def find(self, request_id):
        return self.directory.find_release(request_id)


DatasetReleaseLedger = ReleaseLedger
ReleaseBook = ReleaseLedger


def _as_directory(directory):
    if isinstance(directory, ReviewDirectory):
        return directory, False
    return open_review(directory), True


def preview_release(directory, *args, **kwargs):
    """模块级预览入口；可传已打开句柄或复核目录路径。"""
    handle, should_close = _as_directory(directory)
    try:
        return handle.preview_release(*args, **kwargs)
    finally:
        if should_close:
            handle.close()


def publish_release(directory, *args, **kwargs):
    """模块级发布入口；可传已打开句柄或复核目录路径。"""
    handle, should_close = _as_directory(directory)
    try:
        return handle.publish_release(*args, **kwargs)
    finally:
        if should_close:
            handle.close()


def read_release(directory, request_id):
    handle, should_close = _as_directory(directory)
    try:
        return handle.read_release(request_id)
    finally:
        if should_close:
            handle.close()


def get_published_manifest(directory, request_id):
    handle, should_close = _as_directory(directory)
    try:
        return handle.find_release(request_id)
    finally:
        if should_close:
            handle.close()


preview_dataset_release = preview_release
publish_dataset_release = publish_release
read_published_manifest = read_release
get_release = get_published_manifest
