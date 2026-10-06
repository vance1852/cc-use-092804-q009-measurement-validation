"""容器和快照检查使用的冒烟验收命令。"""

from __future__ import annotations

import argparse
import json

from .service import ComponentService


def run() -> dict:
    service = ComponentService()
    service.bootstrap_admin()
    token = service.auth.login("admin", "component-admin")
    service.create_lot(token, "LOT-DEMO", "CMOS image sensor", "P3.2", 10)
    service.register_instrument(token, "spectrometer-1", vendor="国产光仪", model="GS-200")
    rows = [
        {"signal_frequency_hz": 450, "response": .71, "noise": .01, "instrument": "spectrometer-1",
         "sample_id": "bench-001"},
        {"signal_frequency_hz": 520, "response": .93, "noise": .02, "instrument": "spectrometer-1",
         "sample_id": "bench-002"},
        {"signal_frequency_hz": 650, "response": .84, "noise": .01, "instrument": "spectrometer-1",
         "sample_id": "bench-003"},
    ]
    imported = service.add_measurements(token, "LOT-DEMO", rows, idempotency_key="bench-import-1")
    replayed = service.add_measurements(token, "LOT-DEMO", rows, idempotency_key="bench-import-1")
    assert imported == replayed, "等价重放必须返回稳定结果"
    assert replayed["inserted"] == 3
    result = service.analyze(token, "LOT-DEMO")
    service.approve(token, "LOT-DEMO", "hold", "awaiting quality review")
    report = service.quality_report(token, "LOT-DEMO")
    return {
        "status": "ok",
        "lot": result["lot_id"],
        "peak": result["signal_profile"]["peak_signal_frequency_hz"],
        "valid_measurements": report["valid_measurement_count"],
        "events": len(service.audit(token, "LOT-DEMO")),
    }


def main() -> None:
    argparse.ArgumentParser().parse_args()
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
