"""候选震源位置诊断 API 的合成示例。

运行：

    python3 examples/run_location_evaluation.py

未安装包时脚本会自动把 ../src 加入 sys.path。示例只使用原创合成数据，
不读取波形、不访问网络，也不修改归并结果。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seismic_assoc import evaluate_location_candidates, process  # noqa: E402


def main() -> None:
    process_result = process({
        "stations": [
            {"id": "A", "lat": "0", "lon": "0"},
            {"id": "B", "lat": "0", "lon": "1"},
            {"id": "C", "lat": "1", "lon": "0"},
        ],
        "events": [
            {"id": "a", "station": "A", "phase": "P", "time": "102"},
            {"id": "b", "station": "B", "phase": "P", "time": "104"},
            {"id": "c", "station": "C", "phase": "P", "time": "106"},
        ],
        "window": "100",
    })

    report = evaluate_location_candidates(
        process_result,
        {"A": {"lat": 0, "lon": 0},
         "B": {"lat": 0, "lon": 1},
         "C": {"lat": 1, "lon": 0}},
        [
            {
                "candidate_id": "close-shift",
                "lat": 2,
                "lon": 2,
                "travel_times": {
                    "A": {"P": 2.04},
                    "B": {"P": 4},
                    "C": {"P": 6},
                },
            },
            {
                "candidate_id": "c1",
                "lat": 1,
                "lon": 1,
                "travel_times": {
                    "A": {"P": 2},
                    "B": {"P": 4},
                    "C": {"P": 6},
                },
            },
        ],
        {
            "residual_threshold": 0.1,
            "min_supporting_stations": 3,
            "ambiguity_margin": 0.05,
        },
    )

    compact = {
        "summary": report["summary"],
        "events": [{
            "event_id": event["event_id"],
            "status": event["status"],
            "usable": event["usable"],
            "best_candidate_id": event["best_candidate_id"],
            "origin_time": (event["best_candidate"] or {}).get("origin_time"),
            "supporting_stations": event["supporting_stations"],
            "ambiguous_candidate_ids": event["ambiguous_candidate_ids"],
            "reason": event["reason"],
        } for event in report["events"]],
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
