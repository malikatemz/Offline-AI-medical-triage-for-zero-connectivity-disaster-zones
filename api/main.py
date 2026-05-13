"""
RescueNet — Main FastAPI Entry Point
Wires: inference + agents + sync + health
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.inference import app as inference_router
from core.sync import sync_lifespan


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with sync_lifespan(app):
        yield


app = FastAPI(
    title="RescueNet",
    version="2.0.0",
    description="Offline medical triage AI — zero-connectivity disaster zones",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount inference routes
app.mount("/", inference_router)
