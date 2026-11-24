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
from fractions import Fraction
from pathlib import Path

from .checks import array, fields, fraction_str, ident, integer, number

_MAX_BLOCKS = 2000
_MAX_SEGMENTS = 128
_MAX_STATIONS = 100
_MAX_ARRIVALS = 2000


def _ival(value):
    """校验 [lo,hi) 半开整数区间；整数边界不接收布尔值。"""
    array(value, 2, 2)
    lo, hi = integer(value[0], -(10 ** 18), 10 ** 18), integer(value[1], -(10 ** 18), 10 ** 18)
    if lo >= hi:
        raise ValueError('interval must be non-empty and half-open [lo,hi)')
    return lo, hi


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
        directory._catalog = json.loads((target / 'catalog.json').read_text('utf-8'))
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
        raw = self._journal.read().decode('utf-8')
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
        archived = _merge([rec['interval'] for rec in self._receipts.values()
                           if rec['block_id'] == bid])
        for lo, hi in needed:
            if not any(alo <= lo and hi <= ahi for alo, ahi in archived):
                raise ValueError('block still has required samples without archive; refused')
        payload = {'block_id': bid}
        response = {'request_id': qid, 'approved': True, 'block_id': bid}
        self._append_journal('offline', qid, payload, response)
        self._offline[bid] = {'block_id': bid, 'request_id': qid}
        self._responses[('offline', qid)] = response
        self._flush_state()
        return json.loads(json.dumps(response))

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
            arch = [(max(rec['interval'][0], seg_lo), min(rec['interval'][1], seg_hi),
                     rec['receipt_id'])
                    for rec in receipts
                    if rec['block_id'] == bid
                    and rec['interval'][0] < seg_hi and rec['interval'][1] > seg_lo]
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
