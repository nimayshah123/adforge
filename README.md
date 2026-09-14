# AdForge

Type one sentence about what you want to advertise. AdForge researches the market on the web, writes the strategy, writes Meta and Google Search ads, designs the ad images, builds a landing page with working lead capture, has a critic review everything (including looking at the rendered images), fixes what failed, and hands you a launch kit.

```
"Get remote workers in Austin to subscribe to our $40/month cold brew delivery, Bean Drop"
        |
      brief ........ sonnet, low effort. Lists every assumption it had to make
        |
      research ..... sonnet + web search. Competitors, pricing, audience insights with source URLs, policy risks
        |
      strategy ..... opus, high effort. Positioning, 3 segments with Meta targeting, budget split, KPIs, brand kit
        |
   +----+-------------------+----------------+
 meta copy            google copy        landing page
   |  code counts characters, feeds exact violations back, up to 3 tries
   |
 moodboard ......... headless Chromium searches Pinterest (no login), builds contact sheets
   |
 art direction ..... opus LOOKS at the sheets: what the references do, the category cliches
   |                 to avoid, 3 radically different visual languages (style only, no copy)
   |
 ad images ......... per ad, one design per visual language (sonnet writes HTML/CSS/SVG),
   |                 Chromium renders 1080x1080, code checks message text off canvas, clipped
   |                 or covered by another element and sends exact measurements back
   |
 judge ............. opus looks at the 3 PNGs, scores stop power / legibility / brand fit / craft
   |                 winner gets one polish pass, then polish vs original head to head
   |
 critic ............ opus opens the PNGs with the Read tool and judges image + copy + policy
   |
 fix round ......... only the assets that failed, copy and/or image
   |
 launch kit ........ campaign.json, meta_ads.csv, google_ads.csv, PNGs, landing page, UTM links, daily budgets
```

## Run it

Needs Python 3.11+ and Claude Code logged in (`claude auth status`). No API key: every model call goes through `claude -p` on your subscription.

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # .venv/bin on mac/linux
.venv/Scripts/python -m playwright install chromium
.venv/Scripts/python -m uvicorn app:app --port 8000
```

Open http://localhost:8000. A full run takes 30 to 50 minutes (the design tournament is most of it).

## Walkthrough video

A 2 minute 26 second narrated walkthrough of one real run, with subtitles: [AdForge_Demo.mov](https://github.com/nimayshah123/adforge/releases/tag/v0.1-demo)

It is rendered from the replay itself, frame by frame, so it can be rebuilt after any new run:

```bash
ELEVENLABS_API_KEY=... .venv/Scripts/python video/tts.py     # narration per scene, with character timings
.venv/Scripts/python video/build_site.py                      # stage page, docs viewer, timeline and captions
.venv/Scripts/python video/render.py frames                   # one H.264 segment per scene
.venv/Scripts/python video/render.py mux AdForge_Demo.mov     # voice + mov_text subtitles + .srt sidecar
```

## Public replay site

The pipeline cannot run on Vercel (subscription CLI auth, 30 minute runs, Chromium), so finished runs are published as a static replay: https://adforge-replay.vercel.app

```bash
.venv/Scripts/python export_site.py test-3 --stages-from test-1   # writes dist/adforge-replay
cd dist/adforge-replay && vercel deploy --prod --yes
```

The export drops the Pinterest contact sheets (other people's work) and disables the landing page form.

## Files

| File | What it does |
|---|---|
| `llm.py` | The only place that calls a model. Wraps `claude -p` with a JSON schema. Swap this one function to move to API keys. |
| `pipeline.py` | The stages, schemas, character limit checkers, fix round and export. Every stage is saved, so a crashed run resumes. |
| `render.py` | Renders the HTML ad in Chromium, measures off-canvas, clipped and covered message text, screenshots the PNG. |
| `moodboard.py` | Signed-out Pinterest search in headless Chromium, pins laid out on contact sheets. Style reference only. |
| `app.py` | FastAPI: start run, resume, stream events (SSE), lead capture, zip export. |
| `static/index.html` | The UI: live agent trace on the left, results on the right. |

## Things that were not obvious

- **`claude -p` loads your whole Claude Code setup by default.** A 1K token task was sending about 76K tokens: MCP servers, skills, settings, memory. `--strict-mcp-config --disable-slash-commands --setting-sources "" --tools ""` plus an empty working directory brings it to about 1K.
- **Models cannot count characters.** Google headlines are 30 characters max. Never ask the model to self-check. Count in code and send back the exact offending line and its length.
- **No image model needed for ad images.** The model writes the ad as HTML and CSS with inline SVG. Chromium renders it, and code checks where every text box landed. Text comes out crisp and never misspelled, which image models still get wrong.
- **The critic has to look at pixels, not HTML.** The HTML for image 1 was valid and nothing overflowed the canvas, but a badge covered a sign and left a stray "CO". Only reading the PNG caught it.
- **Headless agents are memory hungry.** Each `claude` process holds 200 to 400 MB. Four in parallel plus Chromium ran a 16 GB laptop out of memory and the OS killed the run. Model calls are capped at 2 at a time (`ADFORGE_CONCURRENCY`) and Chromium at 1.
- **"Make it loud" breaks legibility first.** With a maximalist brief the harsh judge scored most first designs 3 to 6 out of 10, mostly for giant decorative type covering the message. So: decorative elements are tagged `data-bleed` and may leave the canvas, and the renderer walks `document.elementsFromPoint` over the message's actual glyph lines to find anything painted on top.
- **Covered-text detection has three false positives worth knowing.** Invisible full-canvas wrapper divs sit on top of everything (skip elements that paint nothing). Tight leading makes line boxes overlap even when glyphs don't (test glyph middle bands, not boxes). Riso misprint effects duplicate the headline on purpose (skip covers with identical text). Opacity and blend mode must be read from ancestors too.
- **Art directions must be styles, not concepts.** When the art director wrote directions with their own copy, all three ads converged on the same idea and ignored their own messages.
- **A polish pass can make it worse.** The polished winner has to beat the original head to head before it ships.
- **Tell the judge the medium.** Without it, an HTML-only judge asks for photography it cannot get.
- **The lead form wiring is not left to the model.** The model designs the page; a fixed script is injected that posts the form and the UTM parameters to `/api/runs/<id>/leads`.
