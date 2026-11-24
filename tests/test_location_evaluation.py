"""候选震源位置诊断与下游准入验收测试。

这些测试逐项覆盖任务给出的五类验收情形：anchor 无关性、重复台站到时不增加
支持台站数、歧义边界、缺失旅行时/unresolved/偶数中位数/并列排序/打乱不变性，
以及非法输入整体拒绝和多事件状态隔离。
"""
from __future__ import annotations

import copy
import json
import unittest

from seismic_assoc import (
    LocationInputError,
    evaluate_location_candidates,
    locate_events,
    process,
)


def coords_for(stations):
    return {
        station: (index * 0.01, (index % 7) * 0.01)
        for index, station in enumerate(stations)
    }


def process_three_stations(a_time="102", bad_a=None, extra_bad=0):
    """构造 A/B/C 归并事件；可给 A 增加坏到时和更多同值重复到时。"""
    arrivals = [
        {"id": "a-good", "station": "A", "phase": "P", "time": a_time},
        {"id": "b-good", "station": "B", "phase": "P", "time": "104"},
        {"id": "c-good", "station": "C", "phase": "P", "time": "106"},
    ]
    if bad_a is not None:
        arrivals.append({
            "id": "a-bad", "station": "A", "phase": "P", "time": bad_a,
        })
        for i in range(extra_bad):
            arrivals.append({
                "id": f"a-bad-dup-{i}",
                "station": "A",
                "phase": "P",
                "time": bad_a,
            })
    data = {
        "stations": [
            {"id": "A", "lat": "0", "lon": "0"},
            {"id": "B", "lat": "0", "lon": "1"},
            {"id": "C", "lat": "1", "lon": "0"},
        ],
        "events": arrivals,
        "window": "100",
    }
    return process(data)


def c1_candidate():
    return {
        "candidate_id": "c1",
        "lat": 1.0,
        "lon": 2.0,
        "travel_times": {
            "A": {"P": 2},
            "B": {"P": 4},
            "C": {"P": 6},
        },
    }


def c2_shifted_candidate():
    # 与 c1 保持相同走时差，仅整体多 18；它会独立得到 origin=82。
    return {
        "candidate_id": "c2",
        "lat": 3.0,
        "lon": 4.0,
        "travel_times": {
            "A": {"P": 20},
            "B": {"P": 22},
            "C": {"P": 24},
        },
    }


QUALITY = {
    "residual_threshold": 0.1,
    "min_supporting_stations": 3,
    "ambiguity_margin": 0.05,
}


class AcceptanceScenarioOneTests(unittest.TestCase):
    def test_c1_origin_is_100_and_usable_without_configured_margin(self):
        result = process_three_stations()
        coordinates = coords_for(["A", "B", "C"])
        output = evaluate_location_candidates(
            result,
            coordinates,
            [c1_candidate(), c2_shifted_candidate()],
            residual_threshold=0.1,
            min_supporting_stations=3,
        )
        event = output["events"][0]
        self.assertEqual(event["event_id"], "e1")
        self.assertEqual(event["status"], "usable")
        self.assertTrue(event["usable"])
        self.assertEqual(event["best_candidate_id"], "c1")
        self.assertEqual(event["best_candidate"]["candidate_id"], "c1")
        self.assertEqual(event["best_candidate"]["origin_time"], 100)
        self.assertEqual(
            event["best_candidate"]["supporting_stations"], ["A", "B", "C"])
        # c1 与 c2 残差中位数并列时，c1 凭字典序成为诊断首选，但未配置
        # ambiguity_margin 时不启用歧义准入分支。
        self.assertEqual(
            [c["candidate_id"] for c in event["candidates"]], ["c1", "c2"])

    def test_changing_anchor_does_not_change_location_decision(self):
        original = process_three_stations()
        coordinates = coords_for(["A", "B", "C"])
        candidates = [c1_candidate(), c2_shifted_candidate()]
        params = {
            "residual_threshold": 0.1,
            "min_supporting_stations": 3,
            "ambiguity_margin": 0,
        }
        before = evaluate_location_candidates(
            original, coordinates, candidates, params)

        changed = copy.deepcopy(original)
        changed["events"][0]["anchor"] = "-987654321.5"
        after = evaluate_location_candidates(
            changed, coordinates, candidates, params)

        self.assertNotEqual(
            changed["events"][0]["anchor"], original["events"][0]["anchor"])
        self.assertEqual(
            after["events"][0]["best_candidate"]["origin_time"], 100)
        self.assertEqual(after["events"][0]["status"], "ambiguous")
        self.assertEqual(
            json.dumps(before, sort_keys=True),
            json.dumps(after, sort_keys=True),
        )

    def test_anchor_as_origin_would_produce_wrong_residuals(self):
        result = process_three_stations()
        output = evaluate_location_candidates(
            result, coords_for(["A", "B", "C"]), [c1_candidate()], QUALITY)
        best = output["events"][0]["best_candidate"]
        # anchor 是 102；若错误地把它当 origin，A 的残差会是 2 而不是 0。
        a_evidence = next(
            item for item in best["arrival_evidence"] if item["id"] == "a-good")
        self.assertEqual(a_evidence["origin_estimate"], 100)
        self.assertEqual(a_evidence["residual"], 0)


class DuplicateStationArrivalTests(unittest.TestCase):
    def test_more_arrivals_do_not_become_more_supporting_stations(self):
        result = process_three_stations(bad_a="102.5", extra_bad=3)
        output = evaluate_location_candidates(
            result,
            coords_for(["A", "B", "C"]),
            [c1_candidate()],
            QUALITY,
        )
        event = output["events"][0]
        best = event["best_candidate"]

        self.assertEqual(best["support_count"], 2)
        self.assertEqual(best["supporting_stations"], ["B", "C"])
        self.assertEqual(best["outlier_stations"], ["A"])
        self.assertEqual(event["supporting_stations"], ["B", "C"])
        self.assertEqual(event["outlier_stations"], ["A"])
        self.assertEqual(event["status"], "insufficient_evidence")
        self.assertFalse(event["usable"])

        a_evidence = [
            item for item in best["arrival_evidence"]
            if item["station"] == "A"
        ]
        # 一条好到时、一条初始坏到时、三条额外重复坏到时。
        self.assertEqual(len(a_evidence), 5)
        self.assertEqual(
            [item["id"] for item in a_evidence],
            ["a-good", "a-bad", "a-bad-dup-0", "a-bad-dup-1",
             "a-bad-dup-2"],
        )
        self.assertTrue(all(item["evaluable"] for item in a_evidence))
        self.assertTrue(all(isinstance(item["residual"], (int, float))
                            for item in a_evidence))
        good = next(item for item in a_evidence if item["id"] == "a-good")
        bad = next(item for item in a_evidence if item["id"] == "a-bad")
        self.assertEqual(good["residual"], 0)
        self.assertEqual(bad["residual"], 0.5)

        station_a = next(
            row for row in best["station_evidence"] if row["station"] == "A")
        self.assertEqual(station_a["residual_median"], 0.5)
        self.assertFalse(station_a["supporting"])
        self.assertEqual(station_a["reason_code"],
                         "station_residual_above_threshold")


class AmbiguityTests(unittest.TestCase):
    def test_close_candidate_is_ambiguous_even_though_c1_is_best(self):
        c3 = {
            "candidate_id": "c3",
            "lat": 4,
            "lon": 5,
            # 三个台站 origin estimates = 100, 100.04, 100.10；
            # 候选 origin=100.04，支持站残差中位数=0.04。
            "travel_times": {
                "A": {"P": 2},
                "B": {"P": "3.96"},
                "C": {"P": "5.90"},
            },
        }
        output = evaluate_location_candidates(
            process_three_stations(),
            coords_for(["A", "B", "C"]),
            [c3, c1_candidate()],  # 故意打乱候选输入顺序
            {**QUALITY, "ambiguity_margin": "0.05"},
        )
        event = output["events"][0]
        self.assertEqual(
            [c["candidate_id"] for c in event["candidates"]], ["c1", "c3"])
        self.assertEqual(event["best_candidate_id"], "c1")
        self.assertEqual(event["status"], "ambiguous")
        self.assertFalse(event["usable"])
        self.assertEqual(event["ambiguous_candidate_ids"], ["c3"])
        self.assertEqual(
            event["ambiguous_candidates"][0]["reason_code"],
            "equal_support_within_ambiguity_margin")
        self.assertEqual(
            event["ambiguous_candidates"][0]["difference_from_best"], 0.04)
        # 两个候选及证据都保留。
        self.assertEqual(len(event["candidates"]), 2)
        for candidate in event["candidates"]:
            self.assertEqual(len(candidate["arrival_evidence"]), 3)
            self.assertEqual(len(candidate["station_evidence"]), 3)

    def test_difference_exactly_on_margin_is_still_ambiguous(self):
        c3 = {
            "candidate_id": "c3",
            "lat": 4,
            "lon": 5,
            "travel_times": {
                "A": {"P": 2},
                "B": {"P": "3.96"},
                "C": {"P": "5.90"},
            },
        }
        output = evaluate_location_candidates(
            process_three_stations(),
            coords_for(["A", "B", "C"]),
            [c1_candidate(), c3],
            {**QUALITY, "ambiguity_margin": "0.04"},
        )
        self.assertEqual(output["events"][0]["status"], "ambiguous")

    def test_candidate_outside_margin_restores_usable(self):
        c3 = {
            "candidate_id": "c3",
            "lat": 4,
            "lon": 5,
            # 支持残差中位数 0.05，大于 margin=0.04。
            "travel_times": {
                "A": {"P": 2},
                "B": {"P": "3.95"},
                "C": {"P": "5.90"},
            },
        }
        output = evaluate_location_candidates(
            process_three_stations(),
            coords_for(["A", "B", "C"]),
            [c1_candidate(), c3],
            {**QUALITY, "ambiguity_margin": "0.04"},
        )
        event = output["events"][0]
        self.assertEqual(event["status"], "usable")
        self.assertEqual(event["ambiguous_candidate_ids"], [])

    def test_tied_support_medians_sort_by_candidate_id(self):
        candidates = []
        for cid, shift in [("c3", 18), ("c1", 0), ("c2", 9)]:
            candidates.append({
                "candidate_id": cid,
                "lat": 0,
                "lon": 0,
                # 整体平移不改变零残差，因此支持数和支持残差中位数全并列。
                "travel_times": {
                    "A": {"P": 2 + shift},
                    "B": {"P": 4 + shift},
                    "C": {"P": 6 + shift},
                },
            })
        output = evaluate_location_candidates(
            process_three_stations(),
            coords_for(["A", "B", "C"]),
            candidates,
            {**QUALITY, "ambiguity_margin": 0},
        )
        event = output["events"][0]
        self.assertEqual(
            [c["candidate_id"] for c in event["candidates"]],
            ["c1", "c2", "c3"])
        self.assertEqual(event["best_candidate_id"], "c1")
        self.assertEqual(len(event["ambiguous_candidate_ids"]), 2)


class MissingEvidenceAndMedianTests(unittest.TestCase):
    def test_missing_phase_travel_time_is_unevaluable_not_outlier(self):
        data = {
            "stations": [
                {"id": "A", "lat": "0", "lon": "0"},
                {"id": "B", "lat": "0", "lon": "1"},
            ],
            "events": [
                {"id": "a-p", "station": "A", "phase": "P", "time": "102"},
                {"id": "b-p", "station": "B", "phase": "P", "time": "104"},
                {"id": "b-s", "station": "B", "phase": "S", "time": "108"},
            ],
            "window": "100",
        }
        result = process(data)
        candidate = {
            "candidate_id": "c1",
            "lat": 0,
            "lon": 0,
            "travel_times": {
                "A": {"P": 2},
                # 故意缺少 B.S。
                "B": {"P": 4},
            },
        }
        output = evaluate_location_candidates(
            result,
            coords_for(["A", "B"]),
            [candidate],
            {"residual_threshold": 0.1, "min_supporting_stations": 3},
        )
        best = output["events"][0]["best_candidate"]
        bs = next(e for e in best["arrival_evidence"] if e["id"] == "b-s")
        bp = next(e for e in best["arrival_evidence"] if e["id"] == "b-p")
        self.assertFalse(bs["evaluable"])
        self.assertIsNone(bs["residual"])
        self.assertEqual(bs["reason_code"], "missing_travel_time")
        self.assertTrue(bp["evaluable"])
        station_b = next(
            row for row in best["station_evidence"] if row["station"] == "B")
        self.assertEqual(station_b["evaluable_arrival_count"], 1)
        self.assertEqual(station_b["status"], "supporting")
        self.assertNotIn("B", best["unevaluated_stations"])

    def test_all_candidates_unevaluable_returns_unresolved_event(self):
        data = {
            "stations": [{"id": s, "lat": "0", "lon": "0"}
                         for s in ("X", "Y", "Z")],
            "events": [
                {"id": "x", "station": "X", "phase": "P", "time": "1"},
                {"id": "y", "station": "Y", "phase": "P", "time": "2"},
                {"id": "z", "station": "Z", "phase": "S", "time": "3"},
            ],
            "window": "100",
        }
        result = process(data)
        candidates = [{
            "candidate_id": "c1", "lat": 0, "lon": 0,
            "travel_times": {"A": {"P": 1}},
        }]
        # A 不在事件台站中，但它在坐标映射中不存在会触发未知站拒绝；因此
        # 这里改用空旅行时表表达“所有候选均无法评估这些到时”。
        candidates[0]["travel_times"] = {}
        output = evaluate_location_candidates(
            result,
            coords_for(["X", "Y", "Z"]),
            candidates,
            {"residual_threshold": 0.1, "min_supporting_stations": 3},
        )
        event = output["events"][0]
        self.assertEqual(event["status"], "unresolved")
        self.assertFalse(event["usable"])
        self.assertIsNone(event["best_candidate"])
        self.assertEqual(event["best_candidate_id"], None)
        self.assertEqual(event["unevaluated_stations"], ["X", "Y", "Z"])
        self.assertEqual(len(event["candidates"]), 1)
        self.assertFalse(event["candidates"][0]["evaluable"])
        self.assertEqual(len(event["candidates"][0]["arrival_evidence"]), 3)
        self.assertTrue(all(
            not item["evaluable"]
            for item in event["candidates"][0]["arrival_evidence"]))

    def test_even_median_of_two_arrival_origin_estimates(self):
        data = {
            "stations": [{"id": "A", "lat": "0", "lon": "0"}],
            "events": [
                # origin estimates: 102-2=100 and 104-2=102 -> median 101.
                {"id": "a1", "station": "A", "phase": "P", "time": "102"},
                {"id": "a2", "station": "A", "phase": "P", "time": "104"},
            ],
            "window": "100",
        }
        result = process(data)
        output = evaluate_location_candidates(
            result,
            coords_for(["A"]),
            [{
                "candidate_id": "c1", "lat": 0, "lon": 0,
                "travel_times": {"A": {"P": 2}},
            }],
            {"residual_threshold": 10, "min_supporting_stations": 3},
        )
        best = output["events"][0]["best_candidate"]
        station_a = best["station_evidence"][0]
        self.assertEqual(station_a["origin_time"], 101)
        self.assertEqual(best["origin_time"], 101)

    def test_residual_exactly_on_threshold_counts_as_support(self):
        data = {
            "stations": [{"id": s, "lat": "0", "lon": "0"}
                         for s in ("A", "B", "C")],
            "events": [
                {"id": "a", "station": "A", "phase": "P", "time": "102"},
                {"id": "b", "station": "B", "phase": "P", "time": "104.1"},
                {"id": "c", "station": "C", "phase": "P", "time": "106.2"},
            ],
            "window": "100",
        }
        result = process(data)
        candidate = {
            "candidate_id": "c1", "lat": 0, "lon": 0,
            # origin estimates: 100, 100.1, 100.2; median=100.1;
            # residuals .1, 0, .1，恰好等于 threshold=.1。
            "travel_times": {
                "A": {"P": 2}, "B": {"P": 4}, "C": {"P": 6},
            },
        }
        output = evaluate_location_candidates(
            result, coords_for(["A", "B", "C"]), [candidate],
            {**QUALITY, "ambiguity_margin": 0},
        )
        best = output["events"][0]["best_candidate"]
        self.assertEqual(best["supporting_stations"], ["A", "B", "C"])
        self.assertEqual(best["support_count"], 3)
        self.assertEqual(output["events"][0]["status"], "usable")


class DeterminismAndImmutabilityTests(unittest.TestCase):
    def test_shuffled_candidate_and_arrival_order_outputs_unchanged(self):
        result = process_three_stations()
        coordinates = coords_for(["A", "B", "C"])
        candidates = [
            c2_shifted_candidate(),
            {
                "candidate_id": "c3", "lat": 9, "lon": 9,
                "travel_times": {"A": {"P": 9}, "B": {"P": 9},
                                 "C": {"P": 9}},
            },
            c1_candidate(),
        ]
        params = {**QUALITY, "ambiguity_margin": 0.01}
        baseline = evaluate_location_candidates(
            result, coordinates, candidates, params)

        shuffled_result = copy.deepcopy(result)
        shuffled_result["events"][0]["arrivals"].reverse()
        shuffled_candidates = list(reversed(copy.deepcopy(candidates)))
        shuffled = evaluate_location_candidates(
            shuffled_result, coordinates, shuffled_candidates, params)

        self.assertEqual(
            json.dumps(baseline, sort_keys=True),
            json.dumps(shuffled, sort_keys=True),
        )

    def test_inputs_are_not_modified_and_output_is_strict_json(self):
        result = process_three_stations()
        coordinates = coords_for(["A", "B", "C"])
        candidates = [c1_candidate(), c2_shifted_candidate()]
        params = QUALITY
        snapshots = copy.deepcopy((result, coordinates, candidates, params))

        output = evaluate_location_candidates(
            result, coordinates, candidates, params)

        self.assertEqual(
            snapshots, (result, coordinates, candidates, params))
        json.dumps(output, allow_nan=False, sort_keys=True)

    def test_locate_events_alias_is_public(self):
        self.assertIs(locate_events, evaluate_location_candidates)


class InvalidInputTests(unittest.TestCase):
    def setUp(self):
        self.result = process_three_stations()
        self.coordinates = coords_for(["A", "B", "C"])
        self.candidates = [c1_candidate()]
        self.quality = QUALITY

    def assert_rejected(self, result=None, coordinates=None,
                        candidates=None, quality=None):
        with self.assertRaises((LocationInputError, ValueError)):
            evaluate_location_candidates(
                self.result if result is None else result,
                self.coordinates if coordinates is None else coordinates,
                self.candidates if candidates is None else candidates,
                self.quality if quality is None else quality,
            )

    def test_duplicate_candidate_ids_rejected(self):
        duplicate = [c1_candidate(), c1_candidate()]
        self.assert_rejected(candidates=duplicate)

    def test_unknown_station_rejected(self):
        bad_candidate = c1_candidate()
        bad_candidate["travel_times"]["Z"] = {"P": 1}
        self.assert_rejected(candidates=[bad_candidate])

        bad_coordinates = dict(self.coordinates)
        # 删除 A 坐标会使 process 结果中的 A 到时成为未知台站。
        del bad_coordinates["A"]
        self.assert_rejected(coordinates=bad_coordinates)

    def test_illegal_numeric_values_rejected(self):
        bad_negative = c1_candidate()
        bad_negative["travel_times"]["A"]["P"] = -0.01
        self.assert_rejected(candidates=[bad_negative])

        bad_nan = c1_candidate()
        bad_nan["travel_times"]["A"]["P"] = float("nan")
        self.assert_rejected(candidates=[bad_nan])

        bad_inf = c1_candidate()
        bad_inf["travel_times"]["A"]["P"] = float("inf")
        self.assert_rejected(candidates=[bad_inf])

        bad_text = c1_candidate()
        bad_text["lat"] = "not-a-number"
        self.assert_rejected(candidates=[bad_text])

    def test_illegal_coordinates_rejected(self):
        bad_candidate = c1_candidate()
        bad_candidate["lat"] = 91
        self.assert_rejected(candidates=[bad_candidate])
        self.assert_rejected(coordinates={"A": [91, 0], "B": [0, 1],
                                          "C": [1, 0]})

    def test_quality_bounds_and_structure_rejected(self):
        self.assert_rejected(quality={
            "residual_threshold": 0,
            "min_supporting_stations": 3,
        })
        self.assert_rejected(quality={
            "residual_threshold": -0.1,
            "min_supporting_stations": 3,
        })
        self.assert_rejected(quality={
            "residual_threshold": 0.1,
            "min_supporting_stations": 2,
        })
        self.assert_rejected(quality={
            "residual_threshold": 0.1,
            "min_supporting_stations": 3,
            "ambiguity_margin": -0.01,
        })
        self.assert_rejected(quality={
            "min_supporting_stations": 3,
        })

    def test_duplicate_arrival_ids_rejected_globally(self):
        bad_result = copy.deepcopy(self.result)
        bad_result["events"].append(copy.deepcopy(bad_result["events"][0]))
        self.assert_rejected(result=bad_result)

    def test_incomplete_candidate_rejected(self):
        bad = c1_candidate()
        del bad["lat"]
        self.assert_rejected(candidates=[bad])


class MixedEventIsolationTests(unittest.TestCase):
    def test_one_call_contains_all_four_admission_statuses(self):
        stations = [
            "U1", "U2", "U3",
            "M1", "M2", "M3",
            "I1", "I2", "I3",
            "X1", "X2", "X3",
        ]
        arrivals = []

        def add(prefix, station_times):
            for i, (station, time) in enumerate(station_times):
                arrivals.append({
                    "id": f"{prefix}-{i}",
                    "station": station,
                    "phase": "P",
                    "time": str(time),
                })

        add("u", [("U1", 102), ("U2", 104), ("U3", 106)])
        add("m", [("M1", 1002), ("M2", 1004), ("M3", 1006)])
        add("i", [("I1", 2002), ("I2", 2004), ("I3", 2000)])
        add("x", [("X1", 3002), ("X2", 3004), ("X3", 3006)])

        result = process({
            "stations": [
                {"id": s, "lat": "0", "lon": str(index * 0.01)}
                for index, s in enumerate(stations)
            ],
            "events": arrivals,
            "window": "100",
        })

        candidates = [
            {
                "candidate_id": "c1",
                "lat": 0,
                "lon": 0,
                "travel_times": {
                    "U1": {"P": 2}, "U2": {"P": 4}, "U3": {"P": 6},
                    "M1": {"P": 2}, "M2": {"P": 4}, "M3": {"P": 6},
                    "I1": {"P": 2}, "I2": {"P": 4}, "I3": {"P": 6},
                },
            },
            {
                "candidate_id": "c2",
                "lat": 1,
                "lon": 1,
                "travel_times": {
                    # U 上走时形态不一致，不能支持 U。
                    "U1": {"P": 20}, "U2": {"P": 23}, "U3": {"P": 27},
                    # M 上整体平移，形成同等零残差。
                    "M1": {"P": 20}, "M2": {"P": 22}, "M3": {"P": 24},
                    "I1": {"P": 20}, "I2": {"P": 22}, "I3": {"P": 24},
                },
            },
        ]

        output = evaluate_location_candidates(
            result, coords_for(stations), candidates, QUALITY)

        by_status = {e["status"]: e for e in output["events"]}
        self.assertEqual(
            set(by_status),
            {"usable", "ambiguous", "insufficient_evidence", "unresolved"})
        self.assertEqual(output["summary"]["usable"], 1)
        self.assertEqual(output["summary"]["ambiguous"], 1)
        self.assertEqual(output["summary"]["insufficient_evidence"], 1)
        self.assertEqual(output["summary"]["unresolved"], 1)

        usable = by_status["usable"]
        ambiguous = by_status["ambiguous"]
        insufficient = by_status["insufficient_evidence"]
        unresolved = by_status["unresolved"]

        self.assertEqual(usable["best_candidate_id"], "c1")
        self.assertEqual(usable["supporting_stations"],
                         ["U1", "U2", "U3"])
        self.assertEqual(usable["ambiguous_candidate_ids"], [])

        self.assertEqual(ambiguous["best_candidate_id"], "c1")
        self.assertEqual(ambiguous["ambiguous_candidate_ids"], ["c2"])
        self.assertEqual(ambiguous["supporting_stations"],
                         ["M1", "M2", "M3"])

        self.assertEqual(insufficient["best_candidate_id"], "c1")
        self.assertEqual(insufficient["supporting_stations"], ["I1", "I2"])
        self.assertEqual(insufficient["outlier_stations"], ["I3"])

        self.assertIsNone(unresolved["best_candidate"])
        self.assertEqual(unresolved["unevaluated_stations"],
                         ["X1", "X2", "X3"])

        # 每个事件都保留两个候选的完整诊断，状态没有共享可变对象。
        for event in output["events"]:
            self.assertEqual(len(event["candidates"]), 2)
            self.assertEqual(
                len(event["candidates"][0]["arrival_evidence"]), 3)


if __name__ == "__main__":
    unittest.main()
