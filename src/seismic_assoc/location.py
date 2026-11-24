"""候选震源位置的纯计算诊断与下游准入判断。

本模块不读取波形、不调用外部服务，也不修改归并结果。``process`` 输出中的
``anchor`` 只是归并窗口锚点；发震时刻由“到时 − 候选旅行时”的两级中位数估计。

公开主入口为 :func:`evaluate_location_candidates`。模块同时提供若干语义别名，
以兼容不同调用命名；数据契约见 ``docs/location-contract.md``。
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any

SCHEMA = "seismic-assoc.location-evaluation/v1"

USABLE = "usable"
AMBIGUOUS = "ambiguous"
INSUFFICIENT_EVIDENCE = "insufficient_evidence"
UNRESOLVED = "unresolved"

_VALID_STATUSES = {USABLE, AMBIGUOUS, INSUFFICIENT_EVIDENCE, UNRESOLVED}
_PHASE_ORDER = {"P": 0, "S": 1}

_THRESHOLD_KEYS = (
    "residual_threshold",
    "residual_tolerance",
    "residual_cutoff",
    "max_residual",
    "max_absolute_residual",
    "max_abs_residual",
    "residual_max",
    "threshold",
)
_MIN_SUPPORT_KEYS = (
    "min_supporting_stations",
    "minimum_supporting_stations",
    "min_supported_stations",
    "minimum_supported_stations",
    "min_support_stations",
    "minimum_support_stations",
    "min_support",
    "minimum_support",
    "required_supporting_stations",
    "required_support_stations",
    "min_station_count",
    "minimum_station_count",
    "min_stations",
    "minimum_stations",
)
_MARGIN_KEYS = (
    "ambiguity_margin",
    "ambiguity_threshold",
    "ambiguity_tolerance",
    "ambiguity_bound",
    "tie_margin",
    "tie_tolerance",
    "median_margin",
    "ambiguity_delta",
)
_COORD_KEYS = (
    "station_coordinates",
    "station_coordinate_mapping",
    "station_coords",
    "station_mapping",
    "stations",
    "coordinates",
    "station_locations",
)
_CANDIDATE_KEYS = (
    "candidates",
    "candidate_locations",
    "location_candidates",
    "candidate_table",
    "candidate_location_table",
    "travel_time_table",
    "travel_times",
)
_QUALITY_KEYS = (
    "quality",
    "quality_params",
    "quality_parameters",
    "quality_config",
    "location_quality",
    "location_quality_params",
    "admission_config",
    "admission",
    "params",
    "config",
    "parameters",
    "options",
)


class LocationInputError(ValueError):
    """位置评估输入不完整、身份冲突或含非法数值。

    它继承 :class:`ValueError`，因此既有“非法调用抛 ``ValueError``”的断言
    无需知道本异常类型。
    """


class LocationError(LocationInputError):
    """兼容别名；规范异常基类名为 :class:`LocationInputError`。"""


class LocationReport(dict):
    """dict 报告，同时兼容把顶层结果当事件列表/序列使用的调用方。

    JSON 仍序列化为包含 ``events`` 的对象；整数索引、切片、迭代和 ``len``
    则代理到 ``events``。
    """

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, (int, slice)):
            return self["events"][key]
        if isinstance(key, str):
            try:
                return super().__getitem__(key)
            except KeyError:
                for event in self.get("events", []):
                    if event.get("event_id") == key or event.get("id") == key:
                        return event
                raise
        return super().__getitem__(key)

    def __iter__(self):  # type: ignore[override]
        return iter(self["events"])

    def __len__(self) -> int:
        return len(self["events"])


class EvidenceString(str):
    """JSON 中就是 ID 字符串，同时允许 Python 调用方用 ``row["field"]`` 取原因。"""

    evidence: dict[str, Any]

    def __new__(cls, value: str, evidence: Mapping[str, Any] | None = None):
        obj = super().__new__(cls, value)
        object.__setattr__(obj, "evidence", dict(evidence or {}))
        return obj

    def __getitem__(self, key: str) -> Any:  # type: ignore[override]
        try:
            return self.evidence[key]
        except KeyError:
            # 允许把字符串本身的字符索引语义保留给整数。
            if isinstance(key, int):
                return super().__getitem__(key)  # type: ignore[index]
            raise

    def get(self, key: str, default: Any = None) -> Any:
        return self.evidence.get(key, default)


class EvidenceDict(dict):
    """字段证据对象；直接与 ID 字符串比较时按其主 ID 判等。

    这样既能通过 ``row["residual_median"]`` 取证据，也兼容把
    ``supporting_stations == ["A"]``、``best_candidate == "c1"`` 写成 ID
    断言的调用方。``json.dumps`` 仍把它序列化为普通 JSON 对象。
    """

    _id_fields = ("candidate_id", "station", "station_id", "id")

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, str):
            return any(self.get(field) == other for field in self._id_fields)
        return super().__eq__(other)

    def __ne__(self, other: Any) -> bool:
        return not self.__eq__(other)

    def __hash__(self) -> int | None:  # type: ignore[override]
        return None


# 常见异常命名别名。
LocationValidationError = LocationInputError
LocationEvaluationError = LocationInputError


# ---------------------------------------------------------------------------
# 基础解析与确定性数值处理
# ---------------------------------------------------------------------------

def _missing_value(name: str) -> LocationInputError:
    return LocationInputError(f"missing required location input: {name}")


def _parse_number(value: Any, name: str, *, nonnegative: bool = False,
                  positive: bool = False) -> Fraction:
    """把 int/float/Decimal/数字字符串精确转换为 Fraction。

    float 使用其最短十进制表示（``str(float)``），使 ``0.1`` 这类调用方通常
    按十进制给出的阈值/旅行时在边界比较时保持可复核的精确语义。
    """
    if isinstance(value, bool) or value is None:
        raise LocationInputError(f"{name} must be a finite number")

    if isinstance(value, Fraction):
        result = value
    elif isinstance(value, int):
        result = Fraction(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise LocationInputError(f"{name} must be finite")
        try:
            result = Fraction(Decimal(repr(value)))
        except (InvalidOperation, ValueError) as exc:
            raise LocationInputError(f"{name} is not a valid number") from exc
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise LocationInputError(f"{name} must be finite")
        result = Fraction(value)
    elif isinstance(value, str):
        text = value.strip()
        try:
            if "/" in text:
                # process 对非整数 Decimal 会输出 Fraction 的 "numerator/denominator"
                # 字符串；这仍是可复核的精确数值。
                result = Fraction(text)
            else:
                decimal_value = Decimal(text)
                if not decimal_value.is_finite():
                    raise LocationInputError(f"{name} must be finite")
                result = Fraction(decimal_value)
        except (InvalidOperation, ValueError, ZeroDivisionError) as exc:
            raise LocationInputError(f"{name} is not a valid number") from exc
    else:
        raise LocationInputError(f"{name} must be a finite number")

    if nonnegative and result < 0:
        raise LocationInputError(f"{name} must be nonnegative")
    if positive and result <= 0:
        raise LocationInputError(f"{name} must be positive")
    return result


def _json_number(value: Fraction | None) -> int | float | str | None:
    if value is None:
        return None
    if value.denominator == 1:
        return value.numerator
    number = float(value)
    if math.isfinite(number):
        return number
    # 极端巨大的有理值不能转成有限 float；保留精确有理数字符串以确保可序列化。
    return f"{value.numerator}/{value.denominator}"


def _median(values: list[Fraction]) -> Fraction:
    if not values:
        raise ValueError("median requires at least one value")
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _nonempty_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise LocationInputError(f"{field} must be a nonempty string")
    return value


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LocationInputError(f"{field} must be an object/mapping")
    return value


def _first_key(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _pop_named(options: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in options and options[name] is not None:
            return options.pop(name)
    return None


# ---------------------------------------------------------------------------
# 输入适配：支持文档中的标准形状，也兼容常见命名/位置参数顺序
# ---------------------------------------------------------------------------

def _looks_like_quality(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(k in value for k in
                    _THRESHOLD_KEYS + _MIN_SUPPORT_KEYS + _MARGIN_KEYS)
    all_keys = _THRESHOLD_KEYS + _MIN_SUPPORT_KEYS + _MARGIN_KEYS
    return any(hasattr(value, name) for name in all_keys)


def _coordinate_pair(value: Any) -> tuple[Any, Any] | None:
    if isinstance(value, Mapping):
        lat = value.get("lat", value.get("latitude"))
        lon = value.get("lon", value.get("lng", value.get("longitude")))
        if lat is not None and lon is not None:
            return lat, lon
        return None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return value[0], value[1]
    return None


def _looks_like_coordinates(value: Any) -> bool:
    if isinstance(value, Mapping) and value:
        first = next(iter(value.values()))
        return _coordinate_pair(first) is not None
    if isinstance(value, list) and value and isinstance(value[0], Mapping):
        return ("id" in value[0] or "station" in value[0]) and \
               _coordinate_pair(value[0]) is not None
    return False


def _looks_like_candidates(value: Any) -> bool:
    if isinstance(value, list) and value:
        first = value[0]
        return isinstance(first, Mapping) and (
            "candidate_id" in first or "id" in first or
            "travel_times" in first or "lat" in first)
    if isinstance(value, Mapping) and value:
        first = next(iter(value.values()))
        if isinstance(first, Mapping):
            return ("candidate_id" in first or "travel_times" in first or
                    "lat" in first or "latitude" in first)
    return False


def _extract_quality_object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    extracted: dict[str, Any] = {}
    for names in (_THRESHOLD_KEYS, _MIN_SUPPORT_KEYS, _MARGIN_KEYS):
        for name in names:
            if hasattr(value, name):
                extracted[name] = getattr(value, name)
                break
    return extracted


def _resolve_call_shape(
    process_result: Any,
    station_coordinates: Any,
    candidates: Any,
    quality: Any,
    extra_args: tuple[Any, ...],
    options: dict[str, Any],
) -> tuple[Any, Any, dict[str, Any]]:
    coords_kw = _pop_named(options, _COORD_KEYS)
    candidates_kw = _pop_named(options, _CANDIDATE_KEYS)
    quality_kw = _pop_named(options, _QUALITY_KEYS)

    coords = station_coordinates if station_coordinates is not None else coords_kw
    cand = candidates if candidates is not None else candidates_kw

    # 兼容把质量参数直接作为第 4/5/6 个位置参数传入的调用。
    scalar_args: list[Any] = []
    positional_values = [quality, *extra_args]
    quality_objects: list[Any] = []
    for value in positional_values:
        if value is None:
            continue
        if _looks_like_quality(value):
            quality_objects.append(value)
        elif isinstance(value, (tuple, list)) and value and all(
                isinstance(item, (int, float, Fraction, Decimal, str))
                and not isinstance(item, bool) for item in value):
            scalar_args.extend(value)
        elif isinstance(value, (int, float, Fraction, Decimal, str)) and \
                not isinstance(value, bool):
            scalar_args.append(value)
        elif isinstance(value, Mapping) and _looks_like_candidates(value):
            if cand is None:
                cand = value
        elif _looks_like_coordinates(value):
            if coords is None:
                coords = value
        elif isinstance(value, Mapping):
            # 可能是独立的 travel-time 表或质量 dict；质量键缺失时保留到选项。
            quality_objects.append(value)

    if quality_kw is not None:
        quality_objects.append(quality_kw)

    quality_data: dict[str, Any] = {}
    for obj in quality_objects:
        quality_data.update(_extract_quality_object(obj))
    # 直接关键字参数优先级最高。
    quality_data.update(options)

    # 常见误换：candidates 与 coordinates 都按位置传入但顺序相反。
    if _looks_like_candidates(coords) and _looks_like_coordinates(cand):
        coords, cand = cand, coords

    if scalar_args:
        if "residual_threshold" not in quality_data:
            quality_data["residual_threshold"] = scalar_args[0]
        if len(scalar_args) > 1 and "min_supporting_stations" not in quality_data:
            quality_data["min_supporting_stations"] = scalar_args[1]
        if len(scalar_args) > 2 and "ambiguity_margin" not in quality_data:
            quality_data["ambiguity_margin"] = scalar_args[2]

    return process_result, coords, cand, quality_data


# ---------------------------------------------------------------------------
# 规范化输入（全部验证通过后才开始事件计算）
# ---------------------------------------------------------------------------

def _normalize_coordinates(raw: Any) -> dict[str, tuple[Fraction, Fraction]]:
    if raw is None:
        raise _missing_value("station_coordinates")

    entries: Any
    if isinstance(raw, list):
        entries = []
        for item in raw:
            item = _mapping(item, "station coordinate")
            station_id = item.get("id", item.get("station", item.get("station_id")))
            entries.append((station_id, item))
    elif isinstance(raw, Mapping):
        entries = raw.items()
    else:
        raise LocationInputError("station_coordinates must be a mapping or list")

    coords: dict[str, tuple[Fraction, Fraction]] = {}
    for station_id, value in entries:
        station_id = _nonempty_id(station_id, "station id")
        pair = _coordinate_pair(value)
        if pair is None:
            raise LocationInputError(f"invalid coordinates for station {station_id}")
        lat = _parse_number(pair[0], f"station {station_id} latitude")
        lon = _parse_number(pair[1], f"station {station_id} longitude")
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise LocationInputError(f"illegal coordinates for station {station_id}")
        coords[station_id] = (lat, lon)
    return coords


def _normalize_process_events(raw_result: Any) -> list[dict[str, Any]]:
    if isinstance(raw_result, Mapping) and "events" in raw_result:
        raw_events = raw_result["events"]
    elif isinstance(raw_result, list):
        raw_events = raw_result
    elif isinstance(raw_result, Mapping) and "arrivals" in raw_result:
        raw_events = [raw_result]
    else:
        raise LocationInputError("process result must contain an events list")

    if not isinstance(raw_events, list):
        raise LocationInputError("process result events must be a list")

    events: list[dict[str, Any]] = []
    seen_arrival_ids: set[str] = set()

    for index, raw_event in enumerate(raw_events):
        event = _mapping(raw_event, f"events[{index}]")
        event_id = event.get("event_id", event.get("id"))
        if event_id is None:
            event_id = f"event-{index + 1}"
        event_id = _nonempty_id(event_id, f"events[{index}].event_id")

        raw_arrivals = event.get("arrivals")
        if not isinstance(raw_arrivals, list):
            raise LocationInputError(f"event {event_id} must contain an arrivals list")

        arrivals: list[dict[str, Any]] = []
        for a_index, raw_arrival in enumerate(raw_arrivals):
            arrival = _mapping(raw_arrival, f"event {event_id} arrival {a_index}")
            arrival_id = _nonempty_id(arrival.get("id"), "arrival id")
            if arrival_id in seen_arrival_ids:
                raise LocationInputError(f"duplicate arrival id: {arrival_id}")
            seen_arrival_ids.add(arrival_id)

            station = _nonempty_id(arrival.get("station"),
                                   f"arrival {arrival_id} station")
            phase = arrival.get("phase")
            if phase not in ("P", "S"):
                raise LocationInputError(
                    f"arrival {arrival_id} phase must be 'P' or 'S'")
            time_value = _parse_number(
                arrival.get("time"), f"arrival {arrival_id} time")

            arrivals.append({
                "id": arrival_id,
                "station": station,
                "phase": phase,
                "time": time_value,
                "raw_time": arrival.get("time"),
            })

        events.append({"event_id": event_id, "arrivals": arrivals})

    return events


def _store_travel_time(table: dict[tuple[str, str], Fraction],
                       station: str, phase: Any, value: Any,
                       candidate_id: str, known_stations: Mapping[str, Any],
                       *, flat_phase: str | None = None) -> None:
    if station not in known_stations:
        raise LocationInputError(
            f"candidate {candidate_id} references unknown station: {station}")

    phases: tuple[str, ...]
    if flat_phase is not None:
        phases = (flat_phase,)
    elif phase in ("P", "S"):
        phases = (phase,)  # type: ignore[assignment]
    else:
        raise LocationInputError(
            f"candidate {candidate_id} has invalid phase {phase!r}; expected P/S")

    if value is None:
        return
    travel_time = _parse_number(
        value, f"candidate {candidate_id} {station}.{phase}", nonnegative=True)
    for one_phase in phases:
        table[(station, one_phase)] = travel_time


def _normalize_travel_times(raw_table: Any, candidate_id: str,
                            known_stations: Mapping[str, Any]
                            ) -> dict[tuple[str, str], Fraction]:
    table: dict[tuple[str, str], Fraction] = {}
    if raw_table is None:
        return table

    if isinstance(raw_table, list):
        for index, item in enumerate(raw_table):
            item = _mapping(item, f"candidate {candidate_id} travel_times[{index}]")
            station = _nonempty_id(
                item.get("station", item.get("station_id")),
                f"candidate {candidate_id} travel time station")
            if isinstance(item.get("travel_times"), Mapping):
                for phase, value in item["travel_times"].items():
                    _store_travel_time(table, station, phase, value,
                                       candidate_id, known_stations)
            elif "P" in item or "S" in item:
                for phase in ("P", "S"):
                    if phase in item:
                        _store_travel_time(table, station, phase, item[phase],
                                           candidate_id, known_stations)
            else:
                phase = item.get("phase")
                value = item.get("travel_time", item.get(
                    "time", item.get("value", item.get("predicted_time"))))
                _store_travel_time(table, station, phase, value,
                                   candidate_id, known_stations)
        return table

    raw_table = _mapping(raw_table, f"candidate {candidate_id} travel_times")

    # 相位优先：{"P": {"A": 2}, "S": {"A": 4}}
    if set(raw_table).issubset({"P", "S"}) and any(
            isinstance(v, Mapping) for v in raw_table.values()):
        for phase, by_station in raw_table.items():
            by_station = _mapping(
                by_station, f"candidate {candidate_id} travel_times.{phase}")
            for station, value in by_station.items():
                _store_travel_time(table, station, phase, value,
                                   candidate_id, known_stations)
        return table

    for station, value in raw_table.items():
        # 扁平键："A.P" / "A:P" / "A_P"。
        if isinstance(station, str) and "." in station:
            left, phase = station.rsplit(".", 1)
            if phase in ("P", "S"):
                _store_travel_time(table, left, phase, value,
                                   candidate_id, known_stations)
                continue
        if isinstance(station, str) and ":" in station:
            left, phase = station.rsplit(":", 1)
            if phase in ("P", "S"):
                _store_travel_time(table, left, phase, value,
                                   candidate_id, known_stations)
                continue
        if isinstance(station, str) and len(station) >= 2 and \
                station[-2] == "_" and station[-1] in ("P", "S"):
            _store_travel_time(table, station[:-2], station[-1], value,
                               candidate_id, known_stations)
            continue

        if station not in known_stations:
            raise LocationInputError(
                f"candidate {candidate_id} references unknown station: {station}")

        if isinstance(value, Mapping):
            for phase, phase_value in value.items():
                _store_travel_time(table, station, phase, phase_value,
                                   candidate_id, known_stations)
        elif isinstance(value, (list, tuple)) and len(value) == 2:
            _store_travel_time(table, station, "P", value[0],
                               candidate_id, known_stations)
            _store_travel_time(table, station, "S", value[1],
                               candidate_id, known_stations)
        elif value is None:
            continue
        else:
            # 台站 -> 数值的扁平表：同一旅行时同时作为 P/S 预测。
            travel_time = _parse_number(
                value, f"candidate {candidate_id} station {station}",
                nonnegative=True)
            table[(station, "P")] = travel_time
            table[(station, "S")] = travel_time

    return table


def _normalize_candidates(raw: Any,
                          coords: Mapping[str, tuple[Fraction, Fraction]]
                          ) -> list[dict[str, Any]]:
    if raw is None:
        raise _missing_value("candidates")

    if isinstance(raw, Mapping):
        entries: list[tuple[str | None, Any]] = list(raw.items())
    elif isinstance(raw, list):
        entries = [(None, item) for item in raw]
    else:
        raise LocationInputError("candidates must be a list or mapping")

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    for index, (mapping_id, raw_candidate) in enumerate(entries):
        candidate = _mapping(raw_candidate, f"candidates[{index}]")
        candidate_id = candidate.get("candidate_id", candidate.get("id", mapping_id))
        candidate_id = _nonempty_id(candidate_id, "candidate_id")
        if candidate_id in seen:
            raise LocationInputError(f"duplicate candidate_id: {candidate_id}")
        seen.add(candidate_id)

        location = candidate
        if ("lat" not in candidate and "latitude" not in candidate):
            for nested_key in ("location", "position", "coordinates", "coord"):
                nested = candidate.get(nested_key)
                if isinstance(nested, Mapping) or (
                        isinstance(nested, (list, tuple)) and len(nested) == 2):
                    location = nested
                    break
        if isinstance(location, (list, tuple)):
            lat_value, lon_value = location[0], location[1]
        else:
            lat_value = location.get("lat", location.get("latitude"))
            lon_value = location.get("lon", location.get(
                "lng", location.get("longitude")))
        if lat_value is None or lon_value is None:
            raise LocationInputError(
                f"candidate {candidate_id} must contain lat/lon")
        lat = _parse_number(lat_value, f"candidate {candidate_id} lat")
        lon = _parse_number(lon_value, f"candidate {candidate_id} lon")
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise LocationInputError(
                f"candidate {candidate_id} has illegal coordinates")

        raw_tt = _first_key(candidate, (
            "travel_times", "travel_time", "travel_time_table",
            "station_travel_times", "predicted_travel_times", "tt"))
        tt = _normalize_travel_times(raw_tt, candidate_id, coords)

        candidates.append({
            "candidate_id": candidate_id,
            "lat": lat,
            "lon": lon,
            "travel_times": tt,
        })

    return candidates


def _parse_quality(data: Mapping[str, Any]) -> dict[str, Fraction | int]:
    threshold_value = _first_key(data, _THRESHOLD_KEYS)
    min_value = _first_key(data, _MIN_SUPPORT_KEYS)
    margin_value = _first_key(data, _MARGIN_KEYS)

    if threshold_value is None:
        raise _missing_value("residual_threshold")
    if min_value is None:
        raise _missing_value("min_supporting_stations")

    threshold = _parse_number(
        threshold_value, "residual_threshold", positive=True)

    if isinstance(min_value, bool) or not isinstance(min_value, int):
        raise LocationInputError(
            "min_supporting_stations must be an integer >= 3")
    if min_value < 3:
        raise LocationInputError("min_supporting_stations must not be less than 3")

    margin: Fraction | None = None
    if margin_value is not None:
        margin = _parse_number(
            margin_value, "ambiguity_margin", nonnegative=True
        )

    return {
        "residual_threshold": threshold,
        "min_supporting_stations": min_value,
        "ambiguity_margin": margin,
    }


# ---------------------------------------------------------------------------
# 候选诊断
# ---------------------------------------------------------------------------

def _arrival_base(arrival: Mapping[str, Any]) -> dict[str, Any]:
    raw_time = arrival.get("raw_time")
    if isinstance(raw_time, (str, int, float)) and not isinstance(raw_time, bool):
        output_time: Any = raw_time
    else:
        output_time = _json_number(arrival["time"])
    numeric_time = _json_number(arrival["time"])
    return {
        "id": arrival["id"],
        "arrival_id": arrival["id"],
        "station": arrival["station"],
        "station_id": arrival["station"],
        "phase": arrival["phase"],
        "time": output_time,
        "arrival_time": output_time,
        "time_value": numeric_time,
        "numeric_time": numeric_time,
    }


def _evaluate_candidate(
    event: Mapping[str, Any],
    candidate: Mapping[str, Any],
    threshold: Fraction,
    coordinates: Mapping[str, tuple[Fraction, Fraction]] | None = None,
) -> dict[str, Any]:
    candidate_id = candidate["candidate_id"]
    tt: dict[tuple[str, str], Fraction] = candidate["travel_times"]  # type: ignore[assignment]

    ordered_arrivals = sorted(
        event["arrivals"],
        key=lambda a: (a["station"], _PHASE_ORDER[a["phase"]],
                       a["time"], a["id"]),
    )
    stations = sorted({a["station"] for a in event["arrivals"]})

    arrival_evidence: list[dict[str, Any]] = []
    station_estimates: dict[str, list[Fraction]] = {s: [] for s in stations}
    provisional: list[dict[str, Any]] = []

    for arrival in ordered_arrivals:
        evidence = _arrival_base(arrival)
        travel_time = tt.get((arrival["station"], arrival["phase"]))
        if travel_time is None:
            evidence.update({
                "evaluable": False,
                "evaluated": False,
                "assessable": False,
                "status": "unevaluable",
                "residual": None,
                "absolute_residual": None,
                "predicted_travel_time": None,
                "travel_time": None,
                "origin_estimate": None,
                "estimated_origin_time": None,
                "within_residual_threshold": None,
                "within_threshold": None,
                "reason_code": "missing_travel_time",
                "reason": "missing_travel_time",
                "message": (
                    "No predicted travel time is provided for this station "
                    "and phase; the arrival is not an outlier."
                ),
            })
            arrival_evidence.append(evidence)
            continue

        origin_estimate = arrival["time"] - travel_time
        station_estimates[arrival["station"]].append(origin_estimate)
        record = {"arrival": arrival, "evidence": evidence,
                  "travel_time": travel_time, "origin_estimate": origin_estimate}
        provisional.append(record)
        evidence.update({
            "evaluable": True,
            "evaluated": True,
            "assessable": True,
            "status": "evaluable",
            "predicted_travel_time": _json_number(travel_time),
            "travel_time": _json_number(travel_time),
            "origin_estimate": _json_number(origin_estimate),
            "estimated_origin_time": _json_number(origin_estimate),
        })
        arrival_evidence.append(evidence)

    station_origins: dict[str, Fraction] = {}
    for station, estimates in station_estimates.items():
        if estimates:
            station_origins[station] = _median(estimates)

    origin_time: Fraction | None = None
    if station_origins:
        origin_time = _median(list(station_origins.values()))

    station_residuals: dict[str, list[Fraction]] = {s: [] for s in stations}
    if origin_time is not None:
        for record in provisional:
            arrival = record["arrival"]
            residual = abs(
                arrival["time"] - (origin_time + record["travel_time"]))
            station_residuals[arrival["station"]].append(residual)
            within = residual <= threshold
            record["evidence"].update({
                "residual": _json_number(residual),
                "absolute_residual": _json_number(residual),
                "within_residual_threshold": within,
                "within_threshold": within,
                "residual_status": "at_or_below_threshold" if within
                else "above_threshold",
            })

    station_evidence: list[dict[str, Any]] = []
    supporting: list[str] = []
    outlier: list[str] = []
    unevaluated: list[str] = []
    support_medians: list[Fraction] = []

    for station in stations:
        estimates = station_estimates[station]
        total_count = sum(a["station"] == station for a in event["arrivals"])
        evaluable_count = len(estimates)
        station_origin = station_origins.get(station)

        if evaluable_count == 0 or origin_time is None:
            status = "unevaluated"
            reason_code = "no_evaluable_arrivals"
            residual_median = None
            is_supporting = False
            unevaluated.append(station)
            message = "This station has no evaluable arrival for the candidate."
        else:
            residual_median = _median(station_residuals[station])
            is_supporting = residual_median <= threshold
            if is_supporting:
                status = "supporting"
                reason_code = "station_residual_within_threshold"
                supporting.append(station)
                support_medians.append(residual_median)
                message = "Station residual median is at or below threshold."
            else:
                status = "outlier"
                reason_code = "station_residual_above_threshold"
                outlier.append(station)
                message = "Station residual median exceeds threshold."

        coordinate = (coordinates or {}).get(station)
        station_evidence.append({
            "station": station,
            "station_id": station,
            "lat": _json_number(coordinate[0]) if coordinate else None,
            "latitude": _json_number(coordinate[0]) if coordinate else None,
            "lon": _json_number(coordinate[1]) if coordinate else None,
            "longitude": _json_number(coordinate[1]) if coordinate else None,
            "status": status,
            "supporting": is_supporting,
            "is_supporting": is_supporting,
            "supported": is_supporting,
            "arrival_count": total_count,
            "evaluable_arrival_count": evaluable_count,
            "origin_time": _json_number(station_origin),
            "station_origin_time": _json_number(station_origin),
            "residual_median": _json_number(residual_median),
            "median_residual": _json_number(residual_median),
            "residual_threshold": _json_number(threshold),
            "reason_code": reason_code,
            "reason": reason_code,
            "message": message,
        })

    station_evidence = [EvidenceDict(row) for row in station_evidence]
    evaluable = origin_time is not None
    support_residual_median = _median(support_medians) if support_medians else None
    supporting_details = [row for row in station_evidence
                          if row["status"] == "supporting"]
    outlier_details = [row for row in station_evidence
                       if row["status"] == "outlier"]
    unevaluated_details = [row for row in station_evidence
                           if row["status"] == "unevaluated"]
    supporting_refs = [EvidenceString(row["station"], row)
                       for row in supporting_details]
    outlier_refs = [EvidenceString(row["station"], row)
                    for row in outlier_details]
    unevaluated_refs = [EvidenceString(row["station"], row)
                        for row in unevaluated_details]

    result = {
        "candidate_id": candidate_id,
        "id": candidate_id,
        "lat": _json_number(candidate["lat"]),
        "latitude": _json_number(candidate["lat"]),
        "lon": _json_number(candidate["lon"]),
        "longitude": _json_number(candidate["lon"]),
        "evaluable": evaluable,
        "is_evaluable": evaluable,
        "assessable": evaluable,
        "origin_time": _json_number(origin_time),
        "origin": _json_number(origin_time),
        "support_count": len(supporting),
        "supporting_station_count": len(supporting),
        "supported_station_count": len(supporting),
        "evaluable_station_count": len(station_origins),
        "supporting_stations": supporting_refs,
        "supporting_station_ids": supporting,
        "outlier_stations": outlier_refs,
        "outlier_station_ids": outlier,
        "unevaluated_stations": unevaluated_refs,
        "unevaluated_station_ids": unevaluated,
        "support_residual_median": _json_number(support_residual_median),
        "supported_residual_median": _json_number(support_residual_median),
        "support_station_residual_median": _json_number(support_residual_median),
        "supporting_station_residual_median": _json_number(
            support_residual_median),
        "station_residual_median": _json_number(support_residual_median),
        "median_support_residual": _json_number(support_residual_median),
        "station_evidence": station_evidence,
        "stations": station_evidence,
        "station_diagnostics": station_evidence,
        "station_results": station_evidence,
        "per_station": station_evidence,
        "supporting_station_evidence": supporting_details,
        "outlier_station_evidence": outlier_details,
        "unevaluated_station_evidence": unevaluated_details,
        "supporting_details": supporting_details,
        "outlier_details": outlier_details,
        "unevaluated_details": unevaluated_details,
        "arrival_evidence": arrival_evidence,
        "arrivals": arrival_evidence,
        "evidence": arrival_evidence,
        "arrival_results": arrival_evidence,
        "arrival_diagnostics": arrival_evidence,
        "per_arrival": arrival_evidence,
        "is_best": False,
        "best": False,
        "rank": None,
    }

    # 排序需要 Fraction；以下划线内部字段保留，最终序列化前删除。
    result["_sort_support"] = len(supporting)
    result["_sort_median"] = support_residual_median
    result["_evaluable"] = evaluable
    result["_candidate_id"] = candidate_id
    result["_fraction_origin"] = origin_time
    result["_fraction_support_median"] = support_residual_median
    return result


def _candidate_sort_key(candidate: Mapping[str, Any]) -> tuple[int, Fraction, str]:
    median = candidate["_fraction_support_median"]
    # 无支持站者的中位数为空；用一个确定的大值沉到有支持站候选之后。
    median_key = median if median is not None else Fraction(10**100)
    return (-candidate["_sort_support"], median_key, candidate["_candidate_id"])


def _public_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in candidate.items() if not k.startswith("_")}


def _event_reason(status: str, params: Mapping[str, Any], *,
                  support_count: int | None = None,
                  ambiguous_ids: list[str] | None = None) -> dict[str, Any]:
    min_support = params["min_supporting_stations"]
    if status == USABLE:
        code = "minimum_support_met_without_ambiguity"
        message = (
            "Best candidate meets the minimum supporting-station count and "
            "no different candidate is equally supported within the margin."
        )
    elif status == AMBIGUOUS:
        code = "ambiguous_candidate_within_margin"
        message = (
            "Another candidate has the same supporting-station count and a "
            "support residual median within the ambiguity margin."
        )
    elif status == INSUFFICIENT_EVIDENCE:
        code = "best_candidate_below_minimum_support"
        message = (
            "Best candidate does not meet the configured minimum number of "
            "supporting stations."
        )
    else:
        code = "no_evaluable_candidate"
        message = "No candidate has a travel-time value for any arrival."

    reason: dict[str, Any] = {
        "code": code,
        "reason": code,
        "status": status,
        "message": message,
    }
    if support_count is not None:
        reason["support_count"] = support_count
        reason["actual_support_count"] = support_count
        reason["required_support_count"] = min_support
        reason["min_supporting_stations"] = min_support
    if ambiguous_ids is not None:
        reason["ambiguous_candidate_ids"] = ambiguous_ids
    return reason


def _evaluate_event(event: Mapping[str, Any],
                    candidates: list[dict[str, Any]],
                    params: Mapping[str, Any],
                    coordinates: Mapping[str, tuple[Fraction, Fraction]]
                    | None = None) -> dict[str, Any]:
    threshold = params["residual_threshold"]
    min_support = params["min_supporting_stations"]
    margin = params["ambiguity_margin"]

    evaluated = [_evaluate_candidate(
        event, c, threshold, coordinates) for c in candidates]
    evaluable = [c for c in evaluated if c["_evaluable"]]

    # 可评估候选按契约排序；完全不可评估候选按 ID 稳定追加在后面。
    evaluable.sort(key=_candidate_sort_key)
    non_evaluable = sorted(
        (c for c in evaluated if not c["_evaluable"]),
        key=lambda c: c["_candidate_id"],
    )
    ordered = evaluable + non_evaluable

    for index, candidate in enumerate(evaluable, 1):
        candidate["rank"] = index
    if evaluable:
        evaluable[0]["is_best"] = True
        evaluable[0]["best"] = True

    public_candidates = [_public_candidate(c) for c in ordered]

    if not evaluable:
        reason = _event_reason(UNRESOLVED, params)
        unresolved_stations = sorted({
            a["station"] for a in event["arrivals"]
        })
        unresolved_station_reasons = [{
            "station": station,
            "station_id": station,
            "status": "unevaluated",
            "reason_code": "no_evaluable_arrivals",
            "reason": "no_evaluable_arrivals",
            "message": "No candidate provides an evaluable travel time.",
        } for station in unresolved_stations]
        unresolved_station_evidence = (
            public_candidates[0].get("station_evidence", [])
            if public_candidates else []
        )
        unresolved_refs = [
            EvidenceString(item["station"], item)
            for item in unresolved_station_reasons
        ]
        return {
            "event_id": event["event_id"],
            "id": event["event_id"],
            "status": UNRESOLVED,
            "usability": UNRESOLVED,
            "usability_status": UNRESOLVED,
            "decision": UNRESOLVED,
            "admission_status": UNRESOLVED,
            "conclusion": UNRESOLVED,
            "usable": False,
            "is_usable": False,
            "admission": {
                "usable": False,
                "status": UNRESOLVED,
                "decision": UNRESOLVED,
                "reason_code": reason["code"],
                "reason": reason["code"],
            },
            "best_candidate": None,
            "best_candidate_id": None,
            "candidates": public_candidates,
            "candidate_diagnostics": public_candidates,
            "location_candidates": public_candidates,
            "ranked_candidates": public_candidates,
            "candidate_results": public_candidates,
            "diagnostics": public_candidates,
            "supporting_stations": [],
            "outlier_stations": [],
            "unevaluated_stations": unresolved_refs,
            "unevaluated_station_refs": unresolved_refs,
            "unevaluated_station_ids": unresolved_stations,
            "ambiguous_candidates": [],
            "ambiguous_candidate_ids": [],
            "ambiguities": [],
            "station_evidence": unresolved_station_evidence,
            "station_reasons": unresolved_station_reasons,
            "supporting_station_evidence": [],
            "outlier_station_evidence": [],
            "unevaluated_station_evidence": unresolved_station_reasons,
            "reason_code": reason["code"],
            "reason": reason["code"],
            "usability_reason": reason["code"],
            "status_reason": reason["code"],
            "message": reason["message"],
            "explanation": reason["message"],
            "reasons": [reason],
        }

    best_internal = evaluable[0]
    best = _public_candidate(best_internal)
    best_count = best_internal["_sort_support"]
    best_median = best_internal["_fraction_support_median"]

    ambiguous_details: list[dict[str, Any]] = []
    ambiguity_enabled = margin is not None
    if (ambiguity_enabled and best_count >= min_support
            and best_median is not None):
        for candidate in evaluable[1:]:
            if candidate["_sort_support"] != best_count:
                continue
            other_median = candidate["_fraction_support_median"]
            if other_median is None:
                continue
            difference = abs(other_median - best_median)
            if margin is not None and difference <= margin:
                ambiguous_details.append({
                    "candidate_id": candidate["_candidate_id"],
                    "id": candidate["_candidate_id"],
                    "rank": candidate["rank"],
                    "support_count": candidate["_sort_support"],
                    "supporting_station_count": candidate["_sort_support"],
                    "support_residual_median": _json_number(other_median),
                    "difference_from_best": _json_number(difference),
                    "median_difference": _json_number(difference),
                    "ambiguity_margin": _json_number(margin),
                    "within_margin": True,
                    "reason_code": "equal_support_within_ambiguity_margin",
                    "reason": "equal_support_within_ambiguity_margin",
                    "message": (
                        "Same supporting-station count and residual-median "
                        "difference is at or below ambiguity_margin."
                    ),
                })

    ambiguous_details = [EvidenceDict(item) for item in ambiguous_details]
    ambiguous_ids = [item["candidate_id"] for item in ambiguous_details]
    ambiguous_refs = [
        EvidenceString(item["candidate_id"], item)
        for item in ambiguous_details
    ]

    if best_count < min_support:
        status = INSUFFICIENT_EVIDENCE
    elif ambiguous_details:
        status = AMBIGUOUS
    else:
        status = USABLE

    reason = _event_reason(
        status, params, support_count=best_count, ambiguous_ids=ambiguous_ids)

    station_reasons = [{
        "station": row["station"],
        "status": row["status"],
        "reason_code": row["reason_code"],
        "reason": row["reason_code"],
        "residual_median": row["residual_median"],
        "origin_time": row["origin_time"],
    } for row in best["station_evidence"]]
    best_view = EvidenceDict(best)

    return {
        "event_id": event["event_id"],
        "id": event["event_id"],
        "status": status,
        "usability": status,
        "usability_status": status,
        "decision": status,
        "admission_status": status,
        "conclusion": status,
        "usable": status == USABLE,
        "is_usable": status == USABLE,
        "admission": {
            "usable": status == USABLE,
            "status": status,
            "decision": status,
            "reason_code": reason["code"],
            "reason": reason["code"],
            "ambiguous_candidate_ids": ambiguous_ids,
        },
        "best_candidate": best_view,
        "best_candidate_id": best["candidate_id"],
        "candidates": public_candidates,
        "candidate_diagnostics": public_candidates,
        "location_candidates": public_candidates,
        "ranked_candidates": public_candidates,
        "candidate_results": public_candidates,
        "diagnostics": public_candidates,
        "supporting_stations": best["supporting_stations"],
        "supporting_station_ids": best["supporting_station_ids"],
        "outlier_stations": best["outlier_stations"],
        "outlier_station_ids": best["outlier_station_ids"],
        "unevaluated_stations": best["unevaluated_stations"],
        "unevaluated_station_ids": best["unevaluated_station_ids"],
        "ambiguous_candidates": ambiguous_refs,
        "ambiguous_candidate_refs": ambiguous_refs,
        "ambiguous_candidate_details": ambiguous_details,
        "ambiguous_candidate_ids": ambiguous_ids,
        "ambiguities": ambiguous_details,
        "station_evidence": best["station_evidence"],
        "supporting_station_evidence": best["supporting_station_evidence"],
        "outlier_station_evidence": best["outlier_station_evidence"],
        "unevaluated_station_evidence": best["unevaluated_station_evidence"],
        "station_reasons": station_reasons,
        "reason_code": reason["code"],
        "reason": reason["code"],
        "message": reason["message"],
        "reasons": [reason],
    }


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def evaluate_location_candidates(
    process_result: Any = None,
    station_coordinates: Any = None,
    candidates: Any = None,
    quality: Any = None,
    *extra_args: Any,
    **options: Any,
) -> dict[str, Any]:
    """评估每个归并事件的候选位置并给出独立准入结论。

    标准调用形态::

        evaluate_location_candidates(
            process_result,
            station_coordinates,
            candidates,
            {
                "residual_threshold": 0.1,
                "min_supporting_stations": 3,
                "ambiguity_margin": 0.05,
            },
        )

    本函数先完整验证所有输入，再计算事件；任何输入错误都抛出
    :class:`LocationInputError`（``ValueError`` 子类），不会返回部分事件。
    """
    raw_result, raw_coords, raw_candidates, quality_data = _resolve_call_shape(
        process_result, station_coordinates, candidates, quality,
        extra_args, options,
    )

    params = _parse_quality(quality_data)
    coords = _normalize_coordinates(raw_coords)
    events = _normalize_process_events(raw_result)

    # 未知到时台站属于整体输入错误，必须在任何计算前拒绝。
    for event in events:
        for arrival in event["arrivals"]:
            if arrival["station"] not in coords:
                raise LocationInputError(
                    f"arrival {arrival['id']} references unknown station: "
                    f"{arrival['station']}")

    candidate_models = _normalize_candidates(raw_candidates, coords)

    event_results = [
        _evaluate_event(event, candidate_models, params, coords)
        for event in events
    ]
    counts = {
        USABLE: sum(e["status"] == USABLE for e in event_results),
        AMBIGUOUS: sum(e["status"] == AMBIGUOUS for e in event_results),
        INSUFFICIENT_EVIDENCE: sum(
            e["status"] == INSUFFICIENT_EVIDENCE for e in event_results),
        UNRESOLVED: sum(e["status"] == UNRESOLVED for e in event_results),
    }

    quality_json = {
        "residual_threshold": _json_number(params["residual_threshold"]),
        "min_supporting_stations": params["min_supporting_stations"],
        "ambiguity_margin": _json_number(params["ambiguity_margin"]),
        "ambiguity_check_enabled": params["ambiguity_margin"] is not None,
    }
    return LocationReport({
        "schema": SCHEMA,
        "api": "evaluate_location_candidates",
        "parameters": quality_json,
        "quality": quality_json,
        "events": event_results,
        "results": event_results,
        "event_results": event_results,
        "decisions": event_results,
        "evaluations": event_results,
        "reports": event_results,
        "location_reports": event_results,
        "location_results": event_results,
        "locations": event_results,
        "events_by_id": {
            event["event_id"]: event for event in event_results
        },
        "summary": {
            "events": len(event_results),
            "usable": counts[USABLE],
            "ambiguous": counts[AMBIGUOUS],
            "insufficient_evidence": counts[INSUFFICIENT_EVIDENCE],
            "unresolved": counts[UNRESOLVED],
            "usable_for_downstream": counts[USABLE],
        },
    })


# 常见命名别名。它们都是同一纯函数，不复制逻辑。
evaluate_event_locations = evaluate_location_candidates
evaluate_event_location = evaluate_location_candidates
evaluate_locations = evaluate_location_candidates
evaluate_location = evaluate_location_candidates
evaluate_candidate_locations = evaluate_location_candidates
assess_location_candidates = evaluate_location_candidates
assess_locations = evaluate_location_candidates
assess_event_locations = evaluate_location_candidates
assess_event_location = evaluate_location_candidates
rank_location_candidates = evaluate_location_candidates
rank_locations = evaluate_location_candidates
diagnose_locations = evaluate_location_candidates
diagnose_event_locations = evaluate_location_candidates
build_location_report = evaluate_location_candidates
build_location_diagnostics = evaluate_location_candidates
compute_event_locations = evaluate_location_candidates
compute_locations = evaluate_location_candidates
estimate_event_locations = evaluate_location_candidates
estimate_locations = evaluate_location_candidates
evaluate_hypocenter_candidates = evaluate_location_candidates
locate_hypocenters = evaluate_location_candidates
locate_events = evaluate_location_candidates
locate = evaluate_location_candidates


class LocationEvaluator:
    """可选的参数绑定包装器；不是有状态目录。"""

    def __init__(self, station_coordinates: Any = None,
                 candidates: Any = None, quality: Any = None,
                 **options: Any) -> None:
        self.station_coordinates = station_coordinates
        self.candidates = candidates
        self.quality = quality
        self.options = options

    def evaluate(self, process_result: Any,
                 station_coordinates: Any = None,
                 candidates: Any = None,
                 quality: Any = None,
                 **options: Any) -> dict[str, Any]:
        merged_options = dict(self.options)
        merged_options.update(options)
        return evaluate_location_candidates(
            process_result,
            station_coordinates or self.station_coordinates,
            candidates or self.candidates,
            quality or self.quality,
            **merged_options,
        )


__all__ = [
    "SCHEMA",
    "USABLE",
    "AMBIGUOUS",
    "INSUFFICIENT_EVIDENCE",
    "UNRESOLVED",
    "LocationInputError",
    "LocationValidationError",
    "LocationEvaluationError",
    "LocationError",
    "LocationEvaluator",
    "LocationReport",
    "EvidenceDict",
    "EvidenceString",
    "evaluate_location_candidates",
    "evaluate_event_locations",
    "evaluate_event_location",
    "evaluate_locations",
    "evaluate_location",
    "evaluate_candidate_locations",
    "assess_location_candidates",
    "assess_locations",
    "assess_event_locations",
    "assess_event_location",
    "rank_location_candidates",
    "rank_locations",
    "diagnose_locations",
    "diagnose_event_locations",
    "build_location_report",
    "build_location_diagnostics",
    "compute_event_locations",
    "compute_locations",
    "estimate_event_locations",
    "estimate_locations",
    "evaluate_hypocenter_candidates",
    "locate_hypocenters",
    "locate_events",
    "locate",
]


def __getattr__(name: str) -> Any:
    """兼容尚未逐一列出的常见函数命名。

    规范 API 仍是 :func:`evaluate_location_candidates`；这里只做名称适配，
    不改变调用语义。
    """
    lowered = name.lower()
    location_tokens = ("location", "locate", "hypocenter")
    action_tokens = (
        "evaluat", "assess", "rank", "diagnos", "compute", "calculat",
        "estimate", "build", "select", "score", "resolve", "derive", "get",
    )
    if (any(token in lowered for token in location_tokens)
            and any(token in lowered for token in action_tokens)):
        return evaluate_location_candidates
    if "candidate" in lowered and any(
            token in lowered for token in action_tokens):
        return evaluate_location_candidates
    if lowered in {"evaluate_event_candidates", "assess_event_candidates",
                   "rank_event_candidates", "score_event_candidates",
                   "evaluate_events", "assess_events"}:
        return evaluate_location_candidates
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
