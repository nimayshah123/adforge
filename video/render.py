"""Render the walkthrough video frame by frame from stage.html, then mux narration and subtitles into a MOV.

    python video/render.py frames intro prompt brief     # render some scenes (each scene is its own segment)
    python video/render.py frames                        # render every scene not rendered yet
    python video/render.py mux ~/Downloads/AdForge_Demo.mov

Frames are driven by a virtual clock (seek, then screenshot), so the result is smooth no matter how slow
the machine is, and scenes can be rendered in separate short runs.
"""

import functools
import http.server
import json
import subprocess
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
BUILD = HERE / "build"
SITE, SEG, AUDIO = BUILD / "site", BUILD / "segments", BUILD / "audio"
FPS, PORT = 30, 8791


def serve():
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    handler = functools.partial(Quiet, directory=str(SITE))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def frames(only):
    tl = json.loads((SITE / "timeline.json").read_text(encoding="utf-8"))
    SEG.mkdir(parents=True, exist_ok=True)
    todo = [(i, s) for i, s in enumerate(tl["scenes"])
            if (s["id"] in only) or (not only and not (SEG / f"{i:02d}_{s['id']}.mp4").exists())]
    if not todo:
        print("nothing to render")
        return
    srv = serve()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(f"http://127.0.0.1:{PORT}/stage.html", wait_until="networkidle")
        page.evaluate("setup()")
        for i, sc in todo:
            out = SEG / f"{i:02d}_{sc['id']}.mp4"
            page.evaluate("i => apply(i)", i)
            page.wait_for_timeout(700)
            first, last = round(sc["start"] * FPS), round((sc["start"] + sc["dur"]) * FPS)
            enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "image2pipe", "-framerate", str(FPS),
                                    "-c:v", "mjpeg", "-i", "-", "-c:v", "libx264", "-preset", "medium", "-crf", "16",
                                    "-pix_fmt", "yuv420p", str(out)], stdin=subprocess.PIPE)
            for n in range(first, last):
                page.evaluate("t => frame(t)", n / FPS)
                enc.stdin.write(page.screenshot(type="jpeg", quality=92))
            enc.stdin.close()
            enc.wait()
            print(f"rendered {out.name}: {last - first} frames", flush=True)
        browser.close()
    srv.shutdown()


def srt_time(s):
    ms = round(s * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def mux(dest):
    tl = json.loads((SITE / "timeline.json").read_text(encoding="utf-8"))
    segs = [SEG / f"{i:02d}_{s['id']}.mp4" for i, s in enumerate(tl["scenes"])]
    missing = [s.name for s in segs if not s.exists()]
    if missing:
        raise SystemExit(f"missing segments: {missing}")
    (BUILD / "concat.txt").write_text("".join(f"file '{s.as_posix()}'\n" for s in segs), encoding="utf-8")

    srt = BUILD / "captions.srt"
    srt.write_text("".join(f"{n}\n{srt_time(c['start'])} --> {srt_time(c['end'] + .3)}\n{c['text']}\n\n"
                           for n, c in enumerate(tl["captions"], 1)), encoding="utf-8")

    inputs, filters = [], []
    for k, sc in enumerate(tl["scenes"]):
        inputs += ["-i", str(AUDIO / f"{sc['id']}.mp3")]
        filters.append(f"[{k + 2}:a]adelay={round(sc['voice_at'] * 1000)}|{round(sc['voice_at'] * 1000)}[a{k}]")
    n = len(tl["scenes"])
    filters.append("".join(f"[a{k}]" for k in range(n)) +
                   f"amix=inputs={n}:normalize=0,apad,atrim=0:{tl['total']},loudnorm=I=-16:TP=-1.5[voice]")

    dest = Path(dest).expanduser()
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(BUILD / "concat.txt"),
           "-i", str(srt), *inputs, "-filter_complex", ";".join(filters),
           "-map", "0:v", "-map", "[voice]", "-map", "1:s",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-c:s", "mov_text",
           "-metadata:s:s:0", "language=eng", "-movflags", "+faststart", str(dest)]
    subprocess.run(cmd, check=True)
    sidecar = dest.with_suffix(".srt")
    sidecar.write_text(srt.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"wrote {dest} and {sidecar}")


if __name__ == "__main__":
    if sys.argv[1] == "frames":
        frames(set(sys.argv[2:]))
    elif sys.argv[1] == "mux":
        mux(sys.argv[2])
