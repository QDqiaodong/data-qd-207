"""本地、可关闭重开且支持幂等更正的局部震级目录。

持久化布局：

``catalog.json``
    建档后不变的目录身份、固定批次输入和首版完整依据。
``journal.jsonl``
    每笔更正先写一行 JSON，再写一行 ``COMMITTED``。重开时只重放带提交标记
    的记录；最后一笔未提交尾巴会被截断。
``LOCK``
    进程级单写者锁。

一笔已提交更正的完整版本依据、清单和相邻版变更包放在同一条日志记录中，
因此重开后“历史版本、当前指针、变更包”要么一起可见，要么一起不可见。
"""
from __future__ import annotations

import fcntl
import json
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .magnitude import (
    CHANGE_PACKAGE_SCHEMA,
    MagnitudeInputError,
    ManifestDeltaError,  # noqa: F401  为公开异常体系保留导出
    apply_manifest_delta,
    apply_change_package,
    apply_magnitude_delta,
    apply_delta,
    apply_manifest_increment,
    apply_package,
    build_change_package,
    build_manifest,
    estimate_local_magnitudes,
    location_report_to_contract,
    make_cursor,
    normalize_amplitude_observations,
    normalize_distance_corrections,
    normalize_instrument_epochs,
    normalize_location_report,
    normalize_min_stations,
    normalize_station_coordinates,
)

CATALOG_SCHEMA = "seismic-assoc.magnitude-catalog/v1"
VERSION_BASIS_SCHEMA = "seismic-assoc.magnitude-version-basis/v1"

_UNSET = object()


class MagnitudeCatalogError(ValueError):
    """震级目录状态、版本或持久化操作不合法。"""


class MagnitudeVersionConflictError(MagnitudeCatalogError):
    """过期前版、冲突版本 ID 或目录当前状态不接受该提交。"""


# 更短的异常命名别名。
MagnitudeConflictError = MagnitudeVersionConflictError
MagnitudeVersionError = MagnitudeVersionConflictError


class UnknownMagnitudeVersionError(MagnitudeCatalogError, KeyError):
    """读取不存在的震级版本。"""


class UnknownChangePackageError(MagnitudeCatalogError, KeyError):
    """读取不存在的相邻版本变更包（例如首版没有上一版包）。"""


class ManifestView(list):
    """全量清单列表；额外携带绑定目录和版本的游标。"""

    def __init__(self, rows: list[dict[str, Any]], cursor: dict[str, str]):
        super().__init__(rows)
        self.cursor = cursor

    def get_cursor(self) -> dict[str, str]:
        return dict(self.cursor)

    def as_dict(self) -> dict[str, Any]:
        return {
            "manifest": list(self),
            "cursor": dict(self.cursor),
        }


def _new_catalog_identity() -> str:
    return f"magnitude-catalog-{uuid.uuid4().hex}"


def _strict_json_copy(value: Any) -> Any:
    try:
        return json.loads(json.dumps(
            value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise MagnitudeInputError(f"input is not strict JSON data: {exc}") from exc


def _digest(payload: Any) -> str:
    import hashlib

    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False,
                  sort_keys=True, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _first_present(mapping: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return _UNSET


_FIELDS: dict[str, tuple[str, ...]] = {
    "process_result": ("process_result", "process", "process_output",
                       "process_batch", "process_result"),
    "location_report": ("location_report", "location_results",
                        "location_result", "locations", "location"),
    "station_coordinates": ("station_coordinates", "station_coords",
                            "coordinates", "coords"),
    "amplitude_observations": ("amplitude_observations", "amplitudes",
                               "observations", "amplitude_records"),
    "instrument_epochs": ("instrument_epochs", "epochs",
                          "instrument_tables", "calibration_epochs"),
    "distance_corrections": ("distance_corrections", "corrections",
                             "distance_correction_table", "correction_table"),
    "min_stations": ("min_stations", "minimum_stations"),
    "magnitude_threshold": ("magnitude_threshold", "threshold",
                            "minimum_magnitude"),
    "first_version_id": ("first_version_id", "initial_version_id",
                         "version_id"),
    "catalog_id": ("catalog_id", "catalog_identity", "directory_id",
                   "identity"),
}

_VERSION_FIELDS: dict[str, tuple[str, ...]] = {
    "new_version_id": ("new_version_id", "new_version", "version_id",
                       "version", "target_version_id",
                       "to_version_id", "id"),
    "expected_previous_version_id": (
        "expected_previous_version_id", "expected_previous_version",
        "expected_previous", "previous_version_id", "previous_version",
        "base_version_id", "from_version_id", "parent_version_id",
        "expected_previous_version"),
    "instrument_epochs": ("instrument_epochs", "epochs",
                          "instrument_epoch_table", "calibration_table"),
    "location_report": ("location_report", "location_results",
                        "location_result", "locations"),
}


def _merge_named_options(target: dict[str, Any], source: Mapping[str, Any],
                         fields: Mapping[str, tuple[str, ...]]) -> None:
    for canonical, aliases in fields.items():
        value = _first_present(source, aliases)
        if value is not _UNSET:
            target[canonical] = value


def _looks_like_fixed_config(value: Mapping[str, Any]) -> bool:
    keys = {
        "process_result", "process", "station_coordinates", "coordinates",
        "amplitude_observations", "distance_corrections",
        "magnitude_threshold", "threshold", "first_version",
        "instrument_epochs", "location_report",
    }
    return any(key in value for key in keys)


def _parse_create_arguments(path: str | os.PathLike[str],
                            args: tuple[Any, ...],
                            kwargs: dict[str, Any]) -> dict[str, Any]:
    options: dict[str, Any] = {}

    for container_name in ("spec", "config", "catalog_spec", "data", "catalog"):
        container = kwargs.pop(container_name, None)
        if isinstance(container, Mapping):
            _merge_named_options(options, container, _FIELDS)
            first_version = container.get("first_version")
            if isinstance(first_version, Mapping):
                version_options: dict[str, Any] = {}
                _merge_named_options(version_options, first_version, _FIELDS)
                options.update({k: v for k, v in version_options.items()
                                if k not in options})

    first_version_arg = kwargs.pop("first_version", None)
    if isinstance(first_version_arg, Mapping):
        version_options = {}
        _merge_named_options(version_options, first_version_arg, _FIELDS)
        options.update(version_options)

    # 位置参数同时兼容 create(path, config, first_version) 与完整顺序关键字形态。
    positional = list(args)
    if positional and isinstance(positional[0], Mapping) and (
            _looks_like_fixed_config(positional[0])
            or "process_result" in positional[0]):
        config = positional.pop(0)
        _merge_named_options(options, config, _FIELDS)
        fv = config.get("first_version")
        if isinstance(fv, Mapping):
            version_options = {}
            _merge_named_options(version_options, fv, _FIELDS)
            options.update({k: v for k, v in version_options.items()
                            if k not in options})
        if positional and isinstance(positional[0], Mapping):
            fv = positional.pop(0)
            version_options = {}
            _merge_named_options(version_options, fv, _FIELDS)
            options.update(version_options)
    elif positional:
        ordered = (
            "process_result", "location_report", "station_coordinates",
            "amplitude_observations", "instrument_epochs",
            "distance_corrections", "min_stations", "magnitude_threshold",
            "first_version_id", "catalog_id",
        )
        for name, value in zip(ordered, positional):
            options.setdefault(name, value)

    _merge_named_options(options, kwargs, _FIELDS)
    # 允许 threshold= 等别名仍留在 kwargs 中；上面已处理。
    return options


class LocalMagnitudeCatalog:
    """单写者本地震级目录；请用 create/open 函数构造。"""

    def __init__(self, path: str | os.PathLike[str]):
        self._path = Path(path)
        self._closed = True
        self._lock_fh = None
        self._journal = None
        self._catalog_id = ""
        self._config: dict[str, Any] = {}
        self._records: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._current: str | None = None
        self._durable_end = 0
        self._fault: str | None = None

    # ---------- 构造与持久化 ----------

    @classmethod
    def create(cls, path: str | os.PathLike[str], *args: Any,
               **kwargs: Any) -> "LocalMagnitudeCatalog":
        options = _parse_create_arguments(path, args, kwargs)
        return create_magnitude_catalog(path, **options)

    @classmethod
    def open(cls, path: str | os.PathLike[str]) -> "LocalMagnitudeCatalog":
        return open_magnitude_catalog(path)

    def _acquire_lock(self) -> None:
        assert self._lock_fh is None
        self._lock_fh = open(self._path / "LOCK", "a+b")
        try:
            fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._lock_fh.close()
            self._lock_fh = None
            raise MagnitudeCatalogError(
                "magnitude catalog is already open by another writer") from exc

    def _initialize_from_options(self, options: Mapping[str, Any]) -> None:
        required = (
            "process_result", "location_report", "station_coordinates",
            "amplitude_observations", "instrument_epochs",
            "distance_corrections", "magnitude_threshold",
            "first_version_id",
        )
        missing = [name for name in required if options.get(name) is None]
        if missing:
            raise MagnitudeInputError(
                "cannot create magnitude catalog; missing: "
                + ", ".join(missing))

        process_result = options["process_result"]
        coords = normalize_station_coordinates(options["station_coordinates"])

        from .magnitude import _normalize_process_result
        process = _normalize_process_result(process_result)
        event_ids = {event["event_id"] for event in process["events"]}

        locations = normalize_location_report(
            options["location_report"], event_ids)
        arrivals: dict[str, dict[str, str]] = {}
        for event in process["events"]:
            for arrival in event["arrivals"]:
                if arrival["station"] not in coords:
                    raise MagnitudeInputError(
                        f"arrival {arrival['id']} has no coordinates")
                arrivals[arrival["id"]] = {
                    **arrival, "event_id": event["event_id"]}
        observations = normalize_amplitude_observations(
            options["amplitude_observations"], arrivals)
        epochs = normalize_instrument_epochs(
            options["instrument_epochs"], coords)
        corrections = normalize_distance_corrections(
            options["distance_corrections"])
        min_stations = normalize_min_stations(
            options.get("min_stations", 3))
        threshold = options["magnitude_threshold"]
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise MagnitudeInputError("magnitude_threshold must be a finite number")
        try:
            threshold_float = float(threshold)
        except OverflowError as exc:
            raise MagnitudeInputError(
                "magnitude_threshold is too large to represent as finite"
            ) from exc
        import math
        if not math.isfinite(threshold_float):
            raise MagnitudeInputError("magnitude_threshold must be finite")

        first_version_id = options["first_version_id"]
        if not isinstance(first_version_id, str) or not first_version_id:
            raise MagnitudeInputError("first_version_id must be a nonempty string")
        catalog_id = options.get("catalog_id") or _new_catalog_identity()
        if not isinstance(catalog_id, str) or not catalog_id:
            raise MagnitudeInputError("catalog_id must be a nonempty string")

        canonical_location = location_report_to_contract(locations)
        # 保留调用方提交的完整位置诊断供复核；计算和幂等指纹只消费规范字段。
        raw_location_report = _strict_json_copy(options["location_report"])
        magnitudes = estimate_local_magnitudes(
            process_result, canonical_location, coords, observations,
            epochs, corrections, min_stations,
        )
        manifest = build_manifest(magnitudes, threshold_float)

        self._catalog_id = catalog_id
        self._config = {
            "process_result": _strict_json_copy(process_result),
            "station_coordinates": coords,
            "amplitude_observations": observations,
            "distance_corrections": corrections,
            "min_stations": min_stations,
            "magnitude_threshold": threshold_float,
        }
        first_record = {
            "sequence": 0,
            "version_id": first_version_id,
            "previous_version_id": None,
            "provided": {
                "instrument_epochs": True,
                "location_report": True,
            },
            "request_digest": None,
            "instrument_epochs": epochs,
            "location_report": raw_location_report,
            "canonical_location_report": canonical_location,
            "magnitudes": magnitudes,
            "manifest": manifest,
            "change_package": None,
        }
        self._records = {first_version_id: first_record}
        self._order = [first_version_id]
        self._current = first_version_id

        catalog_payload = {
            "schema": CATALOG_SCHEMA,
            "catalog_id": catalog_id,
            "config": self._config,
            "first_version": first_record,
        }
        _write_json_atomic(self._path / "catalog.json", catalog_payload)
        self._journal = open(self._path / "journal.jsonl", "a+b")
        self._closed = False

    def _load_existing(self) -> None:
        catalog_file = self._path / "catalog.json"
        if not self._path.is_dir() or not catalog_file.is_file():
            raise MagnitudeCatalogError(
                f"not a magnitude catalog directory: {self._path}")
        with open(catalog_file, encoding="utf-8") as handle:
            payload = json.load(handle)
        if payload.get("schema") != CATALOG_SCHEMA:
            raise MagnitudeCatalogError("unsupported magnitude catalog schema")
        self._catalog_id = payload["catalog_id"]
        self._config = payload["config"]
        first = payload["first_version"]
        self._records = {first["version_id"]: first}
        self._order = [first["version_id"]]
        self._current = first["version_id"]
        self._journal = open(self._path / "journal.jsonl", "a+b")
        self._recover_journal()
        self._closed = False

    def _recover_journal(self) -> None:
        assert self._journal is not None
        self._journal.seek(0)
        data = self._journal.read()
        text = data.decode("utf-8", errors="strict")
        entries: list[list[Any]] = []
        for line in text.split("\n"):
            if not line:
                continue
            if line == "COMMITTED":
                if entries:
                    entries[-1][1] = True
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                obj = None
            entries.append([obj, False])

        # 最后一条没有 COMMITTED：生效前中断，整笔更正不可见。
        if entries and not entries[-1][1]:
            entries = entries[:-1]
        for obj, committed in entries:
            if (committed and isinstance(obj, Mapping)
                    and obj.get("type") == "magnitude_correction"):
                self._apply_record(obj["record"])

        marker = b"COMMITTED\n"
        marker_at = data.rfind(marker)
        durable_end = marker_at + len(marker) if marker_at >= 0 else 0
        if durable_end < len(data):
            self._journal.seek(durable_end)
            self._journal.truncate()
            self._journal.flush()
            os.fsync(self._journal.fileno())
        self._journal.seek(0, os.SEEK_END)
        self._durable_end = self._journal.tell()

    def _apply_record(self, record: Mapping[str, Any]) -> None:
        version_id = record["version_id"]
        if version_id not in self._records:
            self._order.append(version_id)
        converted = _strict_json_copy(record)
        self._records[version_id] = converted
        self._current = version_id

    def _run_fault(self, point: str) -> None:
        fault = self._fault
        if callable(fault):
            fault(point)
            return
        before_names = {"crash_before_commit", "before_commit"}
        after_names = {"crash_after_commit", "after_commit"}
        if point == "before_commit" and fault in before_names:
            self._fault = None
            raise RuntimeError(
                "injected magnitude catalog interruption before commit")
        if point == "after_commit" and fault in after_names:
            self._fault = None
            raise RuntimeError(
                "injected magnitude catalog response loss after commit")

    def _commit_record(self, record: dict[str, Any]) -> None:
        assert self._journal is not None
        # 同一实例在注入的提交前错误后重试时，先物理丢弃未提交尾巴。
        self._journal.seek(self._durable_end)
        self._journal.truncate()
        envelope = {
            "type": "magnitude_correction",
            "schema": "seismic-assoc.magnitude-correction-journal/v1",
            "record": record,
        }
        line = (json.dumps(
            envelope, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        self._journal.write(line)
        self._journal.flush()
        os.fsync(self._journal.fileno())
        self._run_fault("before_commit")

        self._journal.write(b"COMMITTED\n")
        self._journal.flush()
        os.fsync(self._journal.fileno())
        self._journal.seek(0, os.SEEK_END)
        self._durable_end = self._journal.tell()

        # 对调用方而言，此后目录状态已经生效；after_commit 故障只模拟应答丢失。
        self._apply_record(record)
        self._run_fault("after_commit")

    def _check_open(self) -> None:
        if self._closed:
            raise MagnitudeCatalogError("magnitude catalog is closed")

    def close(self) -> None:
        if self._closed:
            return
        try:
            if self._journal is not None:
                self._journal.flush()
                os.fsync(self._journal.fileno())
                self._journal.close()
                self._journal = None
        finally:
            if self._lock_fh is not None:
                try:
                    fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_UN)
                finally:
                    self._lock_fh.close()
                    self._lock_fh = None
            self._closed = True

    def __enter__(self) -> "LocalMagnitudeCatalog":
        self._check_open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    # ---------- 版本读取 ----------

    @property
    def catalog_id(self) -> str:
        return self._catalog_id

    @property
    def path(self) -> Path:
        return self._path

    def current_version(self) -> str:
        self._check_open()
        assert self._current is not None
        return self._current

    def latest_version(self) -> str:
        return self.current_version()

    def list_versions(self) -> list[str]:
        self._check_open()
        return list(self._order)

    def _resolve_version(self, version_id: str | None) -> str:
        self._check_open()
        if version_id is None:
            return self._current
        if isinstance(version_id, str) and version_id in self._records:
            return version_id
        # 常见“读当前版”别名；若确有名为 current/latest 的真实版本，上面已优先。
        if version_id in ("current", "latest", "HEAD"):
            return self._current
        raise UnknownMagnitudeVersionError(
            f"unknown magnitude version: {version_id!r}")

    def read_version(self, version_id: str | None = None) -> dict[str, Any]:
        """读取指定版（默认当前版）的完整震级依据和固定批次。"""
        resolved = self._resolve_version(version_id)
        record = self._records[resolved]
        result = {
            "schema": VERSION_BASIS_SCHEMA,
            "catalog_id": self._catalog_id,
            "version_id": resolved,
            "previous_version_id": record["previous_version_id"],
            "sequence": record["sequence"],
            "provided": _strict_json_copy(record["provided"]),
            "process_result": _strict_json_copy(self._config["process_result"]),
            "station_coordinates": _strict_json_copy(
                self._config["station_coordinates"]),
            "amplitude_observations": _strict_json_copy(
                self._config["amplitude_observations"]),
            "distance_corrections": _strict_json_copy(
                self._config["distance_corrections"]),
            "min_stations": self._config["min_stations"],
            "magnitude_threshold": self._config["magnitude_threshold"],
            "instrument_epochs": _strict_json_copy(
                record["instrument_epochs"]),
            "location_report": _strict_json_copy(record["location_report"]),
            "magnitudes": _strict_json_copy(record["magnitudes"]),
            "magnitude_result": _strict_json_copy(record["magnitudes"]),
            "result": _strict_json_copy(record["magnitudes"]),
            "manifest": _strict_json_copy(record["manifest"]),
            "cursor": make_cursor(self._catalog_id, resolved),
        }
        return result

    def read_magnitudes(self, version_id: str | None = None) -> dict[str, Any]:
        resolved = self._resolve_version(version_id)
        return _strict_json_copy(self._records[resolved]["magnitudes"])

    def get_magnitude_result(self, version_id: str | None = None
                             ) -> dict[str, Any]:
        return self.read_magnitudes(version_id)

    def read_basis(self, version_id: str | None = None) -> dict[str, Any]:
        return self.read_version(version_id)

    def get_manifest(self, version_id: str | None = None) -> ManifestView:
        """返回按事件 ID 排序的全量清单；返回列表额外带 ``.cursor``。"""
        resolved = self._resolve_version(version_id)
        rows = _strict_json_copy(self._records[resolved]["manifest"])
        return ManifestView(rows, make_cursor(self._catalog_id, resolved))

    def read_manifest(self, version_id: str | None = None) -> ManifestView:
        return self.get_manifest(version_id)

    # 简洁方法名别名。
    manifest = get_manifest

    def get_cursor(self, version_id: str | None = None) -> dict[str, str]:
        resolved = self._resolve_version(version_id)
        return make_cursor(self._catalog_id, resolved)

    cursor = get_cursor

    def access_manifest(self, version_id: str | None = None
                        ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """接入操作：返回 ``(manifest, cursor)``。"""
        manifest = self.get_manifest(version_id)
        return _strict_json_copy(list(manifest)), manifest.get_cursor()

    def get_manifest_and_cursor(self, version_id: str | None = None
                                ) -> dict[str, Any]:
        manifest = self.get_manifest(version_id)
        return manifest.as_dict()

    def get_manifest_cursor(self, version_id: str | None = None
                            ) -> dict[str, Any]:
        return self.get_manifest_and_cursor(version_id)

    # 接入操作的近义方法名。
    open_manifest = access_manifest
    manifest_and_cursor = get_manifest_and_cursor
    checkout = access_manifest

    def find_change_package(self, version_id: str) -> dict[str, Any] | None:
        """查询变更包；未知版本或首版没有上一版包时返回 ``None``。"""
        self._check_open()
        if not isinstance(version_id, str) or version_id not in self._records:
            return None
        package = self._records[version_id].get("change_package")
        return None if package is None else _strict_json_copy(package)

    def get_change_package(self, version_id: str) -> dict[str, Any] | None:
        """同 :meth:`find_change_package`；成功更正版返回包，首版返回 ``None``。"""
        return self.find_change_package(version_id)

    def read_change_package(self, version_id: str) -> dict[str, Any]:
        """严格读取变更包；未知版本或首版无上一版包时抛 ``ValueError``。"""
        self._check_open()
        if not isinstance(version_id, str) or version_id not in self._records:
            raise UnknownMagnitudeVersionError(
                f"unknown magnitude version: {version_id!r}")
        package = self._records[version_id].get("change_package")
        if package is None:
            raise UnknownChangePackageError(
                f"version {version_id!r} is the first version and has no "
                "previous-version change package")
        return _strict_json_copy(package)

    def get_delta(self, version_id: str) -> dict[str, Any] | None:
        return self.find_change_package(version_id)

    def read_delta(self, version_id: str) -> dict[str, Any]:
        return self.read_change_package(version_id)

    change_package = find_change_package
    delta = find_change_package

    # ---------- 更正提交 ----------

    def _parse_submission(self, args: tuple[Any, ...],
                          kwargs: dict[str, Any]) -> dict[str, Any]:
        options: dict[str, Any] = {}
        nested = kwargs.pop("correction", kwargs.pop("request", None))
        if not isinstance(nested, Mapping):
            nested = kwargs.pop("replacement", None)
        if not isinstance(nested, Mapping):
            tables = kwargs.get("tables", kwargs.get("replacement_tables"))
            if isinstance(tables, Mapping):
                nested = tables
        if isinstance(nested, Mapping):
            nested_options: dict[str, Any] = {}
            tables = nested.get("tables")
            if isinstance(tables, Mapping):
                nested = {**dict(nested), **dict(tables)}
            _merge_named_options(nested_options, nested, _VERSION_FIELDS)
            options.update(nested_options)

        positional = list(args)
        if positional and isinstance(positional[0], Mapping):
            payload = dict(positional.pop(0))
            tables = payload.pop("tables", None)
            if isinstance(tables, Mapping):
                table_options: dict[str, Any] = {}
                _merge_named_options(table_options, tables, _VERSION_FIELDS)
                for key, value in table_options.items():
                    payload.setdefault(key, value)
            _merge_named_options(options, payload, _VERSION_FIELDS)
        if positional:
            ordered = ("new_version_id", "expected_previous_version_id",
                       "instrument_epochs", "location_report")
            for name, value in zip(ordered, positional):
                options.setdefault(name, value)
        _merge_named_options(options, kwargs, _VERSION_FIELDS)
        return options

    def submit_correction(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """提交一版完整替代表（仪器有效期和/或位置结果）。

        成功返回目标版变更包形状的 dict，并额外提供
        ``change_package``/``package`` 嵌套键。相同新版本 ID 与相同规范请求
        的重试返回首次结果；同 ID 不同请求抛 :class:`ValueError` 子类。
        """
        self._check_open()
        options = self._parse_submission(args, kwargs)
        new_version_id = options.get("new_version_id")
        expected_previous = options.get("expected_previous_version_id")
        raw_epochs = options.get("instrument_epochs", _UNSET)
        raw_locations = options.get("location_report", _UNSET)

        if not isinstance(new_version_id, str) or not new_version_id:
            raise MagnitudeInputError("new_version_id must be a nonempty string")
        if not isinstance(expected_previous, str) or not expected_previous:
            raise MagnitudeInputError(
                "expected_previous_version_id must be a nonempty string")

        epochs_provided = raw_epochs is not _UNSET and raw_epochs is not None
        locations_provided = (raw_locations is not _UNSET
                              and raw_locations is not None)
        if not epochs_provided and not locations_provided:
            raise MagnitudeInputError(
                "correction must fully replace instrument_epochs and/or "
                "location_report")

        event_ids = {
            event["event_id"]
            for event in self._config["process_result"]["events"]
        }
        # 规范化同时完成整批结构校验；乱序等价表会得到相同规范结构。
        canonical_epochs = (
            normalize_instrument_epochs(
                raw_epochs, self._config["station_coordinates"])
            if epochs_provided else None
        )
        canonical_locations = (
            normalize_location_report(raw_locations, event_ids)
            if locations_provided else None
        )
        digest_epochs = (
            [{**row, "counts_per_mm": float(row["counts_per_mm"])}
             for row in canonical_epochs]
            if canonical_epochs is not None else None
        )
        request_fingerprint = _digest({
            "expected_previous_version_id": expected_previous,
            "provided_instrument_epochs": epochs_provided,
            "provided_location_report": locations_provided,
            "instrument_epochs": digest_epochs,
            "location_report": (
                location_report_to_contract(canonical_locations)
                if canonical_locations is not None else None),
        })

        # 幂等 ID 优先：即使目录已有后续版本，也不能把旧 ID 重新校验为过期。
        if new_version_id in self._records:
            old_record = self._records[new_version_id]
            if old_record.get("request_digest") == request_fingerprint:
                return self._correction_response(old_record)
            raise MagnitudeVersionConflictError(
                f"version id {new_version_id!r} was already used by a "
                "different correction request")

        if expected_previous != self._current:
            raise MagnitudeVersionConflictError(
                "expected_previous_version_id does not match current version "
                f"{self._current!r}; got {expected_previous!r}")
        previous_record = self._records[expected_previous]

        final_epochs = (canonical_epochs if epochs_provided
                        else previous_record["instrument_epochs"])
        final_canonical_locations = (
            location_report_to_contract(canonical_locations)
            if locations_provided
            else previous_record.get(
                "canonical_location_report",
                previous_record["location_report"])
        )
        stored_locations = (
            _strict_json_copy(raw_locations)
            if locations_provided
            else previous_record["location_report"]
        )

        # 只对合并后的最终输入判定震级；该调用承担最终整批数值有限性检查。
        magnitudes = estimate_local_magnitudes(
            self._config["process_result"],
            final_canonical_locations,
            self._config["station_coordinates"],
            self._config["amplitude_observations"],
            final_epochs,
            self._config["distance_corrections"],
            self._config["min_stations"],
        )
        manifest = build_manifest(
            magnitudes, self._config["magnitude_threshold"])
        package = build_change_package(
            self._catalog_id, expected_previous, new_version_id,
            previous_record["manifest"], manifest,
        )
        sequence = len(self._order)
        record = {
            "sequence": sequence,
            "version_id": new_version_id,
            "previous_version_id": expected_previous,
            "provided": {
                "instrument_epochs": epochs_provided,
                "location_report": locations_provided,
            },
            "request_digest": request_fingerprint,
            "instrument_epochs": final_epochs,
            "location_report": stored_locations,
            "canonical_location_report": final_canonical_locations,
            "magnitudes": magnitudes,
            "manifest": manifest,
            "change_package": package,
        }
        self._commit_record(record)
        return self._correction_response(self._records[new_version_id])

    def _correction_response(self, record: Mapping[str, Any]) -> dict[str, Any]:
        package = _strict_json_copy(record["change_package"])
        response = dict(package)
        response["accepted"] = True
        response["change_package"] = _strict_json_copy(package)
        response["package"] = _strict_json_copy(package)
        response["result"] = _strict_json_copy(package)
        return response

    # 更贴近不同调用习惯的方法别名。
    submit = submit_correction
    commit_correction = submit_correction
    apply_correction = submit_correction


def create_magnitude_catalog(path: str | os.PathLike[str] | None = None,
                             *args: Any, **kwargs: Any
                             ) -> LocalMagnitudeCatalog:
    """在新的本地路径创建震级目录。

    规范关键字示例见模块文档和 ``docs/magnitude-catalog.md``。也兼容
    ``create_magnitude_catalog(path, config, first_version)`` 的现有目录风格。
    """
    if path is None:
        path = kwargs.pop("path", kwargs.pop("directory", None))
    if path is None:
        raise MagnitudeCatalogError("catalog path is required")
    options = _parse_create_arguments(path, args, kwargs)
    target = Path(path)
    catalog = LocalMagnitudeCatalog(target)
    try:
        target.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise MagnitudeCatalogError(
            f"magnitude catalog path already exists: {target}") from exc
    try:
        catalog._acquire_lock()
        catalog._initialize_from_options(options)
    except BaseException:
        # 创建失败时释放可能已取得的锁；已建目录保留给调用方检查/清理，
        # 但不会包含已提交目录（catalog.json 可能尚未原子生成）。
        if catalog._lock_fh is not None:
            try:
                fcntl.flock(catalog._lock_fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                catalog._lock_fh.close()
            except OSError:
                pass
            catalog._lock_fh = None
        raise
    return catalog


def open_magnitude_catalog(path: str | os.PathLike[str]
                           ) -> LocalMagnitudeCatalog:
    """打开已存在的震级目录并重放已提交日志。"""
    target = Path(path)
    if not target.is_dir() or not (target / "catalog.json").is_file():
        raise MagnitudeCatalogError(
            f"not a magnitude catalog directory: {target}")
    catalog = LocalMagnitudeCatalog(target)
    catalog._acquire_lock()
    try:
        catalog._load_existing()
    except BaseException:
        if catalog._lock_fh is not None:
            try:
                fcntl.flock(catalog._lock_fh.fileno(), fcntl.LOCK_UN)
            finally:
                catalog._lock_fh.close()
                catalog._lock_fh = None
        raise
    return catalog


# 类名别名：任务描述称其为“本地可重开的震级目录”。
MagnitudeCatalog = LocalMagnitudeCatalog
LocalMagnitudeCatalogStore = LocalMagnitudeCatalog
MagnitudeCatalogStore = LocalMagnitudeCatalog
MagnitudeDirectory = LocalMagnitudeCatalog
LocalMagnitudeDirectory = LocalMagnitudeCatalog


def create_magnitude_directory(*args: Any, **kwargs: Any
                               ) -> LocalMagnitudeCatalog:
    """等价于 :func:`create_magnitude_catalog` 的目录命名别名。"""
    return create_magnitude_catalog(*args, **kwargs)


def open_magnitude_directory(*args: Any, **kwargs: Any
                            ) -> LocalMagnitudeCatalog:
    """等价于 :func:`open_magnitude_catalog` 的目录命名别名。"""
    return open_magnitude_catalog(*args, **kwargs)


def open_local_magnitude_catalog(*args: Any, **kwargs: Any
                                 ) -> LocalMagnitudeCatalog:
    return open_magnitude_catalog(*args, **kwargs)


def create_local_magnitude_catalog(*args: Any, **kwargs: Any
                                   ) -> LocalMagnitudeCatalog:
    return create_magnitude_catalog(*args, **kwargs)


create_local_magnitude_directory = create_magnitude_directory
open_local_magnitude_directory = open_magnitude_directory


def apply_package_to_manifest(*args: Any, **kwargs: Any):
    """等价于 :func:`seismic_assoc.magnitude.apply_manifest_delta`。"""
    return apply_manifest_delta(*args, **kwargs)
