import asyncio
import json
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pipeline import RUNS, Run, build

app = FastAPI()
RUNS.mkdir(exist_ok=True)
live: dict[str, Run] = {}
ROOT = Path(__file__).parent


class NewRun(BaseModel):
    description: str


@app.post("/api/runs")
async def create(body: NewRun):
    if not body.description.strip():
        raise HTTPException(400, "description is empty")
    run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    start(Run(run_id, body.description.strip()))
    return {"id": run_id}


@app.post("/api/runs/{run_id}/resume")
async def resume(run_id: str):
    if run_id in live and live[run_id].state["status"] == "running":
        return {"id": run_id}
    if not (RUNS / run_id / "run.json").exists():
        raise HTTPException(404)
    run = Run.load(run_id)
    run.emit(type="resume")
    start(run)
    return {"id": run_id}


def start(run: Run):
    live[run.id] = run
    run.save()

    async def go():
        try:
            await build(run)
        except Exception as e:
            run.state["status"] = "error"
            run.emit(type="error", detail=str(e)[:1000])

    asyncio.create_task(go())


@app.get("/api/runs")
def list_runs():
    out = []
    for f in sorted(RUNS.glob("*/run.json"), reverse=True):
        s = load(f.parent.name)
        out.append({"id": s["id"], "description": s["description"], "status": s["status"]})
    return out


def load(run_id):
    if run_id in live:
        return live[run_id].state
    f = RUNS / run_id / "run.json"
    if not f.exists():
        raise HTTPException(404)
    state = json.loads(f.read_text(encoding="utf-8"))
    if state["status"] == "running":  # the process that ran it is gone
        state["status"] = "interrupted"
    return state


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    return load(run_id)


@app.get("/api/runs/{run_id}/events")
async def events(run_id: str, request: Request):
    state = load(run_id)
    run = live.get(run_id)

    async def stream():
        q = asyncio.Queue()
        if run:
            run.listeners.append(q)
        try:
            for ev in list(state["events"]):
                yield f"data: {json.dumps(ev)}\n\n"
            if not run or state["status"] != "running":
                return
            while True:
                if await request.is_disconnected():
                    return
                try:
                    ev = await asyncio.wait_for(q.get(), 15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(ev)}\n\n"
                if ev["type"] in ("done", "error"):
                    return
        finally:
            if run and q in run.listeners:
                run.listeners.remove(q)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/api/runs/{run_id}/leads")
async def lead(run_id: str, request: Request):
    load(run_id)
    data = await request.json()
    data["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(RUNS / run_id / "leads.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(data) + "\n")
    return {"ok": True}


@app.get("/api/runs/{run_id}/export.zip")
def export_zip(run_id: str):
    f = RUNS / run_id / "export.zip"
    if not f.exists():
        raise HTTPException(404, "export not ready")
    return FileResponse(f, filename=f"campaign-{run_id}.zip")


app.mount("/runs", StaticFiles(directory=RUNS), name="runs")


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")
