"""auth-service stand-in for the class load test: the two public calls a student entry makes, each with latency.

Run:  python -m uvicorn --app-dir tests/load auth_stub:app --port 8765
Env:  STUB_LAT_MIN_MS / STUB_LAT_MAX_MS (default 50 / 200) - the delay added to every answer, like a real network hop.
"""
from __future__ import annotations

import asyncio
import os
import random
import uuid

from fastapi import FastAPI

LAT_MIN = float(os.getenv("STUB_LAT_MIN_MS", "50")) / 1000
LAT_MAX = float(os.getenv("STUB_LAT_MAX_MS", "200")) / 1000
# the group lookup of the teacher routes (`/auth/org/groups/{id}`) can be made slow on its own: a loaded auth-service
GROUP_LAT = float(os.getenv("STUB_GROUP_LAT_MS", "0")) / 1000
# the report model (Azure OpenAI / DeepSeek chat-completions shape) can be made as slow as a real one
LLM_LAT = float(os.getenv("STUB_LLM_LAT_MS", "0")) / 1000
_NAMESPACE = uuid.UUID("6f1c0c3e-0a52-4d57-9a3d-3b9d2f5a1e10")

app = FastAPI()


async def _latency() -> None:
    await asyncio.sleep(random.uniform(LAT_MIN, LAT_MAX))


@app.get("/api/v1/auth/public/groups/{group_id}/verify-student")
async def verify_student(group_id: str, student_id: str = "", student_name: str = "", q: str = ""):
    await _latency()
    name = student_name or q or "Ученик"
    return {
        "enrolled": True,
        "student": {
            "id": student_id or str(uuid.uuid5(_NAMESPACE, name)),
            "display_id": q or None,
            "full_name": name,
            "class_level": 8,
        },
    }


@app.get("/api/v1/auth/me")
async def me():
    """The teacher of the load test (exam-service's staff dependency)."""
    await _latency()
    return {"id": "USR_LOAD", "org_role": "teacher", "default_org_id": "ORG_LOAD01"}


@app.get("/api/v1/auth/org/groups/{group_id}")
async def group(group_id: str):
    await asyncio.sleep(GROUP_LAT) if GROUP_LAT else await _latency()
    return {"id": group_id, "name": "8А", "class_level": 8, "teacher_id": "USR_LOAD", "teacher_name": "Учитель"}


@app.get("/api/v1/internal/org/{org_id}/groups/{group_id}/lessons/{lesson_id}/attendance")
async def attendance(org_id: str, group_id: str, lesson_id: str, lesson_date: str = ""):
    await _latency()
    return {"status": "in_progress"}


@app.post("/llm")
async def llm():
    """A chat-completions answer after STUB_LLM_LAT_MS: valid JSON with a summary, as the report asks the model for."""
    await asyncio.sleep(LLM_LAT)
    narrative = '{"summary": "Класс справился с работой.", "strengths": ["Линейные уравнения"], "critical_gaps": []}'
    return {"choices": [{"message": {"content": narrative}}]}
