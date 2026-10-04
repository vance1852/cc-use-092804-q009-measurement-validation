from __future__ import annotations

import math
import unittest

from component_qualification.contracts import LIMITS, validate_batch, validate_measurement
from component_qualification.jsonio import _NonFinite


INSTRUMENTS = frozenset({"spectrometer-1"})


def one(raw: dict, instruments: frozenset[str] = INSTRUMENTS, existing: set[str] | None = None):
    violations: list[dict] = []
    item = validate_measurement(
        raw, "measurements[0]", instruments, violations, set(existing or ()), set()
    )
    return item, violations


def valid_sample(**overrides) -> dict:
    sample = {
        "sample_key": "S-001",
        "signal_frequency_hz": 450e9,
        "response": 0.91,
        "noise": 0.02,
        "instrument": "spectrometer-1",
    }
    sample.update(overrides)
    return sample


class ContractTests(unittest.TestCase):
    def test_valid_sample_passes(self) -> None:
        item, violations = one(valid_sample())
        self.assertIsNotNone(item)
        self.assertEqual(violations, [])

    def test_non_finite_constants_name_field_and_rule(self) -> None:
        for token in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(token=token):
                _, violations = one(valid_sample(response=_NonFinite(token)))
                self.assertEqual(len(violations), 1)
                self.assertEqual(violations[0]["field"], "measurements[0].response")
                self.assertEqual(violations[0]["rule"], "finite")

    def test_python_float_inf_is_rejected(self) -> None:
        _, violations = one(valid_sample(response=float("inf")))
        self.assertEqual(violations[0]["rule"], "finite")

    def test_numeric_overflow_token_in_json(self) -> None:
        # 1e999 经标准解析得到 float('inf')，仍须在写入边界被拦截。
        _, violations = one(valid_sample(response=1e999))
        self.assertEqual(violations[0]["rule"], "finite")

    def test_string_disguised_number(self) -> None:
        _, violations = one(valid_sample(response="0.91"))
        self.assertEqual(violations[0]["field"], "measurements[0].response")
        self.assertEqual(violations[0]["rule"], "type.number")

    def test_boolean_is_not_a_number(self) -> None:
        _, violations = one(valid_sample(noise=True))
        self.assertEqual(violations[0]["rule"], "type.number")

    def test_range_violations_report_acceptable_bounds(self) -> None:
        cases = {
            "signal_frequency_hz": (0.0, LIMITS["signal_frequency_hz"].maximum + 1),
            "response": (-0.01, 2.01),
            "noise": (-0.1, 1.0),  # 上界为开区间
        }
        for field, bad_values in cases.items():
            for bad in bad_values:
                with self.subTest(field=field, bad=bad):
                    _, violations = one(valid_sample(**{field: bad}))
                    self.assertEqual(violations[0]["field"], f"measurements[0].{field}")
                    self.assertEqual(violations[0]["rule"], "range")
                    self.assertIn("可接受量程", violations[0]["message"])

    def test_noise_upper_bound_is_open(self) -> None:
        item, violations = one(valid_sample(noise=0.999))
        self.assertIsNotNone(item)
        _, violations = one(valid_sample(noise=1.0))
        self.assertEqual(violations[0]["rule"], "range")

    def test_missing_fields_are_required(self) -> None:
        _, violations = one({"sample_key": "S-001"})
        rules = {(v["field"].split(".", 1)[1], v["rule"]) for v in violations}
        self.assertIn(("signal_frequency_hz", "required"), rules)
        self.assertIn(("response", "required"), rules)
        self.assertIn(("noise", "required"), rules)
        self.assertIn(("instrument", "required"), rules)

    def test_unknown_instrument_identity(self) -> None:
        _, violations = one(valid_sample(instrument="rogue-device"))
        self.assertEqual(violations[0]["rule"], "instrument.registered")

    def test_instrument_format(self) -> None:
        _, violations = one(valid_sample(instrument="bad id!"))
        self.assertEqual(violations[0]["rule"], "format")

    def test_duplicate_sample_key_within_batch(self) -> None:
        rows = [valid_sample(), valid_sample(response=0.5)]
        parsed, violations = validate_batch(rows, INSTRUMENTS)
        self.assertEqual(parsed, ())
        self.assertTrue(any(v["rule"] == "duplicate" and v["field"].endswith("sample_key")
                            for v in violations))

    def test_duplicate_sample_key_against_stored(self) -> None:
        _, violations = one(valid_sample(), existing={"S-001"})
        self.assertEqual(violations[0]["rule"], "duplicate")

    def test_identical_content_distinct_keys_is_duplicate(self) -> None:
        rows = [valid_sample(), valid_sample(sample_key="S-002")]
        _, violations = validate_batch(rows, INSTRUMENTS)
        self.assertTrue(any(v["rule"] == "duplicate" for v in violations))

    def test_unknown_field_rejected(self) -> None:
        _, violations = one(valid_sample(extra=1))
        self.assertEqual(violations[0]["rule"], "unknown_field")

    def test_batch_collects_all_problems_before_rejecting(self) -> None:
        rows = [
            valid_sample(),
            valid_sample(sample_key="S-002", response="bad", instrument="x" * 100),
            valid_sample(sample_key="S-003", response=5.0),
        ]
        parsed, violations = validate_batch(rows, INSTRUMENTS)
        self.assertEqual(parsed, ())
        fields = {v["field"] for v in violations}
        self.assertIn("measurements[1].response", fields)
        self.assertIn("measurements[1].instrument", fields)
        self.assertIn("measurements[2].response", fields)

    def test_non_array_batch(self) -> None:
        _, violations = validate_batch({"sample_key": "S-001"}, INSTRUMENTS)
        self.assertEqual(violations[0]["rule"], "type.array")
        _, violations = validate_batch([], INSTRUMENTS)
        self.assertEqual(violations[0]["rule"], "required")

    def test_auto_sample_key_is_stable(self) -> None:
        raw = {k: v for k, v in valid_sample().items() if k != "sample_key"}
        parsed_first, violations = validate_batch([raw], INSTRUMENTS, auto_sample_key=True)
        parsed_second, _ = validate_batch([raw], INSTRUMENTS, auto_sample_key=True)
        self.assertEqual(violations, [])
        self.assertEqual(parsed_first[0].sample_key, parsed_second[0].sample_key)


if __name__ == "__main__":
    unittest.main()
