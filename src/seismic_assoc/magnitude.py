"""局部震级计算、清单差分与下游接续的纯函数契约。

本模块不访问文件系统或网络。目录持久化由 :mod:`seismic_assoc.magnitude_catalog`
负责，但清单差分的应用函数 :func:`apply_manifest_delta` 同样是无 IO 的纯函数。
"""
from __future__ import annotations

import json
import math
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any

SCHEMA = "seismic-assoc.local-magnitudes/v1"
MANIFEST_SCHEMA = "seismic-assoc.magnitude-manifest/v1"
CHANGE_PACKAGE_SCHEMA = "seismic-assoc.magnitude-change-package/v1"
CURSOR_SCHEMA = "seismic-assoc.manifest-cursor/v1"

EARTH_RADIUS_KM = 6371.0088

LOCATION_USABLE = "usable"
LOCATION_AMBIGUOUS = "ambiguous"
LOCATION_INSUFFICIENT_EVIDENCE = "insufficient_evidence"
LOCATION_UNRESOLVED = "unresolved"
LOCATION_STATUSES = frozenset({
    LOCATION_USABLE,
    LOCATION_AMBIGUOUS,
    LOCATION_INSUFFICIENT_EVIDENCE,
    LOCATION_UNRESOLVED,
})

# 观测/事件原因码。
UNSUPPORTED_PHASE = "unsupported_phase"
NO_CALIBRATION = "no_calibration"
CALIBRATION_BOUNDARY = "calibration_boundary"
LOCATION_NOT_USABLE = "location_not_usable"
DISTANCE_OUT_OF_RANGE = "distance_out_of_range"
INSUFFICIENT_AMPLITUDE_STATIONS = "insufficient_amplitude_stations"
USABLE = "usable"


class MagnitudeInputError(ValueError):
    """震级计算输入不合法；它是 ``ValueError`` 的子类。"""


class ManifestDeltaError(ValueError):
    """变更包、清单或游标不匹配或不合法。"""


# 兼容可能的异常命名习惯。
MagnitudeValidationError = MagnitudeInputError
ManifestCursorError = ManifestDeltaError


def _fail(message: str) -> None:
    raise MagnitudeInputError(message)


def _identity(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{label} must be a nonempty string")
    return value


def _strict_integer(value: Any, label: str, *, nonnegative: bool = False) -> int:
    # bool 是 int 的子类，但采样端点和整数参数明确不接受布尔值。
    if type(value) is not int:
        _fail(f"{label} must be an integer; booleans are not accepted")
    if nonnegative and value < 0:
        _fail(f"{label} must be nonnegative")
    return value


def _finite_number(value: Any, label: str, *, positive: bool = False,
                   nonnegative: bool = False) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{label} must be a finite number; booleans are not accepted")
    try:
        number = float(value)
    except OverflowError:
        _fail(f"{label} is too large to represent as a finite number")
    if not math.isfinite(number):
        _fail(f"{label} must be finite")
    if positive and number <= 0:
        _fail(f"{label} must be positive")
    if nonnegative and number < 0:
        _fail(f"{label} must be nonnegative")
    return value


def _coordinate_number(value: Any, label: str) -> float:
    """接受现有位置 API 支持的 int/float/Decimal/数字字符串坐标。"""
    if isinstance(value, bool) or value is None:
        _fail(f"{label} must be a finite coordinate number")
    try:
        if isinstance(value, bool):
            raise ValueError("boolean coordinate")
        if isinstance(value, Fraction):
            result = float(value)
        elif isinstance(value, (int, float, Decimal)):
            result = float(value)
        elif isinstance(value, str):
            text = value.strip()
            if "/" in text:
                result = float(Fraction(text))
            else:
                result = float(Decimal(text))
        else:
            _fail(f"{label} must be a finite coordinate number")
    except (InvalidOperation, ValueError, ZeroDivisionError, OverflowError):
        _fail(f"{label} must be a finite coordinate number")
    if not math.isfinite(result):
        _fail(f"{label} must be finite")
    return result


def _numeric_key(value: int | float) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        _fail(f"invalid numeric value: {exc}")
        raise  # pragma: no cover - _fail 总是抛出


def _json_copy(value: Any) -> Any:
    """严格 JSON 往返；拒绝 NaN/Infinity，并保证调用方对象不被输出别名修改。"""
    return json.loads(json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=False))


def normalize_station_coordinates(raw: Any) -> dict[str, dict[str, float]]:
    """规范化现有位置 API 形状的台站坐标。"""
    entries: Any
    if isinstance(raw, Mapping):
        entries = raw.items()
    elif isinstance(raw, list):
        entries = []
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping):
                _fail(f"station_coordinates[{index}] must be an object")
            station_id = item.get("id", item.get("station", item.get("station_id")))
            entries.append((station_id, item))
    else:
        _fail("station_coordinates must be a mapping or list")

    coords: dict[str, dict[str, float]] = {}
    for raw_station, value in entries:
        station = _identity(raw_station, "station id")
        if station in coords:
            _fail(f"duplicate station coordinates: {station}")

        if isinstance(value, (list, tuple)):
            if len(value) != 2:
                _fail(f"coordinate pair for station {station} must have lat and lon")
            raw_lat, raw_lon = value
        elif isinstance(value, Mapping):
            raw_lat = value.get("lat", value.get("latitude"))
            raw_lon = value.get(
                "lon", value.get("lng", value.get("longitude")))
        else:
            _fail(f"invalid coordinates for station {station}")

        if raw_lat is None or raw_lon is None:
            _fail(f"coordinates for station {station} require lat and lon")
        lat = _coordinate_number(raw_lat, f"station {station} latitude")
        lon = _coordinate_number(raw_lon, f"station {station} longitude")
        if not -90.0 <= lat <= 90.0:
            _fail(f"latitude for station {station} is out of range")
        if not -180.0 <= lon <= 180.0:
            _fail(f"longitude for station {station} is out of range")
        coords[station] = {"lat": lat, "lon": lon}

    if not coords:
        _fail("station_coordinates must not be empty")
    return {station: coords[station] for station in sorted(coords)}


def _normalize_process_result(process_result: Any) -> dict[str, Any]:
    if not isinstance(process_result, Mapping):
        _fail("process_result must be the object returned by process()")
    raw_events = process_result.get("events")
    if not isinstance(raw_events, list) or not raw_events:
        _fail("process_result must contain a nonempty events list")

    events: list[dict[str, Any]] = []
    event_ids: set[str] = set()
    arrival_ids: set[str] = set()
    for e_index, raw_event in enumerate(raw_events):
        if not isinstance(raw_event, Mapping):
            _fail(f"process_result.events[{e_index}] must be an object")
        event_id = _identity(
            raw_event.get("event_id", raw_event.get("id")), "event_id")
        if event_id in event_ids:
            _fail(f"duplicate event_id: {event_id}")
        event_ids.add(event_id)

        raw_arrivals = raw_event.get("arrivals")
        if not isinstance(raw_arrivals, list):
            _fail(f"event {event_id} must contain an arrivals list")
        arrivals: list[dict[str, str]] = []
        for a_index, raw_arrival in enumerate(raw_arrivals):
            if not isinstance(raw_arrival, Mapping):
                _fail(f"event {event_id} arrival {a_index} must be an object")
            arrival_id = _identity(raw_arrival.get("id"), "arrival id")
            if arrival_id in arrival_ids:
                _fail(f"duplicate arrival id in batch: {arrival_id}")
            arrival_ids.add(arrival_id)
            station = _identity(raw_arrival.get("station"),
                                 f"arrival {arrival_id} station")
            phase = raw_arrival.get("phase")
            if phase not in ("P", "S"):
                _fail(f"arrival {arrival_id} phase must be P or S")
            arrivals.append({
                "id": arrival_id,
                "station": station,
                "phase": phase,
            })
        events.append({"event_id": event_id, "arrivals": arrivals})

    return {"events": events}


def normalize_location_report(location_report: Any,
                              event_ids: set[str] | None = None
                              ) -> dict[str, dict[str, Any]]:
    """只规范化震级功能消费的 ``event_id/status/best_candidate.lat/lon``。"""
    if isinstance(location_report, Mapping) and "events" in location_report:
        raw_events = location_report["events"]
    elif isinstance(location_report, list):
        raw_events = location_report
    elif isinstance(location_report, Mapping) and "event_id" in location_report:
        raw_events = [location_report]
    else:
        _fail("location_report must be an evaluate_location_candidates result")
    if not isinstance(raw_events, list):
        _fail("location_report events must be a list")

    by_event: dict[str, dict[str, Any]] = {}
    for index, raw_event in enumerate(raw_events):
        if not isinstance(raw_event, Mapping):
            _fail(f"location_report.events[{index}] must be an object")
        event_id = _identity(raw_event.get("event_id", raw_event.get("id")),
                             "location event_id")
        if event_id in by_event:
            _fail(f"duplicate location result for event {event_id}")
        status = raw_event.get("status", raw_event.get("location_status"))
        if status not in LOCATION_STATUSES:
            _fail(f"location result for {event_id} has unsupported status {status!r}")

        best: dict[str, float] | None = None
        if status == LOCATION_USABLE:
            candidate = raw_event.get("best_candidate")
            if not isinstance(candidate, Mapping):
                _fail(f"usable location {event_id} requires best_candidate")
            raw_lat = candidate.get("lat", candidate.get("latitude"))
            raw_lon = candidate.get(
                "lon", candidate.get("lng", candidate.get("longitude")))
            if raw_lat is None or raw_lon is None:
                _fail(f"usable location {event_id} requires lat/lon")
            lat = _coordinate_number(raw_lat, f"location {event_id} latitude")
            lon = _coordinate_number(raw_lon, f"location {event_id} longitude")
            if not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0:
                _fail(f"usable location {event_id} has illegal coordinates")
            best = {"lat": lat, "lon": lon}

        by_event[event_id] = {
            "event_id": event_id,
            "status": status,
            "best_candidate": best,
        }

    if event_ids is not None:
        missing = sorted(event_ids - set(by_event))
        extra = sorted(set(by_event) - event_ids)
        if missing:
            _fail(f"location_report is missing events: {', '.join(missing)}")
        if extra:
            _fail(f"location_report contains unknown events: {', '.join(extra)}")
    return {event_id: by_event[event_id] for event_id in sorted(by_event)}


def location_report_to_contract(by_event: Mapping[str, Mapping[str, Any]]
                                ) -> dict[str, Any]:
    """把仅含消费字段的位置诊断恢复成标准 ``{"events": [...]}`` 形状。"""
    return {"events": [
        {
            "event_id": event_id,
            "status": item["status"],
            "best_candidate": (
                None if item["best_candidate"] is None
                else {"lat": item["best_candidate"]["lat"],
                      "lon": item["best_candidate"]["lon"]}
            ),
        }
        for event_id, item in sorted(by_event.items())
    ]}


def _interval(record: Mapping[str, Any], label: str) -> tuple[int, int]:
    start = _strict_integer(record.get("sample_start"),
                            f"{label}.sample_start", nonnegative=True)
    end = _strict_integer(record.get("sample_end"),
                          f"{label}.sample_end", nonnegative=True)
    if start >= end:
        _fail(f"{label} interval must be a nonempty half-open [start, end) interval")
    return start, end


def normalize_amplitude_observations(raw: Any,
                                    arrivals: Mapping[str, Mapping[str, str]]
                                    ) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        _fail("amplitude_observations must be a list")
    required = {
        "measurement_id", "arrival_id", "station", "channel",
        "sample_start", "sample_end", "peak_to_peak_counts",
    }
    measurements: set[str] = set()
    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        label = f"amplitude_observations[{index}]"
        if not isinstance(item, Mapping):
            _fail(f"{label} must be an object")
        if set(item) != required:
            missing = sorted(required - set(item))
            extra = sorted(set(item) - required)
            _fail(f"{label} has invalid fields (missing={missing}, extra={extra})")

        measurement_id = _identity(item["measurement_id"], "measurement_id")
        if measurement_id in measurements:
            _fail(f"duplicate measurement_id: {measurement_id}")
        measurements.add(measurement_id)

        arrival_id = _identity(item["arrival_id"],
                               f"measurement {measurement_id} arrival_id")
        if arrival_id not in arrivals:
            _fail(f"measurement {measurement_id} references unknown arrival {arrival_id}")
        arrival = arrivals[arrival_id]
        station = _identity(item["station"],
                             f"measurement {measurement_id} station")
        if station != arrival["station"]:
            _fail(
                f"measurement {measurement_id} station {station!r} does not match "
                f"arrival {arrival_id} station {arrival['station']!r}"
            )
        channel = _identity(item["channel"],
                             f"measurement {measurement_id} channel")
        start, end = _interval(item, f"measurement {measurement_id}")
        counts = _finite_number(
            item["peak_to_peak_counts"],
            f"measurement {measurement_id} peak_to_peak_counts",
            positive=True,
        )
        result.append({
            "measurement_id": measurement_id,
            "arrival_id": arrival_id,
            "station": station,
            "channel": channel,
            "sample_start": start,
            "sample_end": end,
            "peak_to_peak_counts": counts,
        })
    return sorted(result, key=lambda row: row["measurement_id"])


def normalize_instrument_epochs(raw: Any,
                               coords: Mapping[str, Any]
                               ) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        _fail("instrument_epochs must be a list")
    required = {
        "epoch_id", "station", "channel", "sample_start", "sample_end",
        "counts_per_mm",
    }
    epoch_ids: set[str] = set()
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        label = f"instrument_epochs[{index}]"
        if not isinstance(item, Mapping):
            _fail(f"{label} must be an object")
        if set(item) != required:
            missing = sorted(required - set(item))
            extra = sorted(set(item) - required)
            _fail(f"{label} has invalid fields (missing={missing}, extra={extra})")
        epoch_id = _identity(item["epoch_id"], "epoch_id")
        if epoch_id in epoch_ids:
            _fail(f"duplicate epoch_id: {epoch_id}")
        epoch_ids.add(epoch_id)
        station = _identity(item["station"], f"epoch {epoch_id} station")
        if station not in coords:
            _fail(f"epoch {epoch_id} references station outside coordinates: {station}")
        channel = _identity(item["channel"], f"epoch {epoch_id} channel")
        start, end = _interval(item, f"epoch {epoch_id}")
        gain = _finite_number(
            item["counts_per_mm"], f"epoch {epoch_id} counts_per_mm", positive=True)
        rows.append({
            "epoch_id": epoch_id,
            "station": station,
            "channel": channel,
            "sample_start": start,
            "sample_end": end,
            "counts_per_mm": gain,
        })

    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        by_key.setdefault((row["station"], row["channel"]), []).append(row)
    for (station, channel), intervals in by_key.items():
        intervals.sort(key=lambda row: (row["sample_start"], row["sample_end"]))
        for left, right in zip(intervals, intervals[1:]):
            # 半开区间：start < previous_end 才是重叠；端点相接合法。
            if right["sample_start"] < left["sample_end"]:
                _fail(
                    "overlapping instrument epochs for "
                    f"station={station!r}, channel={channel!r}: "
                    f"{left['epoch_id']} overlaps {right['epoch_id']}"
                )
    return sorted(rows, key=lambda row: row["epoch_id"])


def normalize_distance_corrections(raw: Any) -> list[dict[str, float]]:
    if not isinstance(raw, list) or len(raw) < 2:
        _fail("distance_corrections requires at least two points")
    required = {"distance_km", "correction"}
    rows: list[dict[str, float]] = []
    seen_distances: set[Decimal] = set()
    for index, item in enumerate(raw):
        label = f"distance_corrections[{index}]"
        if not isinstance(item, Mapping) or set(item) != required:
            _fail(f"{label} must contain exactly distance_km and correction")
        distance = _finite_number(item["distance_km"],
                                  f"{label}.distance_km", nonnegative=True)
        correction = _finite_number(item["correction"],
                                    f"{label}.correction")
        key = _numeric_key(distance)
        if key in seen_distances:
            _fail(f"duplicate distance_km in correction table: {distance}")
        seen_distances.add(key)
        rows.append({"distance_km": float(distance),
                     "correction": float(correction)})
    rows.sort(key=lambda row: row["distance_km"])
    return rows


def normalize_min_stations(value: Any) -> int:
    if type(value) is not int or isinstance(value, bool) or value < 2:
        _fail("min_stations must be an integer of at least 2; booleans are rejected")
    return value


def prepare_magnitude_inputs(
    process_result: Mapping[str, Any],
    location_report: Mapping[str, Any],
    station_coordinates: Any,
    amplitude_observations: Any,
    instrument_epochs: Any,
    distance_corrections: Any,
    min_stations: int = 3,
) -> dict[str, Any]:
    """验证并规范化震级契约的全部输入，但不计算震级。"""
    coords = normalize_station_coordinates(station_coordinates)
    process = _normalize_process_result(process_result)

    arrivals: dict[str, dict[str, str]] = {}
    event_ids: set[str] = set()
    for event in process["events"]:
        event_ids.add(event["event_id"])
        for arrival in event["arrivals"]:
            if arrival["station"] not in coords:
                _fail(
                    f"arrival {arrival['id']} references station without "
                    f"coordinates: {arrival['station']}"
                )
            arrivals[arrival["id"]] = {
                **arrival,
                "event_id": event["event_id"],
            }

    locations = normalize_location_report(location_report, event_ids)
    observations = normalize_amplitude_observations(
        amplitude_observations, arrivals)
    epochs = normalize_instrument_epochs(instrument_epochs, coords)
    corrections = normalize_distance_corrections(distance_corrections)
    minimum = normalize_min_stations(min_stations)

    return {
        "process": process,
        "arrivals": arrivals,
        "event_ids": event_ids,
        "coordinates": coords,
        "locations": locations,
        "observations": observations,
        "epochs": epochs,
        "corrections": corrections,
        "min_stations": minimum,
    }


def great_circle_distance_km(lat1: float, lon1: float,
                             lat2: float, lon2: float) -> float:
    """按 R=6371.0088 km 的 Haversine 大圆距离计算。"""
    rlat1 = math.radians(lat1)
    rlat2 = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    haver = (math.sin(dlat / 2.0) ** 2
             + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2.0) ** 2)
    # 消除端点浮点误差造成的略微越界。
    haver = min(1.0, max(0.0, haver))
    central_angle = 2.0 * math.atan2(math.sqrt(haver), math.sqrt(1.0 - haver))
    distance = EARTH_RADIUS_KM * central_angle
    if not math.isfinite(distance) or distance < 0.0:
        _fail("computed great-circle distance is not finite")
    return distance


# 更直白的别名。
haversine_distance_km = great_circle_distance_km


def median_float(values: list[float]) -> float:
    if not values:
        _fail("median requires at least one value")
    ordered = sorted(float(value) for value in values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        result = ordered[mid]
    else:
        # 分开减半，避免两个同号极大有限数相加时不必要地溢出。
        result = ordered[mid - 1] / 2.0 + ordered[mid] / 2.0
    if not math.isfinite(result):
        _fail("computed median is not finite")
    return result


def distance_correction_at(distance: float,
                          corrections: list[dict[str, float]]) -> float | None:
    """分段线性插值；域为闭区间，不向外推。"""
    first = corrections[0]
    last = corrections[-1]
    if distance < first["distance_km"] or distance > last["distance_km"]:
        return None
    for left, right in zip(corrections, corrections[1:]):
        if left["distance_km"] <= distance <= right["distance_km"]:
            span = right["distance_km"] - left["distance_km"]
            if span == 0.0:  # 距离唯一性已拒绝，理论不可达。
                return left["correction"]
            ratio = (distance - left["distance_km"]) / span
            value = (left["correction"]
                     + (right["correction"] - left["correction"]) * ratio)
            if not math.isfinite(value):
                _fail("interpolated distance correction is not finite")
            return value
    return None  # pragma: no cover - 区间检查已覆盖


def _empty_observation(row: Mapping[str, Any], phase: str) -> dict[str, Any]:
    return {
        "measurement_id": row["measurement_id"],
        "id": row["measurement_id"],
        "arrival_id": row["arrival_id"],
        "station": row["station"],
        "station_id": row["station"],
        "channel": row["channel"],
        "phase": phase,
        "sample_start": row["sample_start"],
        "sample_end": row["sample_end"],
        "sample_interval": [row["sample_start"], row["sample_end"]],
        "interval": [row["sample_start"], row["sample_end"]],
        "peak_to_peak_counts": row["peak_to_peak_counts"],
        "counts": row["peak_to_peak_counts"],
        "status": None,
        "calibration_status": None,
        "reason_code": None,
        "epoch_id": None,
        "instrument_epoch_id": None,
        "calibration_epoch_id": None,
        "counts_per_mm": None,
        "gain_counts_per_mm": None,
        "gain": None,
        "amplitude_mm": None,
        "calibrated_amplitude_mm": None,
        "distance_km": None,
        "distance_correction": None,
        "correction": None,
        "single_magnitude": None,
        "magnitude": None,
        "used_for_station_magnitude": False,
    }


def _set_observation_status(record: dict[str, Any], status: str,
                            *, calibration_status: str | None = None) -> None:
    record["status"] = status
    record["calibration_status"] = calibration_status or status
    record["reason_code"] = status


def estimate_local_magnitudes(
    process_result: Mapping[str, Any],
    location_report: Mapping[str, Any],
    station_coordinates: Any,
    amplitude_observations: Any,
    instrument_epochs: Any,
    distance_corrections: Any,
    min_stations: int = 3,
) -> dict[str, Any]:
    """按本批事件、位置诊断和台站振幅估计局部震级。

    全部输入错误均以 :class:`ValueError`（实际子类
    :class:`MagnitudeInputError`）整批拒绝。缺校准、位置不可用或有效站不足
    不是输入错误，而会写入稳定的状态码。
    """
    prepared = prepare_magnitude_inputs(
        process_result, location_report, station_coordinates,
        amplitude_observations, instrument_epochs, distance_corrections,
        min_stations,
    )
    arrivals = prepared["arrivals"]
    coords = prepared["coordinates"]
    locations = prepared["locations"]
    epochs = prepared["epochs"]
    correction_table = prepared["corrections"]

    epochs_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for epoch in epochs:
        epochs_by_key.setdefault((epoch["station"], epoch["channel"]), []).append(epoch)

    observations_by_event: dict[str, list[dict[str, Any]]] = {
        event["event_id"]: [] for event in prepared["process"]["events"]
    }

    for source in prepared["observations"]:
        arrival = arrivals[source["arrival_id"]]
        event_id = arrival["event_id"]
        phase = arrival["phase"]
        record = _empty_observation(source, phase)

        if phase != "S":
            _set_observation_status(record, UNSUPPORTED_PHASE,
                                    calibration_status="not_attempted")
            observations_by_event[event_id].append(record)
            continue

        key = (source["station"], source["channel"])
        matching_epochs = epochs_by_key.get(key, [])
        covering = [
            epoch for epoch in matching_epochs
            if (epoch["sample_start"] <= source["sample_start"]
                and source["sample_end"] <= epoch["sample_end"])
        ]
        if not covering:
            intersects = any(
                epoch["sample_start"] < source["sample_end"]
                and source["sample_start"] < epoch["sample_end"]
                for epoch in matching_epochs
            )
            status = (CALIBRATION_BOUNDARY if intersects
                      else NO_CALIBRATION)
            _set_observation_status(record, status)
            observations_by_event[event_id].append(record)
            continue

        # 同站同通道有效期禁止重叠，因此完全覆盖段至多一个。
        epoch = covering[0]
        gain = float(epoch["counts_per_mm"])
        counts = float(source["peak_to_peak_counts"])
        amplitude = counts / gain
        if not math.isfinite(amplitude) or amplitude <= 0.0:
            _fail(
                f"measurement {source['measurement_id']} calibrated amplitude "
                "is not a finite positive number"
            )
        record.update({
            "epoch_id": epoch["epoch_id"],
            "instrument_epoch_id": epoch["epoch_id"],
            "calibration_epoch_id": epoch["epoch_id"],
            "counts_per_mm": epoch["counts_per_mm"],
            "gain_counts_per_mm": epoch["counts_per_mm"],
            "gain": epoch["counts_per_mm"],
            "amplitude_mm": amplitude,
            "calibrated_amplitude_mm": amplitude,
        })
        record["calibration_status"] = "calibrated"

        location = locations[event_id]
        if location["status"] != LOCATION_USABLE:
            _set_observation_status(record, LOCATION_NOT_USABLE,
                                    calibration_status="calibrated")
            observations_by_event[event_id].append(record)
            continue

        point = location["best_candidate"]
        station_point = coords[source["station"]]
        distance = great_circle_distance_km(
            point["lat"], point["lon"],
            station_point["lat"], station_point["lon"],
        )
        record["distance_km"] = distance
        correction = distance_correction_at(distance, correction_table)
        if correction is None:
            _set_observation_status(record, DISTANCE_OUT_OF_RANGE,
                                    calibration_status="calibrated")
            observations_by_event[event_id].append(record)
            continue

        single_magnitude = math.log10(amplitude) + correction
        if not math.isfinite(single_magnitude):
            _fail(
                f"measurement {source['measurement_id']} magnitude is not finite"
            )
        record["distance_correction"] = correction
        record["correction"] = correction
        record["single_magnitude"] = single_magnitude
        record["magnitude"] = single_magnitude
        record["status"] = USABLE
        record["calibration_status"] = "calibrated"
        record["reason_code"] = None
        record["used_for_station_magnitude"] = True
        observations_by_event[event_id].append(record)

    event_results: list[dict[str, Any]] = []
    for event in prepared["process"]["events"]:
        event_id = event["event_id"]
        location = locations[event_id]
        obs_records = observations_by_event[event_id]

        # 同时提供嵌套复核视图；规范扁平字段保持不变。
        for record in obs_records:
            record["calibration"] = {
                "status": record["calibration_status"],
                "calibration_status": record["calibration_status"],
                "epoch_id": record["epoch_id"],
                "instrument_epoch_id": record["instrument_epoch_id"],
                "counts_per_mm": record["counts_per_mm"],
                "gain": record["gain"],
                "gain_counts_per_mm": record["gain_counts_per_mm"],
                "amplitude_mm": record["amplitude_mm"],
                "calibrated_amplitude_mm": record["calibrated_amplitude_mm"],
            }
            record["distance"] = {
                "distance_km": record["distance_km"],
                "distance_correction": record["distance_correction"],
                "correction": record["correction"],
            }

        values_by_station: dict[str, list[tuple[float, str]]] = {}
        for record in obs_records:
            if record["status"] == USABLE and record["single_magnitude"] is not None:
                values_by_station.setdefault(record["station"], []).append(
                    (record["single_magnitude"], record["measurement_id"]))

        station_results: list[dict[str, Any]] = []
        station_medians: dict[str, float] = {}
        for station in sorted(values_by_station):
            pairs = values_by_station[station]
            magnitudes = [value for value, _ in pairs]
            median = median_float(magnitudes)
            ids = sorted(measurement_id for _, measurement_id in pairs)
            station_medians[station] = median
            station_results.append({
                "station": station,
                "station_id": station,
                "median_magnitude": median,
                "station_magnitude": median,
                "magnitude": median,
                "median": median,
                "value": median,
                "observation_ids": ids,
                "measurement_ids": ids,
                "participating_observation_ids": ids,
                "participating_measurement_ids": ids,
                "observation_count": len(ids),
            })

        station_values = [row["median_magnitude"] for row in station_results]
        location_status = location["status"]
        if location_status != LOCATION_USABLE:
            magnitude_status = LOCATION_NOT_USABLE
            event_magnitude: float | None = None
        elif len(station_results) < prepared["min_stations"]:
            magnitude_status = INSUFFICIENT_AMPLITUDE_STATIONS
            event_magnitude = None
        else:
            event_magnitude = median_float(station_values)
            magnitude_status = USABLE

        event_results.append({
            "event_id": event_id,
            "location_status": location_status,
            "magnitude_status": magnitude_status,
            "status": magnitude_status,
            "usable": magnitude_status == USABLE,
            "magnitude": event_magnitude,
            "local_magnitude": event_magnitude,
            "ml": event_magnitude,
            "station_count": len(station_results),
            "valid_station_count": len(station_results),
            "required_station_count": prepared["min_stations"],
            "station_ids": [row["station"] for row in station_results],
            "stations": station_results,
            "station_magnitudes": station_results,
            "station_medians": station_medians,
            "location": (
                None if location["best_candidate"] is None
                else dict(location["best_candidate"])
            ),
            "observations": obs_records,
            "amplitude_observations": obs_records,
            "reason_code": (None if magnitude_status == USABLE
                            else magnitude_status),
        })

    result = {
        "schema": SCHEMA,
        "min_stations": prepared["min_stations"],
        "events": event_results,
        "results": event_results,
        "event_results": event_results,
    }
    # 严格 JSON 可序列化，并拒绝溢出成 NaN/Infinity 的结果。
    return _json_copy(result)


def build_manifest(magnitude_result: Mapping[str, Any],
                   magnitude_threshold: int | float) -> list[dict[str, Any]]:
    """从震级结果构造固定阈值下游清单；不做任何舍入。"""
    threshold = _finite_number(magnitude_threshold, "magnitude_threshold")
    events = magnitude_result.get("events", [])
    if not isinstance(events, list):
        _fail("magnitude_result must contain an events list")
    rows: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        magnitude = event.get("magnitude")
        status = event.get("magnitude_status", event.get("status"))
        if (status == USABLE and type(magnitude) in (int, float)
                and not isinstance(magnitude, bool)
                and math.isfinite(float(magnitude))
                and float(magnitude) >= float(threshold)):
            rows.append({
                "event_id": event["event_id"],
                "magnitude": magnitude,
            })
    rows.sort(key=lambda row: row["event_id"])
    return rows


def make_cursor(catalog_id: str, version_id: str) -> dict[str, str]:
    return {
        "schema": CURSOR_SCHEMA,
        "catalog_id": catalog_id,
        "version_id": version_id,
    }


def _version_key(mapping: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return None


def build_change_package(catalog_id: str,
                         previous_version_id: str,
                         target_version_id: str,
                         previous_manifest: list[Mapping[str, Any]],
                         target_manifest: list[Mapping[str, Any]]
                         ) -> dict[str, Any]:
    """对照相邻两版清单，生成确定的增删变更包。"""
    previous = {row["event_id"]: row["magnitude"] for row in previous_manifest}
    target = {row["event_id"]: row["magnitude"] for row in target_manifest}

    upsert_ids: set[str] = set()
    for event_id, magnitude in target.items():
        if event_id not in previous or previous[event_id] != magnitude:
            upsert_ids.add(event_id)
    deletion_ids = sorted(set(previous) - set(target))
    upserts = [
        {"event_id": event_id, "magnitude": target[event_id]}
        for event_id in sorted(upsert_ids)
    ]
    deletions = deletion_ids
    cursor = make_cursor(catalog_id, target_version_id)
    package = {
        "schema": CHANGE_PACKAGE_SCHEMA,
        "catalog_id": catalog_id,
        "catalog_identity": catalog_id,
        "previous_version_id": previous_version_id,
        "from_version_id": previous_version_id,
        "base_version_id": previous_version_id,
        "target_version_id": target_version_id,
        "to_version_id": target_version_id,
        "version_id": target_version_id,
        "upserts": upserts,
        "upsert": upserts,
        "upserted": upserts,
        "updates": upserts,
        "added_or_updated": upserts,
        "records": upserts,
        "items": upserts,
        "deletions": deletions,
        "deletes": deletions,
        "delete": deletions,
        "deleted": deletions,
        "removed": deletions,
        "removed_event_ids": deletions,
        "deleted_ids": deletions,
        "delete_ids": deletions,
        "deleted_event_ids": deletions,
        "deletion_ids": deletions,
        "changes": {
            "upserts": upserts,
            "deletions": deletions,
            "upserted": upserts,
            "deleted": deletions,
        },
        "cursor": cursor,
        "new_cursor": cursor,
    }
    return _json_copy(package)


def _as_manifest(value: Any) -> list[dict[str, Any]] | None:
    if isinstance(value, list):
        rows = value
    elif isinstance(value, Mapping):
        for key in ("manifest", "events", "items", "records", "rows"):
            candidate = value.get(key)
            if isinstance(candidate, list):
                rows = candidate
                break
        else:
            return None
    else:
        return None
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping) or "event_id" not in row:
            raise ManifestDeltaError("manifest rows must contain event_id")
        magnitude = row.get("magnitude")
        if (type(magnitude) not in (int, float) or isinstance(magnitude, bool)
                or not math.isfinite(float(magnitude))):
            raise ManifestDeltaError("manifest row has an invalid magnitude")
        out.append({"event_id": row["event_id"], "magnitude": magnitude})
    return out


def _package_operations(package: Mapping[str, Any]
                         ) -> tuple[list[Mapping[str, Any]], list[Any]]:
    upserts = _version_key(
        package, "upserts", "upserted", "upsert", "updates",
        "added_or_updated", "records", "items")
    deletions = _version_key(
        package, "deletions", "deletes", "delete", "deleted",
        "removed", "removed_event_ids", "deleted_ids", "delete_ids",
        "deleted_event_ids", "deletion_ids")
    changes = package.get("changes")
    if (upserts is None or deletions is None) and isinstance(changes, Mapping):
        if upserts is None:
            upserts = _version_key(changes, "upserts", "upserted", "upsert")
        if deletions is None:
            deletions = _version_key(
                changes, "deletions", "deleted", "deletes", "delete")
    if upserts is None:
        upserts = []
    if deletions is None:
        deletions = []
    if not isinstance(upserts, list) or not isinstance(deletions, list):
        raise ManifestDeltaError("change package operations must be lists")
    return upserts, deletions


def _is_package(value: Any) -> bool:
    return (isinstance(value, Mapping)
            and (_version_key(value, "target_version_id", "version_id",
                              "to_version_id") is not None)
            and ("upserts" in value or "deletions" in value
                 or "upsert" in value or "delete" in value
                 or "changes" in value))


def _is_cursor(value: Any) -> bool:
    return (isinstance(value, Mapping)
            and _version_key(value, "catalog_id", "catalog_identity",
                             "directory_id") is not None
            and _version_key(value, "version_id", "version",
                             "cursor_version_id", "at_version_id") is not None
            and not _is_package(value))


class ManifestApplication(tuple):
    """``(new_manifest, new_cursor)`` 元组；也兼容字典/属性访问。"""

    def __new__(cls, manifest: list[dict[str, Any]],
                cursor: dict[str, str]):
        return super().__new__(cls, (manifest, cursor))

    @property
    def manifest(self) -> list[dict[str, Any]]:
        return tuple.__getitem__(self, 0)

    @property
    def new_manifest(self) -> list[dict[str, Any]]:
        return self.manifest

    @property
    def cursor(self) -> dict[str, str]:
        return tuple.__getitem__(self, 1)

    @property
    def new_cursor(self) -> dict[str, str]:
        return self.cursor

    def __getitem__(self, key: Any):
        if isinstance(key, str):
            if key in ("manifest", "new_manifest", "rows"):
                return self.manifest
            if key in ("cursor", "new_cursor"):
                return self.cursor
            raise KeyError(key)
        return tuple.__getitem__(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def as_dict(self) -> dict[str, Any]:
        return {"manifest": self.manifest, "cursor": self.cursor}


def apply_manifest_delta(manifest: Any = None,
                         cursor: Any = None,
                         change_package: Any = None,
                         *args: Any,
                         **kwargs: Any
                         ) -> ManifestApplication:
    """把相邻版本变更包完整应用到下游清单。

    规范位置参数为 ``(manifest, cursor, change_package)``；同时容忍
    ``(change_package, manifest, cursor)`` 等常见参数顺序。返回
    ``(new_manifest, new_cursor)``，不修改任何入参。
    """
    candidates: list[Any] = [manifest, cursor, change_package, *args]
    for key in ("manifest", "downstream_manifest", "rows",
                "cursor", "state_cursor",
                "change_package", "package", "delta"):
        if key in kwargs:
            candidates.append(kwargs[key])

    package = next((value for value in candidates if _is_package(value)), None)
    old_cursor = next((value for value in candidates if _is_cursor(value)), None)
    old_manifest = None
    for value in candidates:
        if _is_package(value):
            continue
        parsed = _as_manifest(value) if value is not None else None
        if parsed is not None:
            old_manifest = parsed
            break

    if package is None:
        raise ManifestDeltaError("a valid change package is required")
    if old_cursor is None:
        raise ManifestDeltaError("a catalog/version-bound cursor is required")
    if old_manifest is None:
        raise ManifestDeltaError("a valid downstream manifest is required")

    catalog_id = _version_key(package, "catalog_id", "catalog_identity",
                              "directory_id")
    cursor_catalog_id = _version_key(
        old_cursor, "catalog_id", "catalog_identity", "directory_id")
    if catalog_id != cursor_catalog_id:
        raise ManifestDeltaError(
            "change package belongs to a different magnitude catalog")

    previous_id = _version_key(
        package, "previous_version_id", "from_version_id",
        "base_version_id", "expected_previous_version_id")
    cursor_version = _version_key(
        old_cursor, "version_id", "version", "cursor_version_id",
        "at_version_id")
    if previous_id != cursor_version:
        raise ManifestDeltaError(
            "change package is out of order or already consumed: "
            f"package expects {previous_id!r}, cursor is {cursor_version!r}"
        )

    target_id = _version_key(package, "target_version_id", "version_id",
                             "to_version_id")
    upserts, deletions = _package_operations(package)

    delete_ids: set[str] = set()
    for event_id in deletions:
        if not isinstance(event_id, str) or not event_id:
            raise ManifestDeltaError("deletion IDs must be nonempty strings")
        if event_id in delete_ids:
            raise ManifestDeltaError(f"duplicate deletion id: {event_id}")
        delete_ids.add(event_id)

    upsert_rows: dict[str, Any] = {}
    for row in upserts:
        if not isinstance(row, Mapping):
            raise ManifestDeltaError("upsert rows must be objects")
        event_id = row.get("event_id", row.get("id"))
        if not isinstance(event_id, str) or not event_id:
            raise ManifestDeltaError("upsert row requires event_id")
        magnitude = row.get("magnitude")
        if (type(magnitude) not in (int, float) or isinstance(magnitude, bool)
                or not math.isfinite(float(magnitude))):
            raise ManifestDeltaError(f"upsert {event_id} has invalid magnitude")
        if event_id in upsert_rows:
            raise ManifestDeltaError(f"duplicate upsert row: {event_id}")
        upsert_rows[event_id] = {
            "event_id": event_id,
            "magnitude": magnitude,
        }

    overlap = delete_ids & set(upsert_rows)
    if overlap:
        raise ManifestDeltaError(
            "events cannot be both upserted and deleted: "
            + ", ".join(sorted(overlap))
        )

    # 从深拷贝开始，保证调用方持有的清单不被修改。
    next_rows = {row["event_id"]: dict(row) for row in old_manifest}
    for event_id in delete_ids:
        next_rows.pop(event_id, None)
    next_rows.update({event_id: dict(row)
                      for event_id, row in upsert_rows.items()})
    new_manifest = [next_rows[event_id]
                     for event_id in sorted(next_rows)]
    new_cursor = make_cursor(str(catalog_id), str(target_id))
    new_manifest = _json_copy(new_manifest)
    new_cursor = _json_copy(make_cursor(str(catalog_id), str(target_id)))
    return ManifestApplication(new_manifest, new_cursor)


# 常见公开别名。
apply_change_package = apply_manifest_delta
apply_magnitude_delta = apply_manifest_delta
apply_magnitude_manifest_delta = apply_manifest_delta
apply_downstream_delta = apply_manifest_delta
apply_manifest_change = apply_manifest_delta
apply_manifest_package = apply_manifest_delta
apply_catalog_delta = apply_manifest_delta
apply_change_package_to_manifest = apply_manifest_delta
apply_delta = apply_manifest_delta
apply_manifest_increment = apply_manifest_delta
apply_increment = apply_manifest_delta
apply_package = apply_manifest_delta
apply_manifest_update = apply_manifest_delta
apply_update_package = apply_manifest_delta
apply_changes = apply_manifest_delta


_CATALOG_LAZY_NAMES = {
    "LocalMagnitudeCatalog",
    "MagnitudeCatalog",
    "MagnitudeDirectory",
    "MagnitudeCatalogError",
    "MagnitudeVersionConflictError",
    "MagnitudeConflictError",
    "UnknownMagnitudeVersionError",
    "UnknownChangePackageError",
    "create_magnitude_catalog",
    "open_magnitude_catalog",
    "create_local_magnitude_catalog",
    "open_local_magnitude_catalog",
    "create_magnitude_directory",
    "open_magnitude_directory",
}


def __getattr__(name: str) -> Any:
    """允许从纯计算模块路径导入目录名称，同时避免循环导入。"""
    if name in _CATALOG_LAZY_NAMES:
        from . import magnitude_catalog
        return getattr(magnitude_catalog, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
