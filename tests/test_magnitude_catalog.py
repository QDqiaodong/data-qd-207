"""本地震级目录、版本增量、下游接续与崩溃恢复验收。"""
from __future__ import annotations

import copy
import json
import random
import tempfile
import unittest
from pathlib import Path

from seismic_assoc import evaluate_location_candidates, process
from seismic_assoc.magnitude import apply_manifest_delta
from seismic_assoc.magnitude_catalog import (
    MagnitudeCatalogError,
    create_magnitude_catalog,
    open_magnitude_catalog,
)


def two_event_process():
    return process({
        "stations": [
            {"id": "A", "lat": "0", "lon": "0"},
            {"id": "B", "lat": "0", "lon": "0"},
            {"id": "C", "lat": "0", "lon": "0"},
        ],
        "window": "5",
        "events": [
            {"id": "aA", "station": "A", "phase": "S", "time": "0"},
            {"id": "aB", "station": "B", "phase": "S", "time": "1"},
            {"id": "aC", "station": "C", "phase": "S", "time": "2"},
            {"id": "dA", "station": "A", "phase": "S", "time": "100"},
            {"id": "dB", "station": "B", "phase": "S", "time": "101"},
            {"id": "dC", "station": "C", "phase": "S", "time": "102"},
        ],
    })


COORDS = [{"id": sid, "lat": 0.0, "lon": 0.0} for sid in ("A", "B", "C")]


def usable_location(process_result):
    return evaluate_location_candidates(
        process_result,
        COORDS,
        [{"candidate_id": "c0", "lat": 0, "lon": 0,
          "travel_times": {sid: {"S": "1"} for sid in "ABC"}}],
        {"residual_threshold": 100, "min_supporting_stations": 3},
    )


AMPLITUDES = [
    {"measurement_id": "mA1", "arrival_id": "aA", "station": "A",
     "channel": "HHZ", "sample_start": 0, "sample_end": 10,
     "peak_to_peak_counts": 10},
    {"measurement_id": "mA2", "arrival_id": "aA", "station": "A",
     "channel": "HHN", "sample_start": 0, "sample_end": 10,
     "peak_to_peak_counts": 100},
    {"measurement_id": "mB", "arrival_id": "aB", "station": "B",
     "channel": "HHZ", "sample_start": 0, "sample_end": 10,
     "peak_to_peak_counts": 1000},
    {"measurement_id": "mC", "arrival_id": "aC", "station": "C",
     "channel": "HHZ", "sample_start": 0, "sample_end": 10,
     "peak_to_peak_counts": 10000},
    {"measurement_id": "m2A", "arrival_id": "dA", "station": "A",
     "channel": "HHZ", "sample_start": 100, "sample_end": 110,
     "peak_to_peak_counts": 10000},
    {"measurement_id": "m2B", "arrival_id": "dB", "station": "B",
     "channel": "HHZ", "sample_start": 100, "sample_end": 110,
     "peak_to_peak_counts": 10000},
    {"measurement_id": "m2C", "arrival_id": "dC", "station": "C",
     "channel": "HHZ", "sample_start": 100, "sample_end": 110,
     "peak_to_peak_counts": 10000},
]

EPOCHS_V1 = [
    {"epoch_id": "epA1", "station": "A", "channel": "HHZ",
     "sample_start": 0, "sample_end": 20, "counts_per_mm": 10},
    {"epoch_id": "epA2", "station": "A", "channel": "HHN",
     "sample_start": 0, "sample_end": 20, "counts_per_mm": 1},
    {"epoch_id": "epB", "station": "B", "channel": "HHZ",
     "sample_start": 0, "sample_end": 20, "counts_per_mm": 1},
    {"epoch_id": "epC", "station": "C", "channel": "HHZ",
     "sample_start": 0, "sample_end": 20, "counts_per_mm": 1},
    {"epoch_id": "ep2A", "station": "A", "channel": "HHZ",
     "sample_start": 100, "sample_end": 120, "counts_per_mm": 1},
    {"epoch_id": "ep2B", "station": "B", "channel": "HHZ",
     "sample_start": 100, "sample_end": 120, "counts_per_mm": 1},
    {"epoch_id": "ep2C", "station": "C", "channel": "HHZ",
     "sample_start": 100, "sample_end": 120, "counts_per_mm": 1},
]

CORRECTIONS = [
    {"distance_km": 1000, "correction": 0.0},
    {"distance_km": 0, "correction": 0.0},
]


def epochs_v2_delete_first():
    rows = copy.deepcopy(EPOCHS_V1)
    next(row for row in rows if row["epoch_id"] == "epC")[
        "counts_per_mm"] = 10000
    return rows


def epochs_v3_rename_second_epoch():
    rows = epochs_v2_delete_first()
    next(row for row in rows if row["epoch_id"] == "ep2A")[
        "epoch_id"] = "ep2A-renamed"
    return rows


def epochs_v4_restore_first():
    rows = epochs_v3_rename_second_epoch()
    next(row for row in rows if row["epoch_id"] == "epC")[
        "counts_per_mm"] = 1
    return rows


def epochs_v5_change_value():
    rows = epochs_v4_restore_first()
    next(row for row in rows if row["epoch_id"] == "epB")[
        "counts_per_mm"] = 0.01
    return rows


class CatalogTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.process_result = two_event_process()
        self.location = usable_location(self.process_result)
        self.first_id = self.process_result["events"][0]["event_id"]
        self.second_id = self.process_result["events"][1]["event_id"]

    def catalog_kwargs(self):
        return dict(
            process_result=self.process_result,
            location_report=copy.deepcopy(self.location),
            station_coordinates=copy.deepcopy(COORDS),
            amplitude_observations=copy.deepcopy(AMPLITUDES),
            instrument_epochs=copy.deepcopy(EPOCHS_V1),
            distance_corrections=copy.deepcopy(CORRECTIONS),
            min_stations=3,
            magnitude_threshold=3,
            first_version_id="v1",
        )

    def create_catalog(self, name="magnitude-catalog"):
        return create_magnitude_catalog(self.root / name,
                                        **self.catalog_kwargs())

    def mixed_unresolved_location(self):
        report = copy.deepcopy(self.location)
        second = next(event for event in report["events"]
                      if event["event_id"] == self.second_id)
        second["status"] = "unresolved"
        second["best_candidate"] = None
        return report


class CatalogWorkflowTests(CatalogTestBase):
    def test_versioned_manifest_packages_and_downstream_apply(self):
        catalog = self.create_catalog()
        catalog_id = catalog.catalog_id
        self.assertTrue(catalog_id)

        v1_manifest = catalog.get_manifest("v1")
        self.assertEqual([row["event_id"] for row in v1_manifest],
                         [self.first_id, self.second_id])
        self.assertEqual(v1_manifest.cursor,
                         {"schema": "seismic-assoc.manifest-cursor/v1",
                          "catalog_id": catalog_id,
                          "version_id": "v1"})

        # v2：C 站换算为 1 mm，事件震级 1，离开阈值清单。
        response_v2 = catalog.submit_correction(
            "v2", "v1", instrument_epochs=epochs_v2_delete_first())
        package_v2 = catalog.get_change_package("v2")
        self.assertEqual(response_v2["change_package"], package_v2)
        self.assertIsNone(catalog.get_change_package("v1"))
        with self.assertRaises(ValueError):
            catalog.read_change_package("v1")
        self.assertEqual(package_v2["catalog_id"], catalog_id)
        self.assertEqual(package_v2["previous_version_id"], "v1")
        self.assertEqual(package_v2["target_version_id"], "v2")
        self.assertEqual(package_v2["deletions"], [self.first_id])
        self.assertEqual(package_v2["upserts"], [])

        # 旧版仍保留原增益、毫米振幅和单条震级。
        old_basis = catalog.read_version("v1")
        # 完整位置诊断（不参与震级身份比较的 summary/candidates 等）也保留供复核。
        self.assertIn("summary", old_basis["location_report"])
        old_c = next(row for row in old_basis["magnitudes"]["events"][0]
                     ["observations"] if row["measurement_id"] == "mC")
        self.assertEqual(old_c["counts_per_mm"], 1)
        self.assertEqual(old_c["amplitude_mm"], 10000.0)
        self.assertEqual(old_c["single_magnitude"], 4.0)

        v2_basis = catalog.read_version("v2")
        new_c = next(row for row in v2_basis["magnitudes"]["events"][0]
                     ["observations"] if row["measurement_id"] == "mC")
        self.assertEqual(new_c["counts_per_mm"], 10000)
        self.assertEqual(new_c["amplitude_mm"], 1.0)
        first_event_v2 = v2_basis["magnitudes"]["events"][0]
        self.assertEqual(first_event_v2["magnitude"], 1.0)

        # v3：只改另一事件的 epoch_id，不改增益；依据变化但清单行为空。
        package_v3 = catalog.submit_correction(
            "v3", "v2", instrument_epochs=epochs_v3_rename_second_epoch())
        self.assertEqual(package_v3["upserts"], [])
        self.assertEqual(package_v3["deletions"], [])
        self.assertEqual(package_v3["previous_version_id"], "v2")
        self.assertEqual(package_v3["target_version_id"], "v3")
        self.assertEqual(catalog.current_version(), "v3")
        v3_second_obs = next(
            row for row in catalog.read_magnitudes("v3")["events"][1]
            ["observations"] if row["measurement_id"] == "m2A")
        self.assertEqual(v3_second_obs["epoch_id"], "ep2A-renamed")
        self.assertEqual(v3_second_obs["amplitude_mm"], 10000.0)
        self.assertEqual(v3_second_obs["single_magnitude"], 4.0)

        # v4：第一个事件重新入选；第二个位置变为 unresolved 并退出清单。
        package_v4 = catalog.submit_correction(
            "v4", "v3",
            instrument_epochs=epochs_v4_restore_first(),
            location_report=self.mixed_unresolved_location(),
        )
        self.assertEqual(
            package_v4["upserts"],
            [{"event_id": self.first_id, "magnitude": 3.0}])
        self.assertEqual(package_v4["deletions"], [self.second_id])
        v4_basis = catalog.read_version("v4")
        second_event_v4 = next(
            event for event in v4_basis["magnitudes"]["events"]
            if event["event_id"] == self.second_id)
        self.assertEqual(second_event_v4["location_status"], "unresolved")
        self.assertEqual(
            second_event_v4["magnitude_status"], "location_not_usable")
        self.assertIsNone(second_event_v4["magnitude"])
        second_obs = next(
            row for row in second_event_v4["observations"]
            if row["measurement_id"] == "m2A")
        self.assertEqual(second_obs["status"], "location_not_usable")
        self.assertEqual(second_obs["amplitude_mm"], 10000.0)
        self.assertIsNotNone(second_obs["epoch_id"])
        self.assertIsNone(second_obs["distance_km"])
        self.assertIsNone(second_obs["single_magnitude"])

        # v5：阈值以上的数值变化必须整条替换。
        package_v5 = catalog.submit_correction(
            "v5", "v4", instrument_epochs=epochs_v5_change_value())
        self.assertEqual(
            package_v5["upserts"],
            [{"event_id": self.first_id, "magnitude": 4.0}])
        self.assertEqual(package_v5["deletions"], [])

        # 从 v1 接入并顺次应用 v2..v5，必须等于各版全量清单。
        manifest, cursor = catalog.access_manifest("v1")
        original_manifest = copy.deepcopy(manifest)
        original_cursor = copy.deepcopy(cursor)
        for version_id in ("v2", "v3", "v4", "v5"):
            package = catalog.get_change_package(version_id)
            manifest, cursor = apply_manifest_delta(
                manifest, cursor, package)
            self.assertEqual(manifest, catalog.get_manifest(version_id))
            self.assertEqual(cursor, catalog.get_cursor(version_id))

        # 纯函数不修改来参。
        self.assertEqual(manifest[-1]["magnitude"], 4.0)
        self.assertEqual(original_manifest, v1_manifest)
        self.assertEqual(original_cursor["version_id"], "v1")

        # 从任一历史版本 v2 接入，也能顺次到达 v5。
        manifest2, cursor2 = catalog.access_manifest("v2")
        for version_id in ("v3", "v4", "v5"):
            manifest2, cursor2 = apply_manifest_delta(
                manifest2, cursor2, catalog.get_change_package(version_id))
        self.assertEqual(manifest2, catalog.get_manifest("v5"))
        self.assertEqual(cursor2, catalog.get_cursor("v5"))

        catalog.close()

        # 关闭重开后所有历史、当前指针和包仍可读。
        reopened = open_magnitude_catalog(self.root / "magnitude-catalog")
        self.assertEqual(reopened.current_version(), "v5")
        self.assertEqual(reopened.list_versions(),
                         ["v1", "v2", "v3", "v4", "v5"])
        self.assertEqual(
            reopened.get_change_package("v2")["deletions"],
            [self.first_id])
        self.assertEqual(reopened.get_manifest("v5"), manifest)
        reopened.close()

    def test_out_of_order_consumed_and_cross_catalog_rejected(self):
        catalog = self.create_catalog("cat-a")
        catalog.submit_correction(
            "v2", "v1", instrument_epochs=epochs_v2_delete_first())
        catalog.submit_correction(
            "v3", "v2", instrument_epochs=epochs_v3_rename_second_epoch())

        other = self.create_catalog("cat-b")
        other.submit_correction(
            "v2", "v1", instrument_epochs=epochs_v2_delete_first())

        manifest, cursor = catalog.access_manifest("v1")
        package_v3 = catalog.get_change_package("v3")
        with self.assertRaises(ValueError):
            apply_manifest_delta(manifest, cursor, package_v3)

        package_v2 = catalog.get_change_package("v2")
        next_manifest, next_cursor = apply_manifest_delta(
            manifest, cursor, package_v2)
        with self.assertRaises(ValueError):
            apply_manifest_delta(next_manifest, next_cursor, package_v2)

        other_package = other.get_change_package("v2")
        with self.assertRaises(ValueError):
            apply_manifest_delta(manifest, cursor, other_package)

        # 失败不修改调用方状态。
        self.assertEqual(cursor["version_id"], "v1")
        self.assertEqual(len(manifest), 2)
        catalog.close()
        other.close()

    def test_idempotent_retry_returns_original_package_after_later_versions(self):
        catalog = self.create_catalog()
        original = catalog.submit_correction(
            "v2", "v1", instrument_epochs=epochs_v2_delete_first())
        catalog.submit_correction(
            "v3", "v2", instrument_epochs=epochs_v3_rename_second_epoch())

        shuffled_epochs = epochs_v2_delete_first()
        random.Random(207006).shuffle(shuffled_epochs)
        retry = catalog.submit_correction(
            "v2", "v1", instrument_epochs=shuffled_epochs)
        self.assertEqual(retry["change_package"],
                         original["change_package"])
        self.assertEqual(retry["target_version_id"], "v2")
        self.assertEqual(catalog.current_version(), "v3")

        different = epochs_v2_delete_first()
        next(row for row in different if row["epoch_id"] == "epC")[
            "counts_per_mm"] = 999
        with self.assertRaises(ValueError):
            catalog.submit_correction(
                "v2", "v1", instrument_epochs=different)
        self.assertEqual(catalog.current_version(), "v3")
        catalog.close()

    def test_invalid_stale_and_conflicting_corrections_do_not_advance(self):
        catalog = self.create_catalog()

        # 首次提交使用过期前版。
        with self.assertRaises(ValueError):
            catalog.submit_correction(
                "v2-stale", "v0",
                instrument_epochs=epochs_v2_delete_first())
        self.assertEqual(catalog.current_version(), "v1")
        self.assertEqual(catalog.list_versions(), ["v1"])

        # 整批非法：制造同站同通道重叠有效期。
        invalid_epochs = epochs_v2_delete_first()
        next(row for row in invalid_epochs
             if row["epoch_id"] == "epA2")["channel"] = "HHZ"
        with self.assertRaises(ValueError):
            catalog.submit_correction(
                "v-bad", "v1", instrument_epochs=invalid_epochs)
        self.assertEqual(catalog.current_version(), "v1")
        self.assertNotIn("v-bad", catalog.list_versions())
        self.assertIsNone(catalog.get_change_package("v-bad"))
        with self.assertRaises(ValueError):
            catalog.read_change_package("v-bad")

        # 冲突 ID：第一次合法，同 ID 不同请求必须拒绝且不能覆盖原包。
        first_response = catalog.submit_correction(
            "v2", "v1", instrument_epochs=epochs_v2_delete_first())
        conflicting = epochs_v2_delete_first()
        next(row for row in conflicting if row["epoch_id"] == "epB")[
            "counts_per_mm"] = 0.5
        with self.assertRaises(ValueError):
            catalog.submit_correction(
                "v2", "v1", instrument_epochs=conflicting)
        self.assertEqual(catalog.current_version(), "v2")
        self.assertEqual(
            catalog.get_change_package("v2"),
            first_response["change_package"])
        catalog.close()

    def test_single_writer_lock_and_reopen(self):
        catalog = self.create_catalog("locked")
        with self.assertRaises(MagnitudeCatalogError):
            open_magnitude_catalog(self.root / "locked")
        catalog.close()
        reopened = open_magnitude_catalog(self.root / "locked")
        self.assertEqual(reopened.current_version(), "v1")
        reopened.close()


class CrashRecoveryTests(CatalogTestBase):
    """两个故障注入点：

    * ``_fault = "crash_before_commit"``：更正 JSON 行已写入并 fsync，
      但尚未写 ``COMMITTED``。重开恢复时截断未提交尾巴，因此没有新版本、
      当前版推进或变更包，失败版本 ID 仍可使用。
    * ``_fault = "crash_after_commit"``：``COMMITTED`` 已 fsync，目录结果
      已生效，但返回应答前抛出异常，模拟调用方没收到响应。重开后可幂等取回。
    """

    def test_interruption_before_commit_leaves_no_version_or_package(self):
        path = self.root / "before-commit"
        catalog = create_magnitude_catalog(path, **self.catalog_kwargs())
        catalog._fault = "crash_before_commit"
        with self.assertRaises(RuntimeError):
            catalog.submit_correction(
                "v2", "v1", instrument_epochs=epochs_v2_delete_first())
        catalog.close()

        reopened = open_magnitude_catalog(path)
        self.assertEqual(reopened.current_version(), "v1")
        self.assertEqual(reopened.list_versions(), ["v1"])
        self.assertIsNone(reopened.get_change_package("v2"))
        with self.assertRaises(ValueError):
            reopened.read_change_package("v2")
        reopened.close()

        # 恢复时物理截断未提交 JSON 尾巴。
        self.assertNotIn("v2", (path / "journal.jsonl").read_text("utf-8"))

        # 失败 ID 未被占用：重开后同一 v2 可以首次成功提交。
        recovered = open_magnitude_catalog(path)
        package = recovered.submit_correction(
            "v2", "v1", instrument_epochs=epochs_v2_delete_first())
        self.assertEqual(package["deletions"], [self.first_id])
        recovered.close()

    def test_committed_but_response_lost_retry_fetches_original_package(self):
        path = self.root / "after-commit"
        catalog = create_magnitude_catalog(path, **self.catalog_kwargs())
        original_epochs = epochs_v2_delete_first()
        catalog._fault = "crash_after_commit"
        with self.assertRaises(RuntimeError):
            catalog.submit_correction(
                "v2", "v1", instrument_epochs=original_epochs)
        catalog.close()

        reopened = open_magnitude_catalog(path)
        self.assertEqual(reopened.current_version(), "v2")
        original_package = reopened.get_change_package("v2")
        self.assertEqual(original_package["deletions"], [self.first_id])

        # 重开后先成功提交下一版。
        v3_response = reopened.submit_correction(
            "v3", "v2",
            instrument_epochs=epochs_v3_rename_second_epoch())
        self.assertEqual(v3_response["target_version_id"], "v3")
        self.assertEqual(reopened.current_version(), "v3")

        # 再用顺序打乱但规范等价的 v2 表重试旧 ID。
        shuffled = epochs_v2_delete_first()
        random.Random(99).shuffle(shuffled)
        retry_response = reopened.submit_correction(
            "v2", "v1", instrument_epochs=shuffled)
        self.assertEqual(retry_response["change_package"], original_package)
        self.assertEqual(retry_response["deletions"], [self.first_id])
        self.assertEqual(reopened.current_version(), "v3")
        self.assertEqual(reopened.list_versions(), ["v1", "v2", "v3"])
        reopened.close()


if __name__ == "__main__":
    unittest.main()
