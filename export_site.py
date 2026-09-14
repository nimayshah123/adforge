"""Build a static replay site from finished runs, for hosting without the pipeline.

    python export_site.py test-3 --stages-from test-1

Pinterest contact sheets are left out on purpose: they are other people's work,
used locally as style reference only. The lead form is disabled.
"""

import argparse
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).parent
RUNS = ROOT / "runs"
OUT = ROOT / "dist" / "adforge-replay"

EARLY = ("brief", "research", "strategy", "meta_copy", "google_copy", "landing")
EARLY_KEYS = ("brief", "research", "strategy", "meta", "google", "landing")
MAX_GAP = 180  # seconds; longer gaps were the process being stopped, not work


def stitched_events(run, early):
    events = []
    if early:
        events += [e for e in early["events"]
                   if e.get("step") in EARLY or (e["type"] == "artifact" and e["key"] in EARLY_KEYS)]
    events += [e for e in run["events"] if e["type"] != "resume"
               and not (e["type"] == "artifact" and early and e["key"] in EARLY_KEYS and
                        any(x.get("key") == e["key"] for x in events))]
    # rebuild the clock as working time: drop downtime between interrupted segments
    clock, prev, out = 0.0, None, []
    for e in events:
        if prev is not None:
            clock += min(max(e["t"] - prev, 0), MAX_GAP)
        prev = e["t"]
        out.append({**e, "t": round(clock, 1)})
    return out


def export(run_id, early_id=None):
    run = json.loads((RUNS / run_id / "run.json").read_text(encoding="utf-8"))
    early = json.loads((RUNS / early_id / "run.json").read_text(encoding="utf-8")) if early_id else None
    if run["status"] != "done":
        raise SystemExit(f"{run_id} is not finished ({run['status']})")

    dst = OUT / "runs" / run_id
    shutil.rmtree(dst, ignore_errors=True)
    (dst / "creatives").mkdir(parents=True)
    (dst / "landing").mkdir()

    for png in (RUNS / run_id / "creatives").glob("*.png"):
        shutil.copyfile(png, dst / "creatives" / png.name)
    shutil.copyfile(RUNS / run_id / "export.zip", dst / "export.zip")

    landing = (RUNS / run_id / "landing" / "index.html").read_text(encoding="utf-8")
    landing = re.sub(r"<script>\s*document\.addEventListener\('submit'.*?</script>",
                     "<script>document.addEventListener('submit', e => { e.preventDefault(); "
                     "e.target.innerHTML = '<p>Demo page: the form is disabled.</p>'; });</script>",
                     landing, flags=re.S)
    (dst / "landing" / "index.html").write_text(landing, encoding="utf-8")

    if early:
        run["steps"] = {**{k: v for k, v in early["steps"].items() if k in EARLY}, **run["steps"]}
    run["events"] = stitched_events(run, early)
    run["started"], run["finished"] = 0, run["events"][-1]["t"]
    run["stitched_from"] = early_id
    for b in run.get("direction", {}).get("boards", []):
        b.pop("sheet", None)
        b.pop("pins", None)
    run["landing"]["url"] = f"runs/{run_id}/landing/index.html"
    run["plan"]["export"] = f"runs/{run_id}/export.zip"
    base = "http://localhost:8000/runs/"
    for k in ("meta_links", "google_links"):
        run["plan"][k] = [l.replace(base, "runs/") for l in run["plan"][k]]
    (dst / "run.json").write_text(json.dumps(run), encoding="utf-8")
    return {"id": run_id, "description": run["description"], "status": "done"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    ap.add_argument("--stages-from", help="earlier run whose brief..landing stages this run was resumed from")
    a = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    entry = export(a.run_id, a.stages_from)
    index = OUT / "runs" / "index.json"
    runs = json.loads(index.read_text(encoding="utf-8")) if index.exists() else []
    runs = [r for r in runs if r["id"] != entry["id"]] + [entry]
    index.write_text(json.dumps(runs), encoding="utf-8")

    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    html = html.replace("</head>", "<script>window.ADFORGE_STATIC = true;</script>\n</head>", 1)
    (OUT / "index.html").write_text(html, encoding="utf-8")
    print(f"exported {a.run_id} to {OUT}")


if __name__ == "__main__":
    main()
