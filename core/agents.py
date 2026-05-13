"""
RescueNet — LangGraph Multi-Agent Pipeline
Triage Agent → Protocol Agent → Logger Agent
Deterministic validator gates every node.
"""

import json
import time
from typing import Any, TypedDict

import httpx
from langgraph.graph import END, StateGraph

from core.validator import DeterministicValidator, TriageLevel, VitalSigns

OLLAMA_URL  = "http://ollama:11434"
GEMMA_MODEL = "gemma4:2b"
QDRANT_URL  = "http://qdrant:6333"
CONTEXT_WIN = 2048

validator = DeterministicValidator("data/hard_limits.json")


# ── Shared state ─────────────────────────────────────────────────────────────
class TriageState(TypedDict):
    # Input
    patient_id:    str
    description:   str
    vitals_raw:    dict
    scene_context: str

    # Triage node output
    deterministic_level: str
    deterministic_conf:  float
    triggered_alerts:    list[str]
    missing_vitals:      list[str]

    # Protocol node output
    rag_chunks:          list[str]
    llm_level:           str
    llm_actions:         list[str]
    protocol_ref:        str
    dosage_safe:         bool
    blocked_dosages:     list[str]

    # Cross-check
    discrepancy:         bool
    discrepancy_detail:  str
    final_level:         str
    final_actions:       list[str]

    # Logger node output
    logged:              bool
    log_id:              str
    latency_ms:          float

    # Meta
    t0:                  float
    error:               str


# ── Node 1: Triage Agent ──────────────────────────────────────────────────────
async def triage_agent(state: TriageState) -> TriageState:
    """
    Deterministic vital sign classification.
    No LLM. Pure rule engine. Always runs first.
    """
    vitals = VitalSigns(**state["vitals_raw"])
    result = validator.check_vitals(vitals)

    # Specialist hard-stop
    spec_req, spec_reason = validator.check_specialist_required(state["description"])
    if spec_req:
        return {
            **state,
            "deterministic_level": "SPECIALIST",
            "deterministic_conf":  1.0,
            "triggered_alerts":    [f"Hard-stop: {spec_reason}"],
            "missing_vitals":      [],
            "final_level":         "SPECIALIST",
            "final_actions":       ["Use physical specialist manual immediately"],
            "discrepancy":         False,
            "discrepancy_detail":  "",
        }

    return {
        **state,
        "deterministic_level": result.deterministic_level.value,
        "deterministic_conf":  result.confidence,
        "triggered_alerts":    result.triggered_alerts,
        "missing_vitals":      result.missing_vitals,
    }


# ── Node 2: Protocol Agent ────────────────────────────────────────────────────
async def protocol_agent(state: TriageState) -> TriageState:
    """
    1. Fetch RAG context from Qdrant
    2. Call Gemma 4 for contextualised protocol
    3. Dosage guard on output
    4. Cross-check vs deterministic
    """
    # Skip LLM if specialist hard-stop or RED with high confidence
    if state["deterministic_level"] == "SPECIALIST":
        return state
    if (state["deterministic_level"] == "RED"
            and state["deterministic_conf"] >= 0.85):
        return {
            **state,
            "llm_level":    "RED",
            "llm_actions":  _default_actions(state["deterministic_level"],
                                             state["triggered_alerts"]),
            "protocol_ref": "WHO_ETAT_2016_DETERMINISTIC_OVERRIDE",
            "dosage_safe":  True,
            "blocked_dosages": [],
            "rag_chunks":   [],
        }

    # RAG retrieval
    chunks = await _qdrant_search(
        f"{state['deterministic_level']} {state['description']}"
    )
    rag_text = "\n".join(chunks) or "No RAG context — base training only"

    # Gemma 4 call
    prompt = _protocol_prompt(state, rag_text)
    llm_raw = await _ollama_call(prompt)

    # Parse LLM output
    llm_level   = _parse_level(llm_raw)
    llm_actions = _parse_actions(llm_raw)
    protocol_ref = _parse_protocol(llm_raw)

    # Dosage guard
    safe, blocked = validator.validate_dosage(llm_raw)
    if not safe:
        llm_actions.append(
            f"⚠ DOSAGE BLOCKED — confirm with physical manual: {blocked}"
        )

    # Cross-check
    from core.validator import ValidationResult
    det_result = ValidationResult(
        deterministic_level=TriageLevel(state["deterministic_level"]),
        confidence=state["deterministic_conf"],
        triggered_alerts=state["triggered_alerts"],
        missing_vitals=state["missing_vitals"],
    )
    det_result = validator.cross_check_llm(det_result, llm_level)

    return {
        **state,
        "rag_chunks":       chunks,
        "llm_level":        llm_level,
        "llm_actions":      llm_actions,
        "protocol_ref":     protocol_ref,
        "dosage_safe":      safe,
        "blocked_dosages":  blocked,
        "discrepancy":      det_result.discrepancy,
        "discrepancy_detail": det_result.discrepancy_detail,
        "final_level":      det_result.deterministic_level.value,
        "final_actions":    llm_actions or _default_actions(
            state["deterministic_level"], state["triggered_alerts"]
        ),
    }


# ── Node 3: Logger Agent ──────────────────────────────────────────────────────
async def logger_agent(state: TriageState) -> TriageState:
    """
    Write triage record to local SQLite (offline).
    Queues for PostgreSQL sync when connectivity restored.
    """
    import sqlite3, uuid
    log_id = str(uuid.uuid4())[:8]
    latency = round((time.time() - state["t0"]) * 1000, 1)

    record = {
        "log_id":            log_id,
        "patient_id":        state["patient_id"],
        "final_level":       state.get("final_level", "UNKNOWN"),
        "deterministic_level": state.get("deterministic_level"),
        "discrepancy":       state.get("discrepancy", False),
        "discrepancy_detail": state.get("discrepancy_detail", ""),
        "triggered_alerts":  json.dumps(state.get("triggered_alerts", [])),
        "final_actions":     json.dumps(state.get("final_actions", [])),
        "protocol_ref":      state.get("protocol_ref", ""),
        "latency_ms":        latency,
        "synced":            0,         # 0 = local only, 1 = synced to Postgres
        "timestamp":         time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    try:
        conn = sqlite3.connect("data/rescuenet_local.db")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS triage_log (
                log_id TEXT PRIMARY KEY,
                patient_id TEXT,
                final_level TEXT,
                deterministic_level TEXT,
                discrepancy INTEGER,
                discrepancy_detail TEXT,
                triggered_alerts TEXT,
                final_actions TEXT,
                protocol_ref TEXT,
                latency_ms REAL,
                synced INTEGER DEFAULT 0,
                timestamp TEXT
            )
        """)
        conn.execute(
            "INSERT INTO triage_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            list(record.values()),
        )
        conn.commit()
        conn.close()
        logged = True
    except Exception as e:
        logged = False
        record["error"] = str(e)

    return {**state, "logged": logged, "log_id": log_id, "latency_ms": latency}


# ── Routing: skip to logger if specialist hard-stop ──────────────────────────
def route_after_triage(state: TriageState) -> str:
    if state.get("deterministic_level") == "SPECIALIST":
        return "logger"
    return "protocol"


# ── Graph assembly ────────────────────────────────────────────────────────────
def build_graph() -> StateGraph:
    g = StateGraph(TriageState)

    g.add_node("triage",   triage_agent)
    g.add_node("protocol", protocol_agent)
    g.add_node("logger",   logger_agent)

    g.set_entry_point("triage")
    g.add_conditional_edges("triage", route_after_triage,
                            {"protocol": "protocol", "logger": "logger"})
    g.add_edge("protocol", "logger")
    g.add_edge("logger", END)

    return g.compile()


GRAPH = build_graph()


async def run_triage(
    patient_id: str,
    description: str,
    vitals: dict,
    scene_context: str = "",
) -> TriageState:
    initial: TriageState = {
        "patient_id":    patient_id,
        "description":   description,
        "vitals_raw":    vitals,
        "scene_context": scene_context,
        "t0":            time.time(),
        # defaults
        "deterministic_level": "", "deterministic_conf": 0.0,
        "triggered_alerts": [],    "missing_vitals": [],
        "rag_chunks": [],          "llm_level": "",
        "llm_actions": [],         "protocol_ref": "",
        "dosage_safe": True,       "blocked_dosages": [],
        "discrepancy": False,      "discrepancy_detail": "",
        "final_level": "",         "final_actions": [],
        "logged": False,           "log_id": "",
        "latency_ms": 0.0,         "error": "",
    }
    return await GRAPH.ainvoke(initial)


# ── Helpers ───────────────────────────────────────────────────────────────────
async def _qdrant_search(query: str, limit: int = 3) -> list[str]:
    try:
        from core.embedder import embed
        vector = await embed(query)
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.post(
                f"{QDRANT_URL}/collections/rescuenet_protocols/points/search",
                json={
                    "vector": vector,
                    "limit": limit,
                    "with_payload": True,
                    "score_threshold": 0.4,
                },
            )
            hits = r.json().get("result", [])
            return [h["payload"].get("text", "") for h in hits]
    except Exception:
        return []


async def _ollama_call(prompt: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": GEMMA_MODEL, "prompt": prompt,
                      "stream": False, "options": {"num_ctx": CONTEXT_WIN}},
            )
            return r.json().get("response", "")
    except Exception:
        return ""


def _protocol_prompt(state: TriageState, rag: str) -> str:
    return f"""### RescueNet Protocol Agent
Deterministic triage: {state['deterministic_level']} (conf: {state['deterministic_conf']})
Alerts: {', '.join(state['triggered_alerts']) or 'none'}
Patient: {state['description']}
Scene: {state['scene_context']}
Protocol context: {rag}

Output ONLY valid JSON — no preamble:
{{"triage_level":"","confidence":0.0,"immediate_actions":[],"protocol_ref":"","flag":""}}"""


def _parse_level(text: str) -> str:
    for l in ("RED", "YELLOW", "GREEN", "BLACK"):
        if l in text.upper():
            return l
    return "YELLOW"


def _parse_actions(text: str) -> list[str]:
    try:
        start = text.index("{")
        data = json.loads(text[start:text.rindex("}") + 1])
        return data.get("immediate_actions", [])
    except Exception:
        return []


def _parse_protocol(text: str) -> str:
    try:
        start = text.index("{")
        data = json.loads(text[start:text.rindex("}") + 1])
        return data.get("protocol_ref", "WHO_ETAT_2016")
    except Exception:
        return "WHO_ETAT_2016"


def _default_actions(level: str, alerts: list[str]) -> list[str]:
    defaults = {
        "RED":    ["Ensure airway", "Control haemorrhage", "IV access — 2 large bore", "Alert surgeon NOW"],
        "YELLOW": ["Monitor vitals q5min", "IV access", "Pain assessment"],
        "GREEN":  ["Walking wounded — queue", "Basic wound care", "Reassess in 30min"],
        "BLACK":  ["Comfort measures only", "Document time of decision", "Chaplain/support"],
    }
    base = defaults.get(level, ["Follow physical manual"])
    return ([f"⚠ {alerts[0]}"] + base) if alerts else base
