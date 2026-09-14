"""Assemble video/build/site: the stage page, the replay app, the export documents and timeline.json.

Run after tts.py and export_site.py.
"""

import json
import re
import shutil
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
SITE = HERE / "build" / "site"
RUN = "test-3"
LEAD_IN, PAD = 0.6, 0.9  # seconds of silence before narration starts and after it ends, per scene

# What each scene shows. cut = replay events shown in the trace (index into run.json events).
SCENES = {
    "intro":      {"view": "card", "chip": ""},
    "prompt":     {"view": "app", "chip": "The input", "cut": -1, "tab": None, "type": True},
    "brief":      {"view": "app", "chip": "01  Brief", "cut": 2, "tab": "brief",
                   "scroll": ["#results", 0, ".assume"], "ring": ".assume"},
    "research":   {"view": "app", "chip": "02  Web research", "cut": 5, "tab": "research",
                   "scroll": ["#results", 0, "#view .card:nth-child(3)"], "ring": "#trace .step:nth-child(2) .meta"},
    "strategy":   {"view": "app", "chip": "03  Strategy", "cut": 8, "tab": "strategy",
                   "scroll": ["#results", 0, "#view > .card:last-child"]},
    "copy":       {"view": "app", "chip": "04  Ad copy, limits checked in code", "cut": 16, "tab": "google",
                   "scroll": ["#results", 0, "#view .card:nth-child(2) .grid"], "ring": "#view .card:nth-child(2) .grid table"},
    "moodboard":  {"view": "app", "chip": "05  Pinterest moodboard and art direction", "cut": 20, "tab": "direction",
                   "scroll": ["#results", 0, "#view .grid"]},
    "tournament": {"view": "app", "chip": "06  Design tournament", "cut": 74, "tab": "meta",
                   "trace": ".retry-note", "ring": "#trace .retry-note"},
    "judge":      {"view": "app", "chip": "07  Judge and rematch", "cut": 74, "tab": "meta",
                   "scroll": ["#results", 0, ".variants"], "ring": ".variants"},
    "critic":     {"view": "app", "chip": "08  Critic and fix round", "cut": 105, "tab": "critic",
                   "scroll": ["#results", 0, "#view > .card:nth-child(3)"]},
    "landing":    {"view": "app", "chip": "09  Landing page", "cut": 105, "tab": "landing", "landing_scroll": True},
    "docs":       {"view": "docs", "chip": "10  The launch kit", "hold": 7.0},
    "outro":      {"view": "end", "chip": ""},
}


def captions(text, align, offset):
    """Split narration into short caption lines timed from ElevenLabs character timings."""
    chars, starts, ends = align["characters"], align["character_start_times_seconds"], align["character_end_times_seconds"]
    out, i = [], 0
    words = [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]
    line = []
    for n, (a, b) in enumerate(words):
        line.append((a, b))
        word = text[a:b]
        long_enough = len(line) >= 9 or (len(line) >= 5 and word[-1] in ",:;") or word[-1] in ".?!"
        if long_enough or n == len(words) - 1:
            s, e = line[0][0], line[-1][1] - 1
            out.append({"start": round(offset + starts[s], 2), "end": round(offset + ends[e], 2),
                        "text": text[s:e + 1]})
            line = []
    return out


def main():
    shutil.rmtree(SITE, ignore_errors=True)
    shutil.copytree(ROOT / "dist" / "adforge-replay", SITE / "app")
    (SITE / "docs").mkdir(parents=True)
    for f in ("meta_ads.csv", "google_ads.csv", "campaign.json"):
        shutil.copyfile(ROOT / "runs" / RUN / f, SITE / "docs" / f)
    for f in ("stage.html", "docs.html"):
        shutil.copyfile(HERE / "stage" / f, SITE / f)

    script = json.loads((HERE / "script.json").read_text(encoding="utf-8"))
    t, scenes, caps = 0.0, [], []
    for sc in script:
        meta = json.loads((HERE / "build" / "audio" / f"{sc['id']}.json").read_text(encoding="utf-8"))
        speech = meta["alignment"]["character_end_times_seconds"][-1]
        dur = LEAD_IN + speech + PAD + SCENES[sc["id"]].get("hold", 0)  # hold: extra silent time to read
        scenes.append({"id": sc["id"], "start": round(t, 2), "dur": round(dur, 2), "voice_at": round(t + LEAD_IN, 2),
                       **SCENES[sc["id"]]})
        caps += captions(sc["text"], meta["alignment"], t + LEAD_IN)
        t += dur
    desc = json.loads((ROOT / "runs" / RUN / "run.json").read_text(encoding="utf-8"))["description"]
    (SITE / "timeline.json").write_text(json.dumps({"run": RUN, "description": desc, "total": round(t, 2), "scenes": scenes,
                                                    "captions": caps}, indent=1), encoding="utf-8")
    print(f"site ready, {len(scenes)} scenes, {t:.1f}s, {len(caps)} captions")


if __name__ == "__main__":
    main()
