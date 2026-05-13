"""
RescueNet — Inference API
FastAPI + SSE streaming + Redis prefetch + hardware telemetry
"""

import asyncio
import json
import socket
import time
from typing import AsyncGenerator, Optional

import httpx
import psutil
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.validator import (
    DeterministicValidator,
    TriageLevel,
    ValidationResult,
    VitalSigns,
)

# ── Config ───────────────────────────────────────────────────────────────────
OLLAMA_URL    = "http://ollama:11434"
GEMMA_MODEL   = "gemma4:2b"
REDIS_URL     = "redis://redis:6379"
QDRANT_URL    = "http://qdrant:6333"
CONTEXT_WIN   = 2048          # KV-cache optimised window
TOP_PROTOCOLS = 20            # Redis prefetch count
STREAM_CHUNK  = 64            # SSE token chunk size

app = FastAPI(title="RescueNet API", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

validator = DeterministicValidator("data/hard_limits.json")
_redis: Optional[aioredis.Redis] = None


# ── Pydantic models ──────────────────────────────────────────────────────────
class VitalsInput(BaseModel):
    hr:       Optional[float] = Field(None, description="Heart rate bpm")
    rr:       Optional[float] = Field(None, description="Respiratory rate br/min")
    bp_sys:   Optional[float] = Field(None, description="Systolic BP mmHg")
    spo2:     Optional[float] = Field(None, description="SpO2 %")
    gcs:      Optional[int]   = Field(None, description="Glasgow Coma Scale 3-15")


class PatientData(BaseModel):
    patient_id:   str
    description:  str               # Free-text symptom description
    vitals:       VitalsInput
    scene_context: str = ""


class TriageResponse(BaseModel):
    triage_level:        str
    confidence:          float
    triggered_alerts:    list[str]
    missing_vitals:      list[str]
    immediate_actions:   list[str]
    protocol_ref:        str
    discrepancy:         bool
    discrepancy_detail:  str
    specialist_required: bool
    deterministic_check: str        # "PASSED" | "DISCREPANCY: MANUAL REVIEW"
    latency_ms:          float


# ── Redis helpers ────────────────────────────────────────────────────────────
async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = await aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


async def prefetch_critical_protocols():
    """
    On startup: cache top 20 WHO life-threatening protocols → 0ms retrieval.
    """
    r = await get_redis()
    protocols = [
        "WHO_ETAT_septic_shock", "WHO_ETAT_respiratory_failure",
        "WHO_ETAT_severe_dehydration", "WHO_ETAT_unconscious_child",
        "START_RED_crush_injury", "START_RED_traumatic_arrest",
        "START_RED_tension_pneumothorax", "START_RED_haemorrhage_control",
        "SALT_BLACK_criteria", "WHO_ETAT_malaria_severe",
        "WHO_ETAT_meningitis", "WHO_ETAT_cholera_IV",
        "RED_CROSS_burns_rule_of_nines", "RED_CROSS_amputation_field",
        "RED_CROSS_blast_injury", "RED_CROSS_hypothermia",
        "RED_CROSS_drowning_resus", "WHO_ETAT_severe_anaemia",
        "WHO_ETAT_hypoglycaemia", "WHO_ETAT_seizure_status",
    ]
    pipe = r.pipeline()
    for p in protocols:
        pipe.set(f"protocol:{p}", f"cached:{p}", ex=86400)  # 24h TTL
    await pipe.execute()


@app.on_event("startup")
async def startup():
    await prefetch_critical_protocols()


# ── Core triage endpoint (non-streaming) ─────────────────────────────────────
@app.post("/triage", response_model=TriageResponse)
async def triage(patient: PatientData):
    t0 = time.perf_counter()

    # 1. Specialist hard-stop check
    spec_required, spec_reason = validator.check_specialist_required(patient.description)
    if spec_required:
        return TriageResponse(
            triage_level="SPECIALIST",
            confidence=1.0,
            triggered_alerts=[f"Specialist domain: {spec_reason}"],
            missing_vitals=[],
            immediate_actions=["Use physical specialist manual immediately"],
            protocol_ref="HARD_STOP",
            discrepancy=False,
            discrepancy_detail="",
            specialist_required=True,
            deterministic_check="PASSED",
            latency_ms=round((time.perf_counter() - t0) * 1000, 1),
        )

    # 2. Deterministic vital sign classification
    vitals = VitalSigns(**patient.vitals.dict())
    det_result: ValidationResult = validator.check_vitals(vitals)

    # 3. Redis: check cached protocol
    r = await get_redis()
    cached_key = f"protocol:WHO_ETAT_{det_result.deterministic_level.value.lower()}"
    cached_protocol = await r.get(cached_key) or "WHO_ETAT_2016"

    # 4. Build LLM prompt (only runs if not RED with high confidence)
    llm_level = det_result.deterministic_level.value  # default = deterministic
    immediate_actions = _default_actions(det_result)

    if det_result.confidence < 0.90:
        rag_context = await _fetch_rag_context(patient.description, det_result)
        llm_response = await _call_ollama(patient, det_result, rag_context)
        llm_level = _parse_llm_level(llm_response)
        immediate_actions = _parse_llm_actions(llm_response) or immediate_actions

        # 5. Dosage guard on LLM output
        safe, blocked = validator.validate_dosage(llm_response)
        if not safe:
            immediate_actions.append(f"⚠ DOSAGE BLOCKED: {blocked} — confirm with physical manual")

    # 6. Cross-check: deterministic vs LLM
    det_result = validator.cross_check_llm(det_result, llm_level)

    latency = round((time.perf_counter() - t0) * 1000, 1)
    return TriageResponse(
        triage_level=det_result.deterministic_level.value,
        confidence=det_result.confidence,
        triggered_alerts=det_result.triggered_alerts,
        missing_vitals=det_result.missing_vitals,
        immediate_actions=immediate_actions,
        protocol_ref=cached_protocol,
        discrepancy=det_result.discrepancy,
        discrepancy_detail=det_result.discrepancy_detail,
        specialist_required=False,
        deterministic_check="DISCREPANCY: MANUAL REVIEW" if det_result.discrepancy else "PASSED",
        latency_ms=latency,
    )


# ── Streaming triage endpoint (SSE) ─────────────────────────────────────────
@app.post("/triage/stream")
async def stream_triage(patient: PatientData):
    """
    SSE stream: deterministic check fires first (instant),
    then Gemma 4 tokens stream in real-time.
    """
    async def event_stream() -> AsyncGenerator[str, None]:
        t0 = time.perf_counter()

        # Immediate: deterministic check — no waiting
        vitals = VitalSigns(**patient.vitals.dict())
        det = validator.check_vitals(vitals)

        yield _sse("deterministic", {
            "level": det.deterministic_level.value,
            "alerts": det.triggered_alerts,
            "confidence": det.confidence,
            "check": "PASSED",
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        })

        # If RED + high confidence: no need to wait for LLM
        if det.deterministic_level == TriageLevel.RED and det.confidence >= 0.85:
            yield _sse("final", {
                "source": "DETERMINISTIC_OVERRIDE",
                "level": "RED",
                "actions": _default_actions(det),
                "total_ms": round((time.perf_counter() - t0) * 1000, 1),
            })
            return

        # Stream Gemma 4 tokens
        rag_context = await _fetch_rag_context(patient.description, det)
        prompt = _build_prompt(patient, det, rag_context)

        async with httpx.AsyncClient(timeout=30) as client:
            async with client.stream(
                "POST",
                f"{OLLAMA_URL}/api/generate",
                json={"model": GEMMA_MODEL, "prompt": prompt, "stream": True,
                      "options": {"num_ctx": CONTEXT_WIN}},
            ) as resp:
                buffer = ""
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    token = chunk.get("response", "")
                    buffer += token
                    yield _sse("token", {"text": token})
                    if chunk.get("done"):
                        break

        # Final: cross-check + dosage guard
        llm_level = _parse_llm_level(buffer)
        safe, blocked = validator.validate_dosage(buffer)
        det = validator.cross_check_llm(det, llm_level)

        yield _sse("final", {
            "level": det.deterministic_level.value,
            "deterministic_check": "DISCREPANCY: MANUAL REVIEW" if det.discrepancy else "PASSED",
            "dosage_safe": safe,
            "blocked_dosages": blocked,
            "total_ms": round((time.perf_counter() - t0) * 1000, 1),
        })

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ── System health endpoint (§3 demo dashboard) ───────────────────────────────
@app.get("/system/health")
async def system_health():
    """
    Hardware telemetry for demo wow-factor dashboard.
    Offline badge: if DNS fails → C-BLACKOUT state.
    """
    # Connectivity check
    offline = True
    try:
        socket.setdefaulttimeout(1)
        socket.gethostbyname("8.8.8.8")
        offline = False
    except OSError:
        pass

    # CPU temp (Linux /sys)
    cpu_temp = None
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            cpu_temp = round(int(f.read()) / 1000, 1)
    except FileNotFoundError:
        temps = psutil.sensors_temperatures()
        if temps:
            first = next(iter(temps.values()))
            cpu_temp = round(first[0].current, 1) if first else None

    mem = psutil.virtual_memory()
    cpu = psutil.cpu_percent(interval=0.1)

    return {
        "connectivity":  "C-BLACKOUT" if offline else "ONLINE",
        "offline_badge": offline,
        "cpu_percent":   cpu,
        "cpu_temp_c":    cpu_temp,
        "ram_used_gb":   round(mem.used  / 1e9, 2),
        "ram_total_gb":  round(mem.total / 1e9, 2),
        "ram_percent":   mem.percent,
        "model_loaded":  await _check_ollama_loaded(),
        "qdrant_ready":  await _check_qdrant(),
        "redis_ready":   await _check_redis(),
    }


# ── RAG context fetch ────────────────────────────────────────────────────────
async def _fetch_rag_context(description: str, det: ValidationResult) -> str:
    query = f"triage {det.deterministic_level.value} {description}"
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.post(
                f"{QDRANT_URL}/collections/rescuenet_protocols/points/search",
                json={"vector": [0.0] * 384, "limit": 3,  # placeholder vector
                      "with_payload": True, "query_vector": query},
            )
            hits = r.json().get("result", [])
            return "\n".join(h["payload"].get("text", "") for h in hits)
    except Exception:
        return ""   # Offline: no RAG — base training only


# ── Prompt builder ───────────────────────────────────────────────────────────
def _build_prompt(patient: PatientData, det: ValidationResult, rag: str) -> str:
    return f"""### RescueNet Triage AI
Deterministic classification: {det.deterministic_level.value} (confidence: {det.confidence})
Alerts: {', '.join(det.triggered_alerts) or 'none'}
Missing vitals: {', '.join(det.missing_vitals) or 'none'}

Patient: {patient.description}
Scene: {patient.scene_context}
Vitals: HR={patient.vitals.hr} RR={patient.vitals.rr} BP={patient.vitals.bp_sys} SpO2={patient.vitals.spo2} GCS={patient.vitals.gcs}

Protocol context:
{rag or 'No RAG context — base WHO ETAT training only'}

Output JSON only:
{{"triage_level": "", "confidence": 0.0, "immediate_actions": [], "protocol_ref": "", "flag": ""}}"""


# ── Helpers ──────────────────────────────────────────────────────────────────
def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _parse_llm_level(text: str) -> str:
    for level in ("RED", "YELLOW", "GREEN", "BLACK"):
        if level in text.upper():
            return level
    return "YELLOW"   # safe default


def _parse_llm_actions(text: str) -> list[str]:
    try:
        start = text.index("{")
        data  = json.loads(text[start:text.rindex("}") + 1])
        return data.get("immediate_actions", [])
    except Exception:
        return []


def _default_actions(det: ValidationResult) -> list[str]:
    defaults = {
        TriageLevel.RED:    ["Ensure airway", "Control haemorrhage", "IV access — 2 large bore", "Alert surgeon"],
        TriageLevel.YELLOW: ["Monitor vitals q5min", "Establish IV access", "Pain assessment"],
        TriageLevel.GREEN:  ["Walking wounded — queue", "Basic wound care", "Reassess in 30min"],
        TriageLevel.BLACK:  ["Comfort measures only", "Document time", "Chaplain/support"],
    }
    actions = defaults.get(det.deterministic_level, [])
    if det.triggered_alerts:
        actions = [f"⚠ {det.triggered_alerts[0]}"] + actions
    return actions


async def _call_ollama(patient: PatientData, det: ValidationResult, rag: str) -> str:
    prompt = _build_prompt(patient, det, rag)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": GEMMA_MODEL, "prompt": prompt, "stream": False,
                      "options": {"num_ctx": CONTEXT_WIN}},
            )
            return r.json().get("response", "")
    except Exception:
        return ""


async def _check_ollama_loaded() -> bool:
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            r = await client.get(f"{OLLAMA_URL}/api/tags")
            models = [m["name"] for m in r.json().get("models", [])]
            return any(GEMMA_MODEL in m for m in models)
    except Exception:
        return False


async def _check_qdrant() -> bool:
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            r = await client.get(f"{QDRANT_URL}/healthz")
            return r.status_code == 200
    except Exception:
        return False


async def _check_redis() -> bool:
    try:
        r = await get_redis()
        await r.ping()
        return True
    except Exception:
        return False
