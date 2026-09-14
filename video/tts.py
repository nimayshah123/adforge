"""Narration per scene from ElevenLabs, with character timings for subtitles.

    ELEVENLABS_API_KEY=... python video/tts.py        # writes video/build/audio/<id>.mp3 + .json
"""

import base64
import json
import os
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "build" / "audio"
VOICE = os.environ.get("VOICE_ID", "cjVigY5qzO86Huf0OWal")  # Eric, smooth and trustworthy
MODEL = "eleven_multilingual_v2"


def tts(text):
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE}/with-timestamps?output_format=mp3_44100_128",
        data=json.dumps({"text": text, "model_id": MODEL,
                         "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "style": 0.15}}).encode(),
        headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"], "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    only = set(sys.argv[1:])
    for scene in json.loads((HERE / "script.json").read_text(encoding="utf-8")):
        if only and scene["id"] not in only:
            continue
        mp3, meta = OUT / f"{scene['id']}.mp3", OUT / f"{scene['id']}.json"
        if mp3.exists() and meta.exists() and json.loads(meta.read_text())["text"] == scene["text"]:
            print("cached", scene["id"])
            continue
        d = tts(scene["text"])
        mp3.write_bytes(base64.b64decode(d["audio_base64"]))
        meta.write_text(json.dumps({"text": scene["text"], "alignment": d["alignment"]}), encoding="utf-8")
        print("wrote", scene["id"], round(d["alignment"]["character_end_times_seconds"][-1], 2), "s")


if __name__ == "__main__":
    main()
