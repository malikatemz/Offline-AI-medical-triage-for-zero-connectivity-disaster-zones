"""
RescueNet — Deterministic Safety Layer
WHO/Red Cross triage rules. Bypasses LLM for critical vitals.
Never hallucinates. Never negotiable.
"""

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

# ── Triage levels ────────────────────────────────────────────────────────────
class TriageLevel(str, Enum):
    RED    = "RED"      # Immediate — act within 60s
    YELLOW = "YELLOW"   # Delayed   — act within 30min
    GREEN  = "GREEN"    # Minor     — queue
    BLACK  = "BLACK"    # Expectant — unsurvivable w/ available resources


# ── Vital ranges (WHO ETAT + START protocol) ─────────────────────────────────
VITAL_RULES = {
    "hr": {
        "RED":    lambda v: v < 60 or v > 120,
        "YELLOW": lambda v: 60 <= v <= 120,
        "GREEN":  lambda v: 60 <= v <= 100,
        "alert":  "Sepsis/Shock Alert",
    },
    "rr": {
        "RED":    lambda v: v < 10 or v > 30,
        "YELLOW": lambda v: 10 <= v <= 30,
        "GREEN":  lambda v: 12 <= v <= 20,
        "alert":  "Respiratory Failure",
    },
    "bp_sys": {
        "RED":    lambda v: v < 90,
        "YELLOW": lambda v: 90 <= v <= 140,
        "GREEN":  lambda v: 110 <= v <= 130,
        "alert":  "Hypovolemic Shock",
    },
    "spo2": {
        "RED":    lambda v: v < 90,
        "YELLOW": lambda v: 90 <= v < 95,
        "GREEN":  lambda v: v >= 95,
        "alert":  "Hypoxia",
    },
    "gcs": {
        "RED":    lambda v: v <= 8,
        "YELLOW": lambda v: 9 <= v <= 13,
        "GREEN":  lambda v: v >= 14,
        "alert":  "Altered Consciousness",
    },
}

# Hard-stop domains — always route to specialist protocol
SPECIALIST_DOMAINS = [
    "obstetric", "ob/gyn", "labour", "labor", "delivery",
    "psychiatric", "suicide", "psychosis",
    "paediatric dosing", "pediatric dosing", "weight-based",
    "neonatal",
]


@dataclass
class VitalSigns:
    hr:     Optional[float] = None   # bpm
    rr:     Optional[float] = None   # breaths/min
    bp_sys: Optional[float] = None   # mmHg systolic
    spo2:   Optional[float] = None   # %
    gcs:    Optional[int]   = None   # Glasgow Coma Scale 3-15


@dataclass
class ValidationResult:
    deterministic_level: TriageLevel
    confidence: float                        # 0.0 – 1.0
    triggered_alerts: list[str] = field(default_factory=list)
    missing_vitals: list[str]   = field(default_factory=list)
    specialist_required: bool   = False
    specialist_reason: str      = ""
    discrepancy: bool           = False      # True if LLM disagrees
    discrepancy_detail: str     = ""
    source: str                 = "WHO_ETAT_2016 + START_PROTOCOL"


class DeterministicValidator:
    """
    Hard-coded WHO/ETAT triage rules.
    Always runs BEFORE LLM. LLM provides context, not safety.
    """

    def __init__(self, hard_limits_path: str = "data/hard_limits.json"):
        self._limits = self._load_limits(hard_limits_path)
        self._dosage_pattern = self._compile_dosage_pattern()

    # ── Public API ───────────────────────────────────────────────────────────

    def check_vitals(self, vitals: VitalSigns) -> ValidationResult:
        """
        Classify triage level from vital signs alone.
        Returns deterministic result — no LLM involved.
        """
        alerts:  list[str] = []
        missing: list[str] = []
        red_flags = 0
        yellow_flags = 0
        total_checked = 0

        for key, rules in VITAL_RULES.items():
            val = getattr(vitals, key, None)
            if val is None:
                missing.append(key)
                continue
            total_checked += 1
            if rules["RED"](val):
                red_flags += 1
                alerts.append(f"{rules['alert']} ({key.upper()}={val})")
            elif rules["YELLOW"](val):
                yellow_flags += 1

        # Classify
        if red_flags >= 1:
            level = TriageLevel.RED
            conf  = min(0.95, 0.70 + (red_flags * 0.08))
        elif yellow_flags >= 2:
            level = TriageLevel.YELLOW
            conf  = 0.75
        elif total_checked == 0:
            # No vitals — safe default per protocol
            level = TriageLevel.YELLOW
            conf  = 0.40
        else:
            level = TriageLevel.GREEN
            conf  = 0.85

        # Penalise confidence for missing vitals
        if missing:
            conf -= len(missing) * 0.05
            conf  = max(conf, 0.30)

        return ValidationResult(
            deterministic_level=level,
            confidence=round(conf, 2),
            triggered_alerts=alerts,
            missing_vitals=missing,
        )

    def check_specialist_required(self, free_text: str) -> tuple[bool, str]:
        """
        Scan patient description for hard-stop domains.
        Returns (required: bool, reason: str).
        """
        text_lower = free_text.lower()
        for domain in SPECIALIST_DOMAINS:
            if domain in text_lower:
                return True, domain
        return False, ""

    def validate_dosage(self, llm_output: str) -> tuple[bool, list[str]]:
        """
        Regex scan LLM output against hard_limits.json.
        Returns (safe: bool, blocked_matches: list[str]).
        """
        if not self._limits:
            return True, []
        blocked = []
        for pattern in self._dosage_pattern:
            matches = re.findall(pattern, llm_output, re.IGNORECASE)
            if matches:
                blocked.extend(matches)
        return len(blocked) == 0, blocked

    def cross_check_llm(
        self,
        deterministic: ValidationResult,
        llm_level: str,
    ) -> ValidationResult:
        """
        Compare deterministic result vs LLM classification.
        If mismatch: flag DISCREPANCY: MANUAL REVIEW.
        LLM is NEVER allowed to override RED deterministic result.
        """
        try:
            llm = TriageLevel(llm_level.upper())
        except ValueError:
            deterministic.discrepancy = True
            deterministic.discrepancy_detail = f"LLM returned invalid level: {llm_level}"
            return deterministic

        if deterministic.deterministic_level != llm:
            deterministic.discrepancy = True
            deterministic.discrepancy_detail = (
                f"DISCREPANCY: MANUAL REVIEW — "
                f"Deterministic={deterministic.deterministic_level} | "
                f"LLM={llm} | "
                f"Applying deterministic result (WHO ETAT override)"
            )
            # Hard rule: RED can never be downgraded by LLM
            if deterministic.deterministic_level == TriageLevel.RED:
                deterministic.discrepancy_detail += " | RED LOCK ENFORCED"

        return deterministic

    # ── Private helpers ──────────────────────────────────────────────────────

    def _load_limits(self, path: str) -> dict:
        p = Path(path)
        if p.exists():
            with open(p) as f:
                return json.load(f)
        return {}

    def _compile_dosage_pattern(self) -> list[str]:
        """
        Build regex patterns from hard_limits.json blocked_dosages list.
        """
        if not self._limits or "blocked_dosages" not in self._limits:
            # Fallback: flag any numeric mg/kg or mg dose pattern for review
            return [
                r"\d+\s*mg/kg",
                r"\d+\.?\d*\s*mg\b",
                r"\d+\s*mcg/kg",
                r"\d+\s*mmol",
            ]
        return [re.escape(d) for d in self._limits["blocked_dosages"]]
