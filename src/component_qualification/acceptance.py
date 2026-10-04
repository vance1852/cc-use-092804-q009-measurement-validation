"""容器和快照检查使用的冒烟验收命令。"""

from __future__ import annotations

import argparse
import json

from .service import ComponentService


def run() -> dict:
    service = ComponentService()
    service.bootstrap_admin()
    token = service.auth.login("admin", "component-admin")
    service.register_instrument(token, "spectrometer-1", "GS-FR-7000", "国产光频平台")
    service.create_lot(token, "LOT-DEMO", "CMOS image sensor", "P3.2", 10)
    measurements = [
        {"sample_key": "S-001", "signal_frequency_hz": 450e9, "response": 0.71, "noise": 0.01, "instrument": "spectrometer-1"},
        {"sample_key": "S-002", "signal_frequency_hz": 520e9, "response": 0.93, "noise": 0.02, "instrument": "spectrometer-1"},
        {"sample_key": "S-003", "signal_frequency_hz": 650e9, "response": 0.84, "noise": 0.015, "instrument": "spectrometer-1"},
    ]
    service.write_measurements(token, "LOT-DEMO", measurements, idempotency_key="acceptance-1")
    result = service.analyze(token, "LOT-DEMO")
    service.approve(token, "LOT-DEMO", "hold", "awaiting quality review")
    return {
        "status": "ok",
        "lot": result["lot_id"],
        "peak": result["signal_profile"]["peak_signal_frequency_hz"],
        "valid_measurements": len(result["valid_measurement_ids"]),
        "input_sha256": result["input_sha256"],
        "events": len(service.audit(token, "LOT-DEMO")),
    }


def main() -> None:
    argparse.ArgumentParser().parse_args()
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
