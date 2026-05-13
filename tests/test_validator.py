"""
RescueNet — Validator Unit Tests
WHO ETAT vital sign boundary cases.
These tests are your answer when judges ask "how do you know it's correct?"
"""

import pytest
from core.validator import (
    DeterministicValidator,
    TriageLevel,
    VitalSigns,
)

v = DeterministicValidator.__new__(DeterministicValidator)
v._limits = {}
v._dosage_pattern = v._compile_dosage_pattern(v)
validator = DeterministicValidator.__new__(DeterministicValidator)
validator._limits = {
    "blocked_dosages": ["10mg/kg", "20mg/kg"],
    "approved_protocols": ["WHO_ETAT_2016"],
}
validator._dosage_pattern = [r"\b10\s*mg/kg\b", r"\b20\s*mg/kg\b"]


# ── Vital sign classification ──────────────────────────────────────────────
class TestVitalClassification:

    def _check(self, **kwargs) -> TriageLevel:
        result = DeterministicValidator().check_vitals(VitalSigns(**kwargs))
        return result.deterministic_level

    # HR boundaries
    def test_hr_below_60_is_red(self):
        assert self._check(hr=55) == TriageLevel.RED

    def test_hr_above_120_is_red(self):
        assert self._check(hr=125) == TriageLevel.RED

    def test_hr_normal_alone_is_green(self):
        assert self._check(hr=80) == TriageLevel.GREEN

    # RR boundaries
    def test_rr_below_10_is_red(self):
        assert self._check(rr=8) == TriageLevel.RED

    def test_rr_above_30_is_red(self):
        assert self._check(rr=32) == TriageLevel.RED

    def test_rr_normal_alone_is_green(self):
        assert self._check(rr=16) == TriageLevel.GREEN

    # BP boundaries
    def test_bp_below_90_is_red(self):
        assert self._check(bp_sys=85) == TriageLevel.RED

    def test_bp_exactly_90_is_yellow(self):
        assert self._check(bp_sys=90) == TriageLevel.YELLOW

    def test_bp_normal_is_green(self):
        assert self._check(bp_sys=120) == TriageLevel.GREEN

    # SpO2 boundaries
    def test_spo2_below_90_is_red(self):
        assert self._check(spo2=88) == TriageLevel.RED

    def test_spo2_90_to_94_is_yellow(self):
        result = DeterministicValidator().check_vitals(VitalSigns(spo2=92))
        assert result.deterministic_level == TriageLevel.YELLOW

    # GCS boundaries
    def test_gcs_8_is_red(self):
        assert self._check(gcs=8) == TriageLevel.RED

    def test_gcs_9_is_yellow_or_better(self):
        result = DeterministicValidator().check_vitals(VitalSigns(gcs=9))
        assert result.deterministic_level in (TriageLevel.YELLOW, TriageLevel.GREEN)

    def test_gcs_15_is_green(self):
        assert self._check(gcs=15) == TriageLevel.GREEN

    # Multi-vital RED
    def test_crush_injury_vitals_is_red(self):
        """BP 90/60 + HR 115 = hypovolemic shock → RED"""
        result = DeterministicValidator().check_vitals(
            VitalSigns(hr=115, bp_sys=85, rr=26, spo2=94, gcs=13)
        )
        assert result.deterministic_level == TriageLevel.RED
        assert result.confidence >= 0.70

    def test_stable_vitals_is_green(self):
        result = DeterministicValidator().check_vitals(
            VitalSigns(hr=78, rr=16, bp_sys=120, spo2=98, gcs=15)
        )
        assert result.deterministic_level == TriageLevel.GREEN

    # Missing vitals
    def test_no_vitals_is_yellow_safe_default(self):
        """No vitals → safe default YELLOW, low confidence"""
        result = DeterministicValidator().check_vitals(VitalSigns())
        assert result.deterministic_level == TriageLevel.YELLOW
        assert result.confidence <= 0.50
        assert len(result.missing_vitals) == 5

    def test_partial_vitals_penalises_confidence(self):
        result_full    = DeterministicValidator().check_vitals(VitalSigns(hr=80, rr=16, bp_sys=120, spo2=98, gcs=15))
        result_partial = DeterministicValidator().check_vitals(VitalSigns(hr=80))
        assert result_partial.confidence < result_full.confidence


# ── Alert generation ───────────────────────────────────────────────────────
class TestAlerts:

    def test_sepsis_shock_alert_on_high_hr(self):
        result = DeterministicValidator().check_vitals(VitalSigns(hr=130))
        assert any("Sepsis" in a or "Shock" in a for a in result.triggered_alerts)

    def test_respiratory_failure_alert_on_low_rr(self):
        result = DeterministicValidator().check_vitals(VitalSigns(rr=8))
        assert any("Respiratory" in a for a in result.triggered_alerts)

    def test_hypovolemic_shock_alert_on_low_bp(self):
        result = DeterministicValidator().check_vitals(VitalSigns(bp_sys=80))
        assert any("Hypovolemic" in a or "Shock" in a for a in result.triggered_alerts)


# ── Specialist hard-stops ──────────────────────────────────────────────────
class TestSpecialistHardStop:

    def _check(self, text: str) -> bool:
        req, _ = DeterministicValidator().check_specialist_required(text)
        return req

    def test_obstetric_blocked(self):
        assert self._check("patient in active labour, 38 weeks") is True

    def test_psychiatric_blocked(self):
        assert self._check("patient expressing suicidal ideation") is True

    def test_pediatric_dosing_blocked(self):
        assert self._check("weight-based dosing for child 12kg") is True

    def test_normal_trauma_not_blocked(self):
        assert self._check("crush injury to lower limb, BP 85 systolic") is False


# ── Dosage guard ───────────────────────────────────────────────────────────
class TestDosageGuard:

    def test_blocked_dosage_flagged(self):
        val = DeterministicValidator()
        safe, blocked = val.validate_dosage("Administer 10mg/kg morphine IV")
        assert safe is False
        assert len(blocked) > 0

    def test_safe_text_passes(self):
        val = DeterministicValidator()
        safe, blocked = val.validate_dosage("Ensure airway, control haemorrhage")
        assert safe is True
        assert blocked == []


# ── LLM cross-check ────────────────────────────────────────────────────────
class TestCrossCheck:

    def test_matching_levels_no_discrepancy(self):
        val = DeterministicValidator()
        result = val.check_vitals(VitalSigns(hr=130))  # RED
        result = val.cross_check_llm(result, "RED")
        assert result.discrepancy is False

    def test_mismatch_flags_discrepancy(self):
        val = DeterministicValidator()
        result = val.check_vitals(VitalSigns(hr=130))  # RED
        result = val.cross_check_llm(result, "GREEN")
        assert result.discrepancy is True
        assert "DISCREPANCY" in result.discrepancy_detail

    def test_red_cannot_be_downgraded(self):
        """Core safety invariant: RED is never overridden by LLM"""
        val = DeterministicValidator()
        result = val.check_vitals(VitalSigns(hr=130))
        result = val.cross_check_llm(result, "GREEN")
        assert result.deterministic_level == TriageLevel.RED
        assert "RED LOCK" in result.discrepancy_detail
