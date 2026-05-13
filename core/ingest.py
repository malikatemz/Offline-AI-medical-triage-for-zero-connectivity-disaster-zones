"""
RescueNet — Qdrant Protocol Ingestion
Chunks WHO ETAT + Red Cross Field Manual → embeds → upserts to local Qdrant.
Run once on first deploy: python -m core.ingest
"""

import hashlib
import json
import re
import time
from pathlib import Path

import httpx
from sentence_transformers import SentenceTransformer

QDRANT_URL      = "http://qdrant:6333"
COLLECTION      = "rescuenet_protocols"
EMBED_MODEL     = "BAAI/bge-small-en-v1.5"   # 45MB — fits edge hardware
CHUNK_SIZE      = 400    # tokens approx
CHUNK_OVERLAP   = 80
VECTOR_DIM      = 384
PROTOCOLS_DIR   = Path("data/protocols")
BATCH_SIZE      = 32


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split on sentence boundaries, respect chunk size."""
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    chunks, current, current_len = [], [], 0
    for sent in sentences:
        words = len(sent.split())
        if current_len + words > size and current:
            chunks.append(" ".join(current))
            # Keep overlap
            overlap_words = " ".join(current).split()[-overlap:]
            current = [" ".join(overlap_words)]
            current_len = len(overlap_words)
        current.append(sent)
        current_len += words
    if current:
        chunks.append(" ".join(current))
    return [c for c in chunks if len(c.strip()) > 50]


def load_protocols() -> list[dict]:
    """
    Load protocol files from data/protocols/.
    Supports: .txt, .md, .json
    Falls back to built-in seed if directory empty.
    """
    docs = []
    if PROTOCOLS_DIR.exists():
        for f in PROTOCOLS_DIR.iterdir():
            if f.suffix in (".txt", ".md"):
                text = f.read_text(encoding="utf-8", errors="ignore")
                docs.append({"source": f.name, "text": text})
            elif f.suffix == ".json":
                data = json.loads(f.read_text())
                if isinstance(data, list):
                    docs.extend(data)
                else:
                    docs.append(data)

    if not docs:
        print("⚠  No protocol files found — using built-in WHO ETAT seed data")
        docs = _seed_protocols()

    return docs


def _seed_protocols() -> list[dict]:
    """Minimal WHO ETAT seed — real deploy should load full PDFs."""
    return [
        {
            "source": "WHO_ETAT_2016",
            "text": (
                "TRIAGE RED — Immediate: Patient requires immediate life-saving intervention. "
                "Airway: Open airway, head-tilt chin-lift. If no spontaneous breathing after "
                "opening airway: classify as BLACK. If breathing: assess circulation. "
                "Circulation: Control severe haemorrhage with direct pressure. "
                "Establish IV access — 2 large bore cannulas. Fluid resuscitation if BP systolic < 90mmHg. "
                "Consciousness: GCS <= 8 requires airway protection. Position recovery if unconscious. "
                "Protocol reference: WHO ETAT 2016 Section 4."
            ),
        },
        {
            "source": "WHO_ETAT_2016",
            "text": (
                "TRIAGE YELLOW — Delayed: Serious but stable. Monitor vitals every 5 minutes. "
                "Establish IV access. Assess and treat pain. "
                "Re-triage if deterioration: elevated HR > 120, RR > 30, BP drop > 20mmHg. "
                "Wound management: irrigate, cover, do not probe. "
                "Protocol reference: WHO ETAT 2016 Section 5."
            ),
        },
        {
            "source": "START_TRIAGE",
            "text": (
                "START TRIAGE ALGORITHM: "
                "Step 1 — Breathing: Not breathing after airway manoeuvre → BLACK. "
                "Breathing rate > 30 → RED. "
                "Step 2 — Perfusion: Capillary refill > 2 seconds OR radial pulse absent → RED. "
                "Step 3 — Mental status: Cannot follow simple commands → RED. "
                "All others → YELLOW or GREEN."
            ),
        },
        {
            "source": "RED_CROSS_FIELD_MANUAL_2022",
            "text": (
                "HAEMORRHAGE CONTROL — Field Protocol: "
                "Direct pressure minimum 10 minutes uninterrupted. "
                "Tourniquet application: 5-7cm proximal to wound, note time of application. "
                "Haemostatic dressing if tourniquet not applicable (junctional wounds). "
                "Wound packing: gauze packing for cavity wounds, maintain pressure. "
                "Do NOT remove impaled objects. "
                "Source: ICRC War Surgery Field Manual 2022."
            ),
        },
        {
            "source": "WHO_ETAT_SEPSIS",
            "text": (
                "SEPTIC SHOCK — Recognition and Management: "
                "Criteria: HR > 120 AND BP systolic < 90 AND suspected infection source. "
                "Immediate: IV access × 2, blood cultures if available, "
                "IV fluid bolus 500ml crystalloid over 15 minutes, reassess. "
                "Monitor: urine output target > 0.5ml/kg/hr. "
                "Do NOT delay fluid resuscitation for culture results in field setting. "
                "Source: WHO ETAT Sepsis Module 2016."
            ),
        },
        {
            "source": "WHO_ETAT_RESPIRATORY",
            "text": (
                "RESPIRATORY FAILURE — Field Management: "
                "RR < 10 or > 30: immediate airway assessment. "
                "SpO2 < 90%: supplemental oxygen if available, target SpO2 >= 94%. "
                "Tension pneumothorax signs (deviated trachea, absent breath sounds, "
                "hypotension): needle decompression 2nd intercostal space midclavicular line. "
                "Open chest wound: 3-sided occlusive dressing. "
                "Source: WHO ETAT Respiratory Module 2016."
            ),
        },
    ]


def create_collection(client: httpx.Client):
    """Create Qdrant collection if not exists."""
    try:
        r = client.get(f"{QDRANT_URL}/collections/{COLLECTION}")
        if r.status_code == 200:
            print(f"✓ Collection '{COLLECTION}' exists")
            return
    except Exception:
        pass

    payload = {
        "vectors": {
            "size": VECTOR_DIM,
            "distance": "Cosine",
            "on_disk": True,       # Low RAM mode — important for edge
        },
        "optimizers_config": {
            "indexing_threshold": 1000,
        },
    }
    r = client.put(f"{QDRANT_URL}/collections/{COLLECTION}", json=payload)
    r.raise_for_status()
    print(f"✓ Created collection '{COLLECTION}'")


def upsert_batch(client: httpx.Client, points: list[dict]):
    r = client.put(
        f"{QDRANT_URL}/collections/{COLLECTION}/points",
        json={"points": points},
        timeout=30,
    )
    r.raise_for_status()


def ingest():
    print("RescueNet — Protocol Ingestion")
    print(f"  Embed model : {EMBED_MODEL}")
    print(f"  Qdrant      : {QDRANT_URL}")
    print(f"  Collection  : {COLLECTION}")

    # Load embedder
    print("Loading embedding model...")
    embedder = SentenceTransformer(EMBED_MODEL)

    # Load protocols
    raw_docs = load_protocols()
    print(f"Loaded {len(raw_docs)} source documents")

    # Chunk
    all_chunks = []
    for doc in raw_docs:
        chunks = chunk_text(doc["text"])
        for c in chunks:
            all_chunks.append({
                "id":     hashlib.md5(c.encode()).hexdigest()[:16],
                "text":   c,
                "source": doc.get("source", "UNKNOWN"),
            })
    print(f"Generated {len(all_chunks)} chunks")

    # Create collection
    with httpx.Client(timeout=10) as client:
        create_collection(client)

        # Embed + upsert in batches
        total = 0
        for i in range(0, len(all_chunks), BATCH_SIZE):
            batch = all_chunks[i:i + BATCH_SIZE]
            texts = [c["text"] for c in batch]
            vectors = embedder.encode(texts, normalize_embeddings=True).tolist()

            points = [
                {
                    "id":      abs(int(c["id"], 16)) % (2**63),
                    "vector":  v,
                    "payload": {"text": c["text"], "source": c["source"]},
                }
                for c, v in zip(batch, vectors)
            ]
            upsert_batch(client, points)
            total += len(points)
            print(f"  Upserted {total}/{len(all_chunks)} chunks")
            time.sleep(0.1)

    print(f"\n✓ Ingestion complete — {total} protocol chunks indexed")
    print(f"  Collection: {COLLECTION} @ {QDRANT_URL}")


if __name__ == "__main__":
    ingest()
