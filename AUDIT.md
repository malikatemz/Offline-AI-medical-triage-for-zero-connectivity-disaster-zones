# RescueNet — Architecture Audit Report
**vs. Designed System (RescueNet Medical Edge Appliance v2)**
Date: 2026-05-11

---

## GAP ANALYSIS

| Component | Designed | Repo Status | Severity |
|-----------|----------|-------------|----------|
| `core/validator.py` | Deterministic WHO triage rules | ❌ MISSING | CRITICAL |
| `core/agents.py` | LangGraph 3-agent pipeline | ❌ MISSING | CRITICAL |
| `core/ingest.py` | Qdrant WHO/Red Cross ingestion | ❌ MISSING | CRITICAL |
| `core/sync.py` | SQLite → PostgreSQL delta sync | ❌ MISSING | CRITICAL |
| `api/inference.py` | FastAPI + SSE streaming + Redis | ❌ MISSING | CRITICAL |
| `api/main.py` | FastAPI entry point + lifespan | ❌ MISSING | HIGH |
| `infra/docker-compose.yml` | GPU + mem lock + service priority | ❌ MISSING | HIGH |
| `data/hard_limits.json` | WHO dosage hard limits | ❌ MISSING | HIGH |
| `apps/web/` | CDSS UI (vitals form, not chat) | ❌ MISSING | HIGH |
| `requirements.txt` | All dependencies | ❌ MISSING | MEDIUM |
| `infra/Dockerfile.api` | API container | ❌ MISSING | MEDIUM |
| `README.md` | Competition-ready | ❌ MISSING | MEDIUM |

**Result: 0/12 critical components present in repo.**

---

## FILES GENERATED (this session)

| File | Purpose | Status |
|------|---------|--------|
| `core/validator.py` | Deterministic safety layer — WHO vital rules + dosage guard + LLM cross-check | ✅ GENERATED |
| `core/agents.py` | LangGraph: TriageAgent → ProtocolAgent → LoggerAgent | ✅ GENERATED |
| `core/ingest.py` | Qdrant ingestion — chunk + embed + upsert WHO/Red Cross manuals | ✅ GENERATED |
| `core/sync.py` | Offline sync — SQLite local write, Postgres push on reconnect | ✅ GENERATED |
| `api/inference.py` | FastAPI endpoints: /triage, /triage/stream (SSE), /system/health | ✅ GENERATED |
| `api/main.py` | App entry point, lifespan, CORS | ✅ GENERATED |
| `infra/docker-compose.yml` | nvidia runtime, mem_limit 3.5GB, service priority order | ✅ GENERATED |
| `infra/Dockerfile.api` | API container | ✅ GENERATED |
| `data/hard_limits.json` | WHO dosage limits + vital thresholds | ✅ GENERATED |
| `apps/web/components/TriagePage.jsx` | CDSS UI — vitals form, SSE stream, health dashboard | ✅ GENERATED |
| `requirements.txt` | Python dependencies | ✅ GENERATED |

---

## REMAINING GAPS (still needed)

| Component | Action Required |
|-----------|----------------|
| `apps/web/package.json` | Next.js 15 + Tailwind config |
| `apps/web/app/page.tsx` | Mount TriagePage component |
| `apps/web/app/layout.tsx` | Root layout with offline service worker |
| `apps/web/public/sw.js` | PWA service worker for offline caching |
| `data/protocols/` | Actual WHO ETAT PDF → text extraction |
| `core/__init__.py` | Python package init |
| `api/__init__.py` | Python package init |
| `.env.example` | Environment variable template |
| `infra/k8s/` | Kubernetes manifests (cloud teacher dashboard) |
| `tests/` | Validator unit tests — critical for medical credibility |

---

## CRITICAL SAFETY GAPS (competition risk)

1. **No unit tests on validator.py** — judges will ask "how do you know the triage rules are correct?"
   → Generate `tests/test_validator.py` with WHO vital boundary cases

2. **No LoRA adapter config** — promised in §3 Gemma 4 usage, not implemented
   → Add `core/lora.py` with adapter loading stub

3. **Qdrant uses placeholder vector** — `inference.py` sends zero-vector for RAG search
   → Wire real `nomic-embed-text` embeddings in `_fetch_rag_context()`

4. **No PWA manifest / service worker** — offline mode not cached on browser
   → Add `public/manifest.json` + `sw.js`
