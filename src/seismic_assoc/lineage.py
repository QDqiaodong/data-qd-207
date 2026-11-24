"""样本级特征血缘、缺陷与交付凭证。

本模块只登记数据依赖与引用（节点声明、父窗口、缺陷区间、凭证选择），
不读取、不解码任何波形或特征数值，也不实现特征算法。

核心设计：

- 节点分两类：``raw``（实际重新取数建立的原始输入节点）与 ``feature``
  （滑窗特征声明）；两类共用同一个不可重定义的 ``node_id`` 空间，
  新节点只能引用已存在节点，因此天然无环。
- 缺陷与健康状态全部用**半开整数区间的有序并集**表达，绝不逐采样点展开；
  节点长度允许到 10^9，十亿输出点的健康查询只产生常数级区间条目。
- 缺陷一经接受不可撤销；节点声明不可变。健康查询是缺陷集合的纯派生结果，
  新增缺陷后清空惰性缓存即可，血缘本身不动。

区间坐标约定：原始节点输出位置 ``i`` 对应该站本地刻度 ``a+i``（建节点时
固定物理区间 ``[a,b)``）；特征节点输出域始终为 ``[0,length)``。
"""
from collections import defaultdict

MAX_RAW_NODES = 32
MAX_FEATURE_NODES = 32
MAX_DEFECTS = 64
MAX_CERTIFICATES = 64
MAX_NODE_LENGTH = 10 ** 9
MAX_FEATURE_DEPTH = 4


class LineageError(ValueError):
    """特征血缘/缺陷/凭证请求不合法或与当前目录状态不符。"""


class UnknownNodeError(LineageError, KeyError):
    """按节点 ID 读取不到已登记节点。"""


class UnknownCertificateError(LineageError, KeyError):
    """按凭证请求 ID 读取不到已签发凭证。"""


def merge_intervals(intervals):
    """合并相交或端点相接的半开整数区间；输入为 (lo, hi) 可迭代对象。"""
    out = []
    for lo, hi in sorted(intervals):
        if out and lo <= out[-1][1]:
            if hi > out[-1][1]:
                out[-1] = (out[-1][0], hi)
        else:
            out.append((lo, hi))
    return out


def intersect_intervals(left, right):
    """两组已排序区间的非空交集（半开区间端点相接不共享样本）。"""
    i = j = 0
    out = []
    while i < len(left) and j < len(right):
        lo = max(left[i][0], right[j][0])
        hi = min(left[i][1], right[j][1])
        if lo < hi:
            out.append((lo, hi))
        if left[i][1] < right[j][1]:
            i += 1
        else:
            j += 1
    return merge_intervals(out)


def subtract_intervals(base, holes):
    """从已合并区间组 ``base`` 中扣除 ``holes`` 覆盖的部分，不枚举采样点。"""
    out = []
    for lo, hi in base:
        pieces = [(lo, hi)]
        for hlo, hhi in holes:
            next_pieces = []
            for plo, phi in pieces:
                if hhi <= plo or hlo >= phi:
                    next_pieces.append((plo, phi))
                else:
                    if hlo > plo:
                        next_pieces.append((plo, min(hlo, phi)))
                    if hhi < phi:
                        next_pieces.append((max(hhi, plo), phi))
            pieces = next_pieces
        out.extend(pieces)
    return merge_intervals(out)


class LineageStore:
    """血缘、缺陷与凭证的内存事实状态；唯一持久事实来源是 review journal。

    本类不做 ID 形态/业务前置校验（由 ``ReviewDirectory`` 在 journal 追加前
    完成），只维护容量、身份唯一性与健康派生索引；所有方法接收的均为已归一
    的纯数据，返回内容也都是新建对象，不暴露可被调用方改写的内部状态。
    """

    def __init__(self):
        self.nodes = {}
        self.raw_count = 0
        self.feature_count = 0
        self.defects = []                      # 按接受顺序保存
        self.defect_ids = set()
        self.certificates = {}
        # 缺陷派生索引：receipt_id / station -> 有序合并区间
        self._receipt_defects = {}
        self._physical_defects = {}
        self._health_cache = {}

    # ---------- 写入（journal 已提交后才允许调用） ----------

    def add_raw_node(self, node):
        node_id = node['node_id']
        if node_id in self.nodes:
            raise LineageError(f'node id already defined: {node_id}')
        if self.raw_count >= MAX_RAW_NODES:
            raise LineageError(f'at most {MAX_RAW_NODES} raw nodes per directory')
        self.nodes[node_id] = node
        self.raw_count += 1

    def add_feature_node(self, node):
        node_id = node['node_id']
        if node_id in self.nodes:
            raise LineageError(f'node id already defined: {node_id}')
        if self.feature_count >= MAX_FEATURE_NODES:
            raise LineageError(f'at most {MAX_FEATURE_NODES} feature nodes per directory')
        self.nodes[node_id] = node
        self.feature_count += 1

    def add_defect(self, defect):
        request_id = defect['request_id']
        if request_id in self.defect_ids:
            raise LineageError(f'defect request id already defined: {request_id}')
        if len(self.defects) >= MAX_DEFECTS:
            raise LineageError(f'at most {MAX_DEFECTS} defects per directory')
        self.defects.append(defect)
        self.defect_ids.add(request_id)
        interval = tuple(defect['interval'])
        if defect['scope'] == 'receipt':
            bucket = self._receipt_defects.setdefault(defect['target'], [])
            bucket.extend([interval])
            self._receipt_defects[defect['target']] = merge_intervals(bucket)
        else:
            bucket = self._physical_defects.setdefault(defect['target'], [])
            bucket.extend([interval])
            self._physical_defects[defect['target']] = merge_intervals(bucket)
        # 健康结论是缺陷集合的派生值：新缺陷使全部缓存失效。
        self._health_cache.clear()

    def add_certificate(self, certificate):
        request_id = certificate['request_id']
        if request_id in self.certificates:
            raise LineageError(f'certificate request id already defined: {request_id}')
        if len(self.certificates) >= MAX_CERTIFICATES:
            raise LineageError(f'at most {MAX_CERTIFICATES} certificates per directory')
        self.certificates[request_id] = certificate

    # ---------- 缺陷索引读取 ----------

    def receipt_defect_intervals(self, receipt_id):
        return list(self._receipt_defects.get(receipt_id, ()))

    def physical_defect_intervals(self, station):
        return list(self._physical_defects.get(station, ()))

    def has_physical_defect(self, station, intervals):
        hits = intersect_intervals(intervals, self.physical_defect_intervals(station))
        return bool(hits)

    def healthy_receipt_pieces(self, receipt_id, receipt_interval):
        """一份回执在给定物理区间内扣除自身缺陷片段后的完好部分。"""
        base = [tuple(receipt_interval)]
        holes = self.receipt_defect_intervals(receipt_id)
        return subtract_intervals(base, holes)

    # ---------- 健康计算（区间代数，绝不逐点展开） ----------

    def health(self, node_id):
        """返回 (整体无效区间列表, {缺陷请求 ID: 区间列表})，坐标为节点输出域。"""
        if node_id not in self.nodes:
            raise UnknownNodeError(f'unknown feature node: {node_id}')
        cached = self._health_cache.get(node_id)
        if cached is not None:
            overall, per_defect = cached
            return list(overall), {k: list(v) for k, v in per_defect.items()}
        node = self.nodes[node_id]
        if node['kind'] == 'raw':
            overall, per_defect = self._raw_health(node)
        else:
            overall, per_defect = self._feature_health(node)
        self._health_cache[node_id] = (overall, per_defect)
        return list(overall), {k: list(v) for k, v in per_defect.items()}

    def _raw_health(self, node):
        """原始节点：把物理刻度缺陷逐片段映射到节点输出坐标 [0,length)。"""
        a = node['interval'][0]
        domain = [(0, node['length'])]
        per_defect = {}
        for defect in self.defects:
            hits = []
            if defect['scope'] == 'receipt':
                # 只有实际固定使用该回执的片段才受副本缺陷牵连。
                if not any(frag['receipt_id'] == defect['target']
                           for frag in node['fragments']):
                    continue
                for frag in node['fragments']:
                    if frag['receipt_id'] != defect['target']:
                        continue
                    for lo, hi in intersect_intervals(
                            [tuple(frag['interval'])], [tuple(defect['interval'])]):
                        hits.append((lo - a, hi - a))
            else:
                if defect['target'] != node['station']:
                    continue
                for lo, hi in intersect_intervals(
                        [tuple(node['interval'])], [tuple(defect['interval'])]):
                    hits.append((lo - a, hi - a))
            hits = intersect_intervals(domain, merge_intervals(hits))
            if hits:
                per_defect[defect['request_id']] = hits
        overall = merge_intervals(
            iv for ivs in per_defect.values() for iv in ivs)
        return overall, per_defect

    def _feature_health(self, node):
        """特征节点：父输出位置 p 落在窗口 [i+offset, i+offset+width) 内时，
        输出位置 i 无效。反解为输出区间：

            i+offset < hi 且 i+offset+width > lo
            => lo-offset-width+1 <= i <= hi-offset-1
            => i in [lo-offset-width+1, hi-offset)

        多父节点取并集；每个缺陷保留自己的精确影响，不做整节点传播。
        """
        length = node['length']
        domain = [(0, length)]
        collected = defaultdict(list)
        for parent in node['parents']:
            _, parent_per_defect = self.health(parent['node_id'])
            offset, width = parent['offset'], parent['width']
            for defect_id, intervals in parent_per_defect.items():
                for lo, hi in intervals:
                    out_lo = lo - offset - width + 1
                    out_hi = hi - offset
                    clipped = intersect_intervals(
                        domain, merge_intervals([(out_lo, out_hi)]))
                    collected[defect_id].extend(clipped)
        per_defect = {defect_id: merge_intervals(intervals)
                      for defect_id, intervals in collected.items()
                      if intervals}
        overall = merge_intervals(
            iv for ivs in per_defect.values() for iv in ivs)
        return overall, per_defect

    def lineage_closure(self, node_ids):
        """给定节点集合，返回含其全部祖先（含自身）的排序节点 ID 列表。"""
        seen = set()
        stack = list(node_ids)
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            node = self.nodes[current]
            if node['kind'] == 'feature':
                stack.extend(parent['node_id'] for parent in node['parents'])
        return sorted(seen)

    def raw_ancestors(self, node_ids):
        closure = self.lineage_closure(node_ids)
        return sorted(nid for nid in closure
                      if self.nodes[nid]['kind'] == 'raw')
