"""局部震级纯计算契约的验收与边界测试。"""
from __future__ import annotations

import copy
import json
import math
import random
import unittest

from seismic_assoc import evaluate_location_candidates, process
from seismic_assoc.magnitude import (
    estimate_local_magnitudes,
    great_circle_distance_km,
    median_float,
)


def station_input(station_ids=("A", "B", "C"), lat=0, lon=0):
    return [{"id": sid, "lat": lat, "lon": lon} for sid in station_ids]


def decimal_station_input(station_ids=("A", "B", "C")):
    # 现有 process() 的严格输入口径要求十进制字符串。
    return [{"id": sid, "lat": "0", "lon": "0"} for sid in station_ids]


def acceptance_process():
    return process({
        "stations": decimal_station_input(),
        "window": "10",
        "events": [
            {"id": "aA", "station": "A", "phase": "S", "time": "0"},
            {"id": "aB", "station": "B", "phase": "S", "time": "1"},
            {"id": "aC", "station": "C", "phase": "S", "time": "2"},
        ],
    })


def usable_location_report(process_result, station_coordinates):
    candidates = [{
        "candidate_id": "c0",
        "lat": 0,
        "lon": 0,
        "travel_times": {
            sid: {"P": "1", "S": "1"}
            for sid in ("A", "B", "C")
        },
    }]
    return evaluate_location_candidates(
        process_result,
        station_coordinates,
        candidates,
        {"residual_threshold": 100, "min_supporting_stations": 3},
    )


def acceptance_amplitudes():
    return [
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
    ]


def acceptance_epochs():
    return [
        {"epoch_id": "epA1", "station": "A", "channel": "HHZ",
         "sample_start": 0, "sample_end": 20, "counts_per_mm": 10},
        {"epoch_id": "epA2", "station": "A", "channel": "HHN",
         "sample_start": 0, "sample_end": 20, "counts_per_mm": 1},
        {"epoch_id": "epB", "station": "B", "channel": "HHZ",
         "sample_start": 0, "sample_end": 20, "counts_per_mm": 1},
        {"epoch_id": "epC", "station": "C", "channel": "HHZ",
         "sample_start": 0, "sample_end": 20, "counts_per_mm": 1},
    ]


def zero_corrections():
    return [
        {"distance_km": 1000, "correction": 0},
        {"distance_km": 0, "correction": 0},
    ]


class AcceptanceMagnitudeTests(unittest.TestCase):
    def setUp(self):
        self.process_result = acceptance_process()
        self.coords = station_input()
        self.location = usable_location_report(
            self.process_result, self.coords)
        self.amps = acceptance_amplitudes()
        self.epochs = acceptance_epochs()
        self.corrections = zero_corrections()

    def estimate(self, **overrides):
        kwargs = dict(
            process_result=self.process_result,
            location_report=self.location,
            station_coordinates=self.coords,
            amplitude_observations=self.amps,
            instrument_epochs=self.epochs,
            distance_corrections=self.corrections,
            min_stations=3,
        )
        kwargs.update(overrides)
        return estimate_local_magnitudes(**kwargs)

    def test_station_median_precedes_event_median(self):
        result = self.estimate()
        event = result["events"][0]
        self.assertEqual(event["event_id"], "e1")
        self.assertEqual(event["location_status"], "usable")
        self.assertEqual(event["magnitude_status"], "usable")
        self.assertEqual(event["status"], "usable")
        self.assertEqual(event["magnitude"], 3.0)

        by_station = {row["station"]: row for row in event["stations"]}
        self.assertEqual(by_station["A"]["median_magnitude"], 1.0)
        self.assertEqual(
            by_station["A"]["participating_observation_ids"],
            ["mA1", "mA2"],
        )
        self.assertEqual(by_station["B"]["median_magnitude"], 3.0)
        self.assertEqual(by_station["C"]["median_magnitude"], 4.0)

        # 直接对四条单条震级取中位数会得到 2.5，而不是事件震级 3。
        self.assertEqual(median_float([0.0, 2.0, 3.0, 4.0]), 2.5)
        with self.assertRaises(AssertionError):
            self.assertEqual(event["magnitude"], 2.5)

        # 严格 JSON 可序列化，且没有 NaN/Infinity。
        json.dumps(result, allow_nan=False)

    def test_even_medians_average_middle_values(self):
        self.assertEqual(median_float([4.0, 1.0, 3.0, 2.0]), 2.5)

    def test_observation_keeps_original_evidence_and_gain(self):
        result = self.estimate()
        obs = {row["measurement_id"]: row
               for row in result["events"][0]["observations"]}
        a1 = obs["mA1"]
        self.assertEqual(a1["station"], "A")
        self.assertEqual(a1["channel"], "HHZ")
        self.assertEqual(a1["phase"], "S")
        self.assertEqual(a1["sample_start"], 0)
        self.assertEqual(a1["sample_end"], 10)
        self.assertEqual(a1["sample_interval"], [0, 10])
        self.assertEqual(a1["peak_to_peak_counts"], 10)
        self.assertEqual(a1["epoch_id"], "epA1")
        self.assertEqual(a1["counts_per_mm"], 10)
        self.assertEqual(a1["amplitude_mm"], 1.0)
        self.assertEqual(a1["single_magnitude"], 0.0)

    def test_permutations_of_tables_do_not_change_output(self):
        baseline = self.estimate()
        rng = random.Random(207006)
        amps = copy.deepcopy(self.amps)
        epochs = copy.deepcopy(self.epochs)
        corrections = copy.deepcopy(self.corrections)
        rng.shuffle(amps)
        rng.shuffle(epochs)
        rng.shuffle(corrections)
        shuffled = self.estimate(
            amplitude_observations=amps,
            instrument_epochs=epochs,
            distance_corrections=corrections,
        )
        self.assertEqual(shuffled, baseline)

        original = copy.deepcopy({
            "amps": self.amps,
            "epochs": self.epochs,
            "corrections": self.corrections,
        })
        self.estimate()
        self.assertEqual({
            "amps": self.amps,
            "epochs": self.epochs,
            "corrections": self.corrections,
        }, original)


class DistanceAndCalibrationTests(unittest.TestCase):
    def _two_station_world(self, lon_o=1.0):
        process_result = process({
            "stations": [
                {"id": "Z", "lat": "0", "lon": "0"},
                {"id": "O", "lat": "0", "lon": str(lon_o)},
            ],
            "window": "10",
            "events": [
                {"id": "aZ", "station": "Z", "phase": "S", "time": "0"},
                {"id": "aO", "station": "O", "phase": "S", "time": "1"},
            ],
        })
        coords = [
            {"id": "Z", "lat": 0.0, "lon": 0.0},
            {"id": "O", "lat": 0.0, "lon": lon_o},
        ]
        location = {"events": [{
            "event_id": "e1",
            "status": "usable",
            "best_candidate": {"lat": 0.0, "lon": 0.0},
        }]}
        amps = [
            {"measurement_id": "mZ", "arrival_id": "aZ", "station": "Z",
             "channel": "H", "sample_start": 0, "sample_end": 1,
             "peak_to_peak_counts": 1},
            {"measurement_id": "mO", "arrival_id": "aO", "station": "O",
             "channel": "H", "sample_start": 0, "sample_end": 1,
             "peak_to_peak_counts": 1},
        ]
        epochs = [
            {"epoch_id": "eZ", "station": "Z", "channel": "H",
             "sample_start": 0, "sample_end": 2, "counts_per_mm": 1},
            {"epoch_id": "eO", "station": "O", "channel": "H",
             "sample_start": 0, "sample_end": 2, "counts_per_mm": 1},
        ]
        return process_result, location, coords, amps, epochs

    def test_interpolation_endpoints_and_out_of_range(self):
        pr, loc, coords, amps, epochs = self._two_station_world()
        expected_distance = great_circle_distance_km(0, 0, 0, 1)
        self.assertAlmostEqual(
            expected_distance, 6371.0088 * math.pi / 180.0, places=6)

        # 内部线性插值：correction = 2 * d / 200。
        table = [
            {"distance_km": 200, "correction": 2.0},
            {"distance_km": 0, "correction": 0.0},
        ]
        result = estimate_local_magnitudes(
            pr, loc, coords, amps, epochs, table, min_stations=2)
        obs = {row["measurement_id"]: row
               for row in result["events"][0]["observations"]}
        self.assertEqual(obs["mZ"]["distance_km"], 0.0)
        self.assertEqual(obs["mZ"]["distance_correction"], 0.0)
        self.assertAlmostEqual(
            obs["mO"]["distance_km"], expected_distance, places=9)
        self.assertAlmostEqual(
            obs["mO"]["distance_correction"],
            2.0 * expected_distance / 200.0,
            places=9,
        )
        self.assertAlmostEqual(
            obs["mO"]["single_magnitude"],
            2.0 * expected_distance / 200.0,
            places=9,
        )

        # 表的右端点包含在内，不向外推。
        endpoint_table = [
            {"distance_km": 0.0, "correction": 0.0},
            {"distance_km": expected_distance, "correction": 1.25},
        ]
        endpoint_result = estimate_local_magnitudes(
            pr, loc, coords, amps, epochs, endpoint_table, min_stations=2)
        endpoint_obs = {
            row["measurement_id"]: row
            for row in endpoint_result["events"][0]["observations"]
        }
        self.assertEqual(
            endpoint_obs["mO"]["distance_correction"], 1.25)
        self.assertEqual(endpoint_obs["mO"]["status"], "usable")

        # 超出表范围：保留实际距离，但没有修正值和单条震级。
        out_table = [
            {"distance_km": 0.0, "correction": 0.0},
            {"distance_km": 50.0, "correction": 1.0},
        ]
        out_result = estimate_local_magnitudes(
            pr, loc, coords, amps, epochs, out_table, min_stations=2)
        out_obs = {row["measurement_id"]: row
                   for row in out_result["events"][0]["observations"]}
        self.assertEqual(out_obs["mO"]["status"], "distance_out_of_range")
        self.assertAlmostEqual(
            out_obs["mO"]["distance_km"], expected_distance, places=9)
        self.assertIsNone(out_obs["mO"]["distance_correction"])
        self.assertIsNone(out_obs["mO"]["single_magnitude"])

    def test_half_open_epoch_endpoints(self):
        pr = process({
            "stations": [{"id": "A", "lat": "0", "lon": "0"}],
            "window": "1",
            "events": [{"id": "aA", "station": "A", "phase": "S",
                        "time": "0"}],
        })
        loc = {"events": [{"event_id": "e1", "status": "unresolved",
                           "best_candidate": None}]}
        coords = [{"id": "A", "lat": 0, "lon": 0}]
        amps = [
            {"measurement_id": "inside", "arrival_id": "aA", "station": "A",
             "channel": "H", "sample_start": 10, "sample_end": 20,
             "peak_to_peak_counts": 2},
            {"measurement_id": "left_touch", "arrival_id": "aA",
             "station": "A", "channel": "H", "sample_start": 5,
             "sample_end": 10, "peak_to_peak_counts": 2},
            {"measurement_id": "right_touch", "arrival_id": "aA",
             "station": "A", "channel": "H", "sample_start": 20,
             "sample_end": 25, "peak_to_peak_counts": 2},
        ]
        epochs = [
            {"epoch_id": "e", "station": "A", "channel": "H",
             "sample_start": 10, "sample_end": 20, "counts_per_mm": 2},
        ]
        result = estimate_local_magnitudes(
            pr, loc, coords, amps, epochs, zero_corrections(),
            min_stations=2)
        obs = {row["measurement_id"]: row
               for row in result["events"][0]["observations"]}
        self.assertEqual(obs["inside"]["status"], "location_not_usable")
        self.assertEqual(obs["inside"]["epoch_id"], "e")
        self.assertEqual(obs["inside"]["amplitude_mm"], 1.0)
        self.assertEqual(obs["left_touch"]["status"], "no_calibration")
        self.assertEqual(obs["right_touch"]["status"], "no_calibration")

    def test_span_across_adjacent_epochs_is_boundary(self):
        pr = process({
            "stations": [{"id": "A", "lat": "0", "lon": "0"}],
            "window": "1",
            "events": [{"id": "aA", "station": "A", "phase": "S",
                        "time": "0"}],
        })
        loc = {"events": [{"event_id": "e1", "status": "unresolved"}]}
        coords = {"A": (0.0, 0.0)}
        amps = [
            {"measurement_id": "span", "arrival_id": "aA", "station": "A",
             "channel": "H", "sample_start": 5, "sample_end": 15,
             "peak_to_peak_counts": 10},
            {"measurement_id": "disjoint", "arrival_id": "aA",
             "station": "A", "channel": "X", "sample_start": 100,
             "sample_end": 110, "peak_to_peak_counts": 10},
        ]
        epochs = [
            {"epoch_id": "e1", "station": "A", "channel": "H",
             "sample_start": 0, "sample_end": 10, "counts_per_mm": 1},
            {"epoch_id": "e2", "station": "A", "channel": "H",
             "sample_start": 10, "sample_end": 20, "counts_per_mm": 2},
        ]
        result = estimate_local_magnitudes(
            pr, loc, coords, amps, epochs, zero_corrections(),
            min_stations=2)
        obs = {row["measurement_id"]: row
               for row in result["events"][0]["observations"]}
        self.assertEqual(obs["span"]["status"], "calibration_boundary")
        self.assertIsNone(obs["span"]["epoch_id"])
        self.assertEqual(obs["disjoint"]["status"], "no_calibration")

    def test_duplicate_channel_observations_do_not_add_station_votes(self):
        pr = process({
            "stations": [
                {"id": "A", "lat": "0", "lon": "0"},
                {"id": "B", "lat": "0", "lon": "0"},
            ],
            "window": "1",
            "events": [
                {"id": "aA", "station": "A", "phase": "S", "time": "0"},
                {"id": "aB", "station": "B", "phase": "S", "time": "1"},
            ],
        })
        coords = station_input(("A", "B"))
        loc = {"events": [{
            "event_id": "e1",
            "status": "usable",
            "best_candidate": {"lat": 0.0, "lon": 0.0},
        }]}
        amps = [
            {"measurement_id": "mA1", "arrival_id": "aA", "station": "A",
             "channel": "H", "sample_start": 0, "sample_end": 10,
             "peak_to_peak_counts": 1},
            {"measurement_id": "mA2", "arrival_id": "aA", "station": "A",
             "channel": "H", "sample_start": 0, "sample_end": 10,
             "peak_to_peak_counts": 2},
            {"measurement_id": "mB", "arrival_id": "aB", "station": "B",
             "channel": "H", "sample_start": 0, "sample_end": 10,
             "peak_to_peak_counts": 1000},
        ]
        epochs = [
            {"epoch_id": "eA", "station": "A", "channel": "H",
             "sample_start": 0, "sample_end": 20, "counts_per_mm": 1},
            {"epoch_id": "eB", "station": "B", "channel": "H",
             "sample_start": 0, "sample_end": 20, "counts_per_mm": 1},
        ]
        result = estimate_local_magnitudes(
            pr, loc, coords, amps, epochs, zero_corrections(),
            min_stations=3)
        event = result["events"][0]
        self.assertEqual(event["station_count"], 2)
        self.assertEqual(
            event["magnitude_status"], "insufficient_amplitude_stations")
        self.assertIsNone(event["magnitude"])
        a_station = next(row for row in event["stations"]
                         if row["station"] == "A")
        self.assertEqual(a_station["observation_ids"], ["mA1", "mA2"])

    def test_p_phase_is_excluded_before_calibration(self):
        pr = process({
            "stations": [{"id": "A", "lat": "0", "lon": "0"}],
            "window": "1",
            "events": [{"id": "aP", "station": "A", "phase": "P",
                        "time": "0"}],
        })
        loc = usable_location_report(
            # 该辅助固定 A/B/C；这里直接构造 usable 诊断。
            process({
                "stations": decimal_station_input(),
                "window": "1",
                "events": [
                    {"id": "x", "station": "A", "phase": "S", "time": "0"},
                    {"id": "y", "station": "B", "phase": "S", "time": "1"},
                    {"id": "z", "station": "C", "phase": "S", "time": "2"},
                ],
            }),
            station_input(),
        )
        # 将三事件报告裁成本测试的一个 e1。
        loc["events"] = [loc["events"][0]]
        loc["events"][0]["event_id"] = "e1"
        loc["events"][0]["best_candidate"] = {"lat": 0, "lon": 0}
        amps = [
            {"measurement_id": "mP", "arrival_id": "aP", "station": "A",
             "channel": "H", "sample_start": 0, "sample_end": 10,
             "peak_to_peak_counts": 1},
        ]
        result = estimate_local_magnitudes(
            pr, loc, station_input(("A",)), amps, [],
            zero_corrections(), min_stations=2)
        obs = result["events"][0]["observations"][0]
        self.assertEqual(obs["status"], "unsupported_phase")
        self.assertIsNone(obs["epoch_id"])
        self.assertIsNone(obs["amplitude_mm"])
        self.assertEqual(result["events"][0]["station_count"], 0)


class InvalidInputTests(unittest.TestCase):
    def setUp(self):
        self.pr = acceptance_process()
        self.coords = station_input()
        self.loc = usable_location_report(self.pr, self.coords)
        self.amps = acceptance_amplitudes()
        self.epochs = acceptance_epochs()
        self.corr = zero_corrections()

    def base_kwargs(self):
        return dict(
            process_result=self.pr,
            location_report=copy.deepcopy(self.loc),
            station_coordinates=copy.deepcopy(self.coords),
            amplitude_observations=copy.deepcopy(self.amps),
            instrument_epochs=copy.deepcopy(self.epochs),
            distance_corrections=copy.deepcopy(self.corr),
            min_stations=3,
        )

    def assert_value_error(self, mutator):
        kwargs = self.base_kwargs()
        mutator(kwargs)
        with self.assertRaises(ValueError):
            estimate_local_magnitudes(**kwargs)

    def test_structural_and_identity_errors_are_rejected_as_batch(self):
        def duplicate_measurement(k):
            k["amplitude_observations"][1]["measurement_id"] = "mA1"

        def unknown_arrival(k):
            k["amplitude_observations"][0]["arrival_id"] = "missing"

        def station_mismatch(k):
            k["amplitude_observations"][0]["station"] = "B"

        def duplicate_epoch(k):
            k["instrument_epochs"][1]["epoch_id"] = "epA1"

        def overlapping_epochs(k):
            # epA2 原本是另一通道；改成同通道后两段 [0,20) 完全重叠。
            k["instrument_epochs"][1]["channel"] = "HHZ"

        def boolean_start(k):
            k["amplitude_observations"][0]["sample_start"] = True

        def negative_start(k):
            k["amplitude_observations"][0]["sample_start"] = -1

        def empty_interval(k):
            k["amplitude_observations"][0]["sample_end"] = 0

        def boolean_min(k):
            k["min_stations"] = True

        def too_small_min(k):
            k["min_stations"] = 1

        def missing_coordinates(k):
            k["station_coordinates"] = station_input(("A", "B"))

        def too_few_corrections(k):
            k["distance_corrections"] = [self.corr[0]]

        def negative_distance(k):
            k["distance_corrections"][0]["distance_km"] = -1

        def duplicate_distance(k):
            # zero_corrections() 故意乱序：第 0 项是 1000，第 1 项是 0。
            k["distance_corrections"][0]["distance_km"] = 0

        def nonfinite_correction(k):
            k["distance_corrections"][0]["correction"] = math.inf

        def zero_counts(k):
            k["amplitude_observations"][0]["peak_to_peak_counts"] = 0

        def nonpositive_gain(k):
            k["instrument_epochs"][0]["counts_per_mm"] = -2

        mutators = [
            duplicate_measurement, unknown_arrival, station_mismatch,
            duplicate_epoch, overlapping_epochs, boolean_start,
            negative_start, empty_interval, boolean_min, too_small_min,
            missing_coordinates, too_few_corrections, negative_distance,
            duplicate_distance, nonfinite_correction, zero_counts,
            nonpositive_gain,
        ]
        for mutator in mutators:
            with self.subTest(mutator=mutator.__name__):
                self.assert_value_error(mutator)

    def test_location_report_must_match_every_event(self):
        def missing(k):
            k["location_report"]["events"].pop()

        def extra(k):
            k["location_report"]["events"].append({
                "event_id": "ghost", "status": "usable",
                "best_candidate": {"lat": 0, "lon": 0}})

        def duplicate(k):
            event = copy.deepcopy(k["location_report"]["events"][0])
            k["location_report"]["events"].append(event)

        def bad_status(k):
            k["location_report"]["events"][0]["status"] = "located"

        def usable_without_coordinates(k):
            k["location_report"]["events"][0]["best_candidate"] = None

        for mutator in (missing, extra, duplicate, bad_status,
                        usable_without_coordinates):
            with self.subTest(mutator=mutator.__name__):
                self.assert_value_error(mutator)


if __name__ == "__main__":
    unittest.main()
