"""Проекты."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, field_validator

from .. import db
from ..pipeline.scoring import COMPONENTS, DEFAULT_WEIGHTS
from ..security import audit, editor, viewer

router = APIRouter(tags=["projects"])

PROJECT_COLS = (
    "id, name, topic, language, tone, max_length, prompt_template, prompt_version, generation_mode, publish_mode, window_hours, "
    "sim_threshold, topic_threshold, weights, context_items, context_sentences, min_items, auto_retry_unknown, show_sources, active, created_at"
)


# ---------- projects ----------
class ProjectBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    topic: str = Field("", max_length=1000)
    language: str = Field("ru", max_length=10)
    tone: str = Field("нейтральный, информативный", max_length=200)
    max_length: int = Field(900, ge=200, le=3500)
    prompt_template: str = Field("", max_length=6000)
    generation_mode: Literal["llm", "extractive", "auto"] = "llm"
    publish_mode: Literal["review", "auto", "full_auto"] = "review"
    window_hours: int = Field(48, ge=1, le=720)
    sim_threshold: float = Field(0.72, ge=0.1, le=0.99)
    topic_threshold: float = Field(0.40, ge=0, le=1)
    weights: dict[str, float] | None = None
    context_items: int = Field(6, ge=1, le=15)
    context_sentences: int = Field(3, ge=1, le=8)
    min_items: int = Field(2, ge=1, le=50)
    auto_retry_unknown: bool = False
    show_sources: bool = True
    active: bool = True

    @field_validator("weights")
    @classmethod
    def _weights(cls, v):
        if v is None:
            return v
        bad = set(v) - set(COMPONENTS)
        if bad:
            raise ValueError(f"Неизвестные веса: {', '.join(sorted(bad))}")
        if any(not (0 <= x <= 1) for x in v.values()):
            raise ValueError("Веса должны быть в диапазоне 0..1")
        return v


@router.get("/projects")
async def list_projects(user=Depends(viewer)):
    return await db.fetchall(f"SELECT {PROJECT_COLS} FROM projects ORDER BY id")


@router.get("/projects/{pid}")
async def get_project(pid: int, user=Depends(viewer)):
    row = await db.fetchone(f"SELECT {PROJECT_COLS} FROM projects WHERE id = %s", (pid,))
    if not row:
        raise HTTPException(404, "Проект не найден")
    return row


@router.post("/projects", status_code=201)
async def create_project(body: ProjectBody, user=Depends(editor)):
    d = body.model_dump()
    d["weights"] = Jsonb({**DEFAULT_WEIGHTS, **(d["weights"] or {})})
    cols = list(d)
    row = await db.fetchone(
        f"INSERT INTO projects({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING {PROJECT_COLS}", [d[c] for c in cols]
    )
    await audit(user, "create", "project", row["id"], {"name": body.name})
    return row


@router.put("/projects/{pid}")
async def update_project(pid: int, body: ProjectBody, user=Depends(editor)):
    old = await db.fetchone("SELECT topic, prompt_template, prompt_version FROM projects WHERE id = %s", (pid,))
    if not old:
        raise HTTPException(404, "Проект не найден")
    d = body.model_dump()
    d["weights"] = Jsonb({**DEFAULT_WEIGHTS, **(d["weights"] or {})})
    d["prompt_version"] = old["prompt_version"] + (1 if body.prompt_template != old["prompt_template"] else 0)
    sets = [f"{k} = %s" for k in d]
    if body.topic != old["topic"]:
        sets.append("topic_embedding = NULL")
    row = await db.fetchone(f"UPDATE projects SET {', '.join(sets)} WHERE id = %s RETURNING {PROJECT_COLS}", [*d.values(), pid])
    await audit(user, "update", "project", pid)
    return row


@router.delete("/projects/{pid}", status_code=204)
async def delete_project(pid: int, user=Depends(editor)):
    await db.execute("DELETE FROM projects WHERE id = %s", (pid,))
    await audit(user, "delete", "project", pid)


