"""One sentence in, a launch-ready ad campaign out.

brief -> research (web) -> strategy -> [meta copy, google copy, landing page]
      -> creatives (HTML rendered to PNG) -> critic (reads the PNGs) -> one fix round -> export
"""

import asyncio
import csv
import os
import shutil
import json
import re
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

from llm import ask, LLMError
from render import render_png
from moodboard import pinterest_moodboard

RUNS = Path(__file__).parent / "runs"
BASE_URL = "http://localhost:8000"
VARIANTS = int(os.environ.get("ADFORGE_VARIANTS", "3"))


# ---------- schema helpers ----------

STR = {"type": "string"}
NUM = {"type": "number"}
BOOL = {"type": "boolean"}


def obj(**props):
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def arr(items):
    return {"type": "array", "items": items}


def enum(*vals):
    return {"type": "string", "enum": list(vals)}


BRIEF = obj(
    brand_name=STR, product=STR, what_it_is=STR, audience=STR,
    goal=enum("awareness", "leads", "signups", "sales", "app_installs", "bookings"),
    offer=STR, geo=arr(STR), monthly_budget_usd=NUM, tone=STR,
    assumptions=arr(STR),
)

RESEARCH = obj(
    market_summary=STR,
    competitors=arr(obj(name=STR, url=STR, positioning=STR, pricing=STR, ad_angle=STR)),
    audience_insights=arr(obj(insight=STR, source_url=STR)),
    keyword_ideas=arr(STR),
    policy_flags=arr(STR),
)

STRATEGY = obj(
    positioning=STR, big_idea=STR, value_props=arr(STR),
    segments=arr(obj(
        id=STR, name=STR, who=STR, pain=STR, message=STR,
        meta_targeting=obj(age_min=NUM, age_max=NUM, locations=arr(STR), interests=arr(STR), exclusions=arr(STR)),
    )),
    channels=arr(obj(channel=enum("meta", "google_search"), budget_share=NUM, objective=STR, rationale=STR)),
    kpis=arr(obj(metric=STR, target=STR, why=STR)),
    brand_kit=obj(background=STR, primary=STR, accent=STR, text=STR,
                  headline_font=STR, body_font=STR, visual_style=STR),
    moodboard_queries=arr(STR),
)

DIRECTION = obj(
    observed=arr(STR), category_cliches=arr(STR),
    directions=arr(obj(name=STR, concept=STR, layout=STR, typography=STR, color=STR, css_techniques=STR)),
)

JUDGE = obj(
    scores=arr(obj(variant=NUM, stop_power=NUM, legibility=NUM, brand_fit=NUM, craft=NUM, overall=NUM, note=STR)),
    winner=NUM, improve=STR,
)

META_CTA = ("LEARN_MORE", "SIGN_UP", "SHOP_NOW", "GET_OFFER", "SUBSCRIBE", "BOOK_NOW", "ORDER_NOW", "DOWNLOAD", "CONTACT_US")
META_AD = obj(segment_id=STR, angle=STR, primary_text=STR, headline=STR, description=STR,
              cta=enum(*META_CTA), creative_concept=STR)
META = obj(ads=arr(META_AD))

GOOGLE = obj(
    campaign_name=STR,
    ad_groups=arr(obj(
        name=STR,
        keywords=arr(obj(text=STR, match=enum("exact", "phrase", "broad"))),
        headlines=arr(STR), descriptions=arr(STR), path1=STR, path2=STR,
    )),
    negative_keywords=arr(STR),
)

HTML = obj(html=STR)

CRITIC = obj(
    verdicts=arr(obj(asset_id=STR, passed=BOOL, fix=enum("none", "copy", "creative", "both"),
                     issues=arr(STR), instructions=STR)),
    overall=STR,
)


# ---------- deterministic checks (the model cannot count characters) ----------

def meta_violations(ad):
    v = []
    for field, limit in (("primary_text", 125), ("headline", 40), ("description", 30)):
        n = len(ad[field])
        if n > limit:
            v.append(f"{field} is {n} chars, limit {limit}: {ad[field]!r}")
    return v


def google_violations(g):
    v = []
    for grp in g["ad_groups"]:
        name = grp["name"]
        if len(grp["headlines"]) != 15:
            v.append(f"{name}: needs exactly 15 headlines, got {len(grp['headlines'])}")
        if len(grp["descriptions"]) != 4:
            v.append(f"{name}: needs exactly 4 descriptions, got {len(grp['descriptions'])}")
        seen = set()
        for h in grp["headlines"]:
            if len(h) > 30:
                v.append(f"{name}: headline is {len(h)} chars, limit 30: {h!r}")
            if h.lower() in seen:
                v.append(f"{name}: duplicate headline {h!r}")
            seen.add(h.lower())
        for d in grp["descriptions"]:
            if len(d) > 90:
                v.append(f"{name}: description is {len(d)} chars, limit 90: {d!r}")
        for p in ("path1", "path2"):
            if len(grp[p]) > 15 or " " in grp[p]:
                v.append(f"{name}: {p} must be 15 chars max with no spaces: {grp[p]!r}")
    return v


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "campaign"


# ---------- the run ----------

class Run:
    def __init__(self, run_id: str, description: str):
        self.id = run_id
        self.dir = RUNS / run_id
        self.work = self.dir / "work"          # empty cwd for the CLI so it sees nothing else
        self.work.mkdir(parents=True, exist_ok=True)
        (self.dir / "creatives").mkdir(exist_ok=True)
        (self.dir / "landing").mkdir(exist_ok=True)
        self.state = {"id": run_id, "description": description, "status": "running",
                      "started": time.time(), "steps": {}, "events": []}
        self.listeners: list[asyncio.Queue] = []

    @classmethod
    def load(cls, run_id: str):
        state = json.loads((RUNS / run_id / "run.json").read_text(encoding="utf-8"))
        run = cls(run_id, state["description"])
        run.state = state
        run.state["status"] = "running"
        return run

    # events ----------------------------------------------------------
    def emit(self, **ev):
        ev["t"] = round(time.time() - self.state["started"], 1)
        self.state["events"].append(ev)
        for q in self.listeners:
            q.put_nowait(ev)
        self.save()

    def put(self, key, value):
        self.state[key] = value
        self.emit(type="artifact", key=key)

    def save(self):
        (self.dir / "run.json").write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    async def step(self, name, label, coro):
        self.emit(type="step", step=name, status="start", label=label)
        try:
            result, meta = await coro
        except Exception as e:
            self.emit(type="step", step=name, status="error", label=label, detail=str(e)[:600])
            raise
        self.state["steps"][name] = meta
        self.emit(type="step", step=name, status="done", label=label, meta=meta)
        return result

    async def call(self, prompt, schema, system, **kw):
        return await ask(prompt, schema, system=system, cwd=self.work, **kw)

    async def validated(self, name, prompt, schema, system, check, **kw):
        """Ask, run the deterministic checker, feed exact violations back. Up to 3 tries."""
        total = {"seconds": 0, "turns": 0, "input_tokens": 0, "output_tokens": 0,
                 "web_searches": 0, "list_price_usd": 0, "attempts": 0, "model": kw.get("model", "sonnet")}
        p = prompt
        for attempt in range(1, 4):
            out, meta = await self.call(p, schema, system, **kw)
            for k in ("seconds", "turns", "input_tokens", "output_tokens", "web_searches", "list_price_usd"):
                total[k] = round(total[k] + (meta[k] or 0), 4)
            total["attempts"] = attempt
            v = check(out)
            if not v:
                return out, total
            self.emit(type="step", step=name, status="retry", label=f"{len(v)} limit violations, retrying",
                      detail="\n".join(v[:8]))
            p = (prompt + "\n\nYour previous answer:\n" + json.dumps(out) +
                 "\n\nIt broke these hard platform limits (counted by code, not estimated):\n- " +
                 "\n- ".join(v) + "\n\nReturn the full answer again with every violation fixed. "
                 "Rewrite the offending lines shorter, do not just truncate words.")
        total["unfixed_violations"] = v
        return out, total


SYSTEM = ("You are a senior performance marketer on a small, sharp agency team. "
          "Be specific and concrete. No filler, no hype words like 'revolutionary' or 'game-changing'. "
          "Never invent facts about real companies; if you are unsure, say so.")


async def build(run: Run):
    desc = run.state["description"]

    # Every stage is skipped if its result is already saved, so a crashed run resumes.
    have = run.state.get

    # 1. brief
    brief = have("brief") or await run.step("brief", "Parsing the one-line description into a brief", run.call(
        f"A client described their campaign in one line:\n\n\"{desc}\"\n\n"
        "Turn it into a campaign brief. Where the line does not say something (budget, geo, offer, "
        "tone, goal), make a sensible assumption for a small business and list every assumption "
        "explicitly in `assumptions`. If no budget is given, assume 1500 USD per month.",
        BRIEF, SYSTEM, model="sonnet", effort="low"))
    have("brief") or run.put("brief", brief)

    # 2. research with real web search
    research = have("research") or await run.step("research", "Researching competitors and audience on the web", run.call(
        "Brief:\n" + json.dumps(brief, indent=2) +
        "\n\nUse web search (4 to 6 searches) to find: the 3 to 5 closest real competitors for this "
        "product in this geo with their positioning, pricing and the angle their ads or homepage lead "
        "with; concrete audience insights with the URL each came from; search keywords people really "
        "use; and any ad policy risks (health claims, finance, alcohol, housing, before/after, etc). "
        "Only cite URLs you actually saw in search results.",
        RESEARCH, SYSTEM, model="sonnet", tools=("WebSearch",), timeout=900))
    have("research") or run.put("research", research)

    # 3. strategy
    strategy = have("strategy") or await run.step("strategy", "Writing strategy, segments and budget split", run.call(
        "Brief:\n" + json.dumps(brief, indent=2) + "\n\nResearch:\n" + json.dumps(research, indent=2) +
        "\n\nWrite the campaign strategy. Exactly 3 audience segments with ids s1, s2, s3, each with a "
        "distinct pain and message that exploits a gap in the competitors' angles. Channels: meta and "
        "google_search only, budget_share values summing to 1. 3 to 5 KPIs with numeric targets that "
        "are realistic for the budget. A brand kit: 4 hex colors with strong contrast between "
        "background and text, and two Google Fonts that exist. moodboard_queries: exactly 3 Pinterest "
        "searches for visual reference: one for ads in this category, two for loud graphic design "
        "styles that suit the brand but that this category almost never uses (e.g. 'maximalist "
        "typography poster', 'risograph collage ad', 'y2k chrome type').",
        STRATEGY, SYSTEM, model="opus", effort="high"))
    have("strategy") or run.put("strategy", strategy)
    ctx = "Brief:\n" + json.dumps(brief, indent=2) + "\n\nStrategy:\n" + json.dumps(strategy, indent=2)

    # 4. copy + landing page in parallel
    async def done(value):
        return value

    meta_task = done(have("meta")) if have("meta") else run.step("meta_copy", "Writing Meta ad copy (checked against char limits)", run.validated(
        "meta_copy", ctx + "\n\nWrite one Meta (Facebook/Instagram) feed ad per segment (3 ads). "
        "Hard limits: primary_text 125 chars, headline 40 chars, description 30 chars. "
        "creative_concept describes the single image: one visual idea plus at most 6 words of overlay "
        "text. Pick the CTA button that matches the goal.",
        META, SYSTEM, lambda o: sum((meta_violations(a) for a in o["ads"]), []), model="sonnet", effort="medium"))

    google_task = done(have("google")) if have("google") else run.step("google_copy", "Writing Google Search ads (checked against char limits)", run.validated(
        "google_copy", ctx + "\n\nResearch keywords:\n" + json.dumps(research["keyword_ideas"]) +
        "\n\nWrite a Google Search campaign with 2 ad groups (different intents). Each ad group: "
        "8 to 15 keywords with match types, exactly 15 responsive search ad headlines (each 30 chars "
        "max, all different, include the brand in 2 and the offer in 2), exactly 4 descriptions "
        "(90 chars max), path1/path2 (15 chars max, no spaces). Plus 10 or more negative keywords.",
        GOOGLE, SYSTEM, google_violations, model="sonnet", effort="medium"))

    landing_task = done(None) if have("landing") else run.step("landing", "Building the landing page", run.call(
        ctx + "\n\nBuild the landing page as ONE self-contained HTML file. Use the brand kit colors "
        "and load the two fonts from Google Fonts. Sections: hero with the big idea and the offer, "
        "3 value props, a short section speaking to each segment, social proof placeholders clearly "
        "marked as examples, FAQ, and a lead form. The form must have id=\"lead-form\" and inputs "
        "with name attributes (at least name and email) and a submit button. No external images and "
        "no JavaScript; use CSS and inline SVG for visuals. Mobile responsive.",
        HTML, SYSTEM, model="sonnet", effort="medium"))

    direction_task = done(None) if have("direction") else run.step(
        "art_direction", "Pinterest moodboard + art direction", art_direction(run, strategy))

    meta, google, landing, direction = await asyncio.gather(meta_task, google_task, landing_task, direction_task)
    if direction:
        run.put("direction", direction)
    direction = run.state["direction"]
    have("meta") or run.put("meta", meta)
    have("google") or run.put("google", google)
    if landing:
        write_landing(run, landing["html"])
        run.put("landing", {"url": f"/runs/{run.id}/landing/index.html"})

    # 5. creatives: VARIANTS designs per ad in different directions, a judge picks the winner
    if not have("creatives"):
        run.put("creatives", list(await asyncio.gather(*(
            tournament(run, i, ad, strategy, direction) for i, ad in enumerate(meta["ads"], 1)))))
    creatives = run.state["creatives"]

    # 6. critic looks at the rendered PNGs, not the HTML
    critic = have("critic") or await run.step("critic", "Critic reviewing every ad and creative image", run.call(
        ctx + "\n\nResearch policy flags:\n" + json.dumps(research["policy_flags"]) +
        "\n\nMeta ads:\n" + json.dumps([{**a, "asset_id": f"m{i}", "image_file": str(run.dir / c["png"])}
                                       for i, (a, c) in enumerate(zip(meta["ads"], creatives), 1)], indent=2) +
        "\n\nGoogle ad groups (asset ids g1, g2):\n" + json.dumps(google["ad_groups"], indent=2) +
        "\n\nYou are the last reviewer before launch. Use the Read tool to LOOK at every image_file. "
        "For each asset (m1, m2, m3, g1, g2) decide pass or fail. Fail for: text that is cut off, "
        "overlapping or unreadable, low contrast, overlay text that does not match the ad, copy that "
        "is generic or could be any brand, claims that would get rejected by Meta or Google ad "
        "policy, or a message that does not fit its segment. Be strict but only fail for real "
        "problems. For a fail set fix to copy, creative or both and write concrete instructions.",
        CRITIC, SYSTEM, model="opus", tools=("Read",), add_dirs=(run.dir / "creatives",), effort="high"))
    have("critic") or run.put("critic", critic)

    # 7. one fix round; each finished fix is saved on its own
    fixed = run.state.setdefault("fixed", [])

    async def fix(v):
        aid = v["asset_id"]
        m = re.fullmatch(r"m(\d)", aid)
        if m and int(m.group(1)) <= len(meta["ads"]):
            await fix_meta(run, int(m.group(1)), v, meta, creatives, strategy, ctx)
        elif aid in ("g1", "g2") and v["fix"] in ("copy", "both"):
            await fix_google(run, int(aid[1]), v, google, ctx)
        else:
            return
        fixed.append(aid)
        run.put("meta", meta)
        run.put("google", google)
        run.put("creatives", creatives)

    await asyncio.gather(*(fix(v) for v in critic["verdicts"]
                           if not v["passed"] and v["fix"] != "none" and v["asset_id"] not in fixed))

    # 8. export
    run.put("plan", build_plan(run, brief, strategy, meta, google))
    export(run)
    run.state["status"] = "done"
    run.state["finished"] = time.time()
    run.emit(type="done", seconds=round(run.state["finished"] - run.state["started"], 1))


CREATIVE_SYSTEM = SYSTEM + (
    " You are also a fearless art director whose static social ads stop the scroll. You design in "
    "HTML and CSS the way a top poster designer works: huge type, confident color, texture, "
    "collision, one unforgettable idea. Safe and tasteful is a failure.")


async def art_direction(run, strategy):
    boards = await asyncio.to_thread(pinterest_moodboard, strategy["moodboard_queries"][:3], run.dir / "moodboard")
    if boards:
        files = "\n".join(f"- {b['sheet']} (search: {b['query']})" for b in boards)
        look = ("Use the Read tool to LOOK at each of these Pinterest moodboard sheets (paths relative to "
                f"{run.dir}):\n" + files +
                "\n\nThey are style reference only. Never copy a specific pin, its text, a logo or a "
                "character; extract principles.")
    else:
        look = "The Pinterest moodboard could not be loaded; rely on your own knowledge of current poster and ad design."
    out, meta = await run.call(
        "Brand kit and strategy:\n" +
        json.dumps({k: strategy[k] for k in ("big_idea", "positioning", "brand_kit")}, indent=2) +
        "\n\n" + look +
        "\n\nReturn: observed (what the strongest references do: layout moves, type treatments, color, "
        "texture), category_cliches (what every ad in this category does that we must avoid), and "
        f"exactly {VARIANTS} art directions for this brand's ads. A direction is a VISUAL LANGUAGE "
        "(layout system, type treatment, color, texture), not an ad concept: put no headlines, numbers "
        "or copy in it, because every ad brings its own message and concept. Each direction must be "
        "radically different from the others and from the category cliches, loud enough to stop a "
        "thumb, keep a message readable at phone size, and be buildable in pure HTML/CSS/SVG (no "
        "photography or photo cutouts exist in this medium; illustrate instead). In css_techniques name concrete techniques (e.g. 400px "
        "condensed type bleeding off canvas, -webkit-text-stroke outlines, mix-blend-mode multiply, "
        "clip-path starburst, SVG feTurbulence grain, repeating-radial-gradient halftone, stacked "
        "text-shadow 3D, rotated sticker badges, cut paper collage).",
        DIRECTION, CREATIVE_SYSTEM, model="opus", tools=("Read",) if boards else (),
        add_dirs=(run.dir / "moodboard",) if boards else (), effort="high")
    out["boards"] = boards
    return out, meta


def creative_prompt(ad, strategy, direction, extra=""):
    return (
        "Brand kit:\n" + json.dumps(strategy["brand_kit"], indent=2) +
        "\n\nAd (the copy shown beside the image):\n" + json.dumps(ad, indent=2) +
        "\n\nVisual language for THIS design (style only; the idea and the words come from the ad's "
        "creative_concept and copy):\n" + json.dumps(direction, indent=2) +
        "\n\nDesign the 1080x1080 feed image as one HTML document. Technical rules:\n"
        "- html, body: width:1080px; height:1080px; margin:0; overflow:hidden.\n"
        "- One <style> block. Google Fonts via <link> allowed. NO <img>, NO external files, NO JavaScript.\n"
        "- All imagery is CSS and inline SVG: illustrate the product and scene yourself.\n"
        "- Decorative giant type or shapes MAY bleed off the canvas; put data-bleed on those elements.\n"
        "- The message (at most 6 words) and the brand name must be fully legible, inside 60px margins, "
        "and never carry data-bleed.\n"
        "Color: the brand kit anchors the design, but add one or two loud accent colors (fluorescent, "
        "electric) if the visual language calls for it. A polite cream layout is a failure.\n"
        "Creative bar: this has to be the loudest, most crafted thing in the feed. Commit fully to the "
        "direction, use scale contrast (something enormous, something tiny), layering, texture and at "
        "least three of the direction's css techniques. Loud never means broken: no message text may "
        "sit under another element. Do not repeat the headline word for word.\n"
        "Return the full HTML document." + extra)


def add_meta(tot, meta):
    for k in ("seconds", "turns", "input_tokens", "output_tokens", "list_price_usd"):
        tot[k] = round(tot.get(k, 0) + (meta[k] or 0), 4)
    tot["model"] = meta["model"]
    tot["web_searches"] = 0
    return tot


async def design(run, i, tag, ad, strategy, direction, extra="", model="sonnet", effort="medium", looks_at=None):
    """One HTML design rendered to creatives/m{i}_{tag}.png, with one overflow retry.
    looks_at: a PNG the designer opens with the Read tool before revising (its own previous render)."""
    name = f"creative_{i}_{tag}"
    base = run.dir / "creatives" / f"m{i}_{tag}"
    see = {}
    if looks_at:
        see = {"tools": ("Read",), "add_dirs": (run.dir / "creatives",)}
        extra = (f"\n\nFIRST use the Read tool to LOOK at the current render: {run.dir / looks_at}. "
                 "Judge it with your own eyes before changing anything." + extra)

    async def go():
        tot, note = {"attempts": 0}, extra
        for attempt in range(1, 3):
            out, meta = await run.call(creative_prompt(ad, strategy, direction, note), HTML, CREATIVE_SYSTEM,
                                       model=model, effort=effort, **see)
            add_meta(tot, meta)
            tot["attempts"] = attempt
            base.with_suffix(".html").write_text(out["html"], encoding="utf-8")
            overflow = await asyncio.to_thread(render_png, out["html"], base.with_suffix(".png"))
            if not overflow:
                break
            last = attempt == 2
            run.emit(type="step", step=name, status="retry",
                     label="Still has off canvas, clipped or covered text; sending to judge flagged" if last
                     else "Message text off canvas, clipped or covered, redesigning",
                     detail="\n".join(overflow[:6]))
            if last:
                break
            note = (extra + "\n\nYour last design rendered with message text outside the canvas or clipped "
                    "(measured in a real browser):\n- " + "\n- ".join(overflow[:10]) +
                    "\nKeep the message inside the safe area. Only data-bleed elements may leave the canvas.")
        result = {"tag": str(tag), "direction": direction["name"], "png": f"creatives/{base.name}.png"}
        if overflow:
            result["overflow"] = overflow
        return result, tot

    return await run.step(name, f"Ad {i}, design {tag}: {direction['name']}", go())


async def tournament(run, i, ad, strategy, direction):
    dirs = direction["directions"][:VARIANTS]
    variants = list(await asyncio.gather(*(design(run, i, k, ad, strategy, d) for k, d in enumerate(dirs, 1))))

    listing = "\n".join(f"- variant {k}: {v['png'].replace('creatives/', '')} (direction: {v['direction']})" +
                        (f"\n  browser measured: {'; '.join(v['overflow'][:4])}" if v.get("overflow") else "")
                        for k, v in enumerate(variants, 1))
    verdict = await run.step(f"judge_{i}", f"Judging {len(variants)} designs for ad {i}", run.call(
        "Brand kit:\n" + json.dumps(strategy["brand_kit"]) + "\n\nAd copy:\n" + json.dumps(ad, indent=2) +
        f"\n\nUse the Read tool to LOOK at each design (files in {run.dir / 'creatives'}):\n" + listing +
        "\n\nScore every variant 1 to 10 on stop_power (would a thumb stop in a busy feed), legibility "
        "(message and brand readable at phone size, nothing broken or overlapping by accident), "
        "brand_fit (does it express THIS ad's creative_concept and message) and craft, plus overall. "
        "The medium is fixed: pure HTML/CSS/SVG, no photography, so never ask for photos. "
        "Be a harsh creative director: 10 is award level, 7 is "
        "competent, 5 is template. Pick the winner by overall; a legibility problem disqualifies. In "
        "improve, give concrete changes that would take the winner to a 10.",
        JUDGE, CREATIVE_SYSTEM, model="opus", tools=("Read",), add_dirs=(run.dir / "creatives",), effort="medium"))

    scores = {int(x["variant"]): x for x in verdict["scores"]}
    for k, v in enumerate(variants, 1):
        v["score"] = scores.get(k)
    w = min(max(int(verdict["winner"]), 1), len(variants))
    win = variants[w - 1]
    best = win
    overall = (win["score"] or {}).get("overall", 0)

    if overall < 9:  # one polish pass on the winner with the judge's notes
        # the polish is done by opus, and it looks at its render instead of editing HTML blind
        polished = await design(run, i, "final", ad, strategy, dirs[w - 1], model="opus", effort="high",
                                looks_at=win["png"],
                                extra="\n\nThis design already beat the other variants. Creative director notes "
                                      "to take it to a 10:\n" + verdict["improve"] +
                                "\n\nThe current winning HTML, keep what works:\n" +
                                (run.dir / win["png"]).with_suffix(".html").read_text(encoding="utf-8"))
        polished["polish_of"] = win["tag"]
        variants.append(polished)
        # a polish pass can make things worse, so it has to beat the original head to head
        rematch = await run.step(f"rematch_{i}", f"Polished vs original for ad {i}", run.call(
            "Ad copy:\n" + json.dumps(ad, indent=2) +
            f"\n\nUse the Read tool to LOOK at both designs (files in {run.dir / 'creatives'}):\n"
            f"- variant 1: {win['png'].replace('creatives/', '')} (original winner)\n"
            f"- variant 2: {polished['png'].replace('creatives/', '')} (polished)\n\n"
            "The polish was meant to apply these notes:\n" + verdict["improve"] +
            "\n\nScore both the same way (1 to 10, harsh). Anything newly covered, clipped or harder to read "
            "counts heavily against the polished version. Winner is 1 or 2.",
            JUDGE, CREATIVE_SYSTEM, model="opus", tools=("Read",), add_dirs=(run.dir / "creatives",), effort="medium"))
        rs = {int(x["variant"]): x for x in rematch["scores"]}
        polished["score"] = rs.get(2)
        if int(rematch["winner"]) == 2:
            best, overall = polished, (rs.get(2) or {}).get("overall", overall)
        else:
            polished["lost_rematch"] = True

    best["winner"] = True
    shutil.copyfile(run.dir / best["png"], run.dir / "creatives" / f"m{i}.png")
    return {"id": f"m{i}", "png": f"creatives/m{i}.png", "direction": best["direction"],
            "direction_index": w - 1, "judge_score": overall, "improve": verdict["improve"], "variants": variants}


async def fix_meta(run, i, verdict, meta, creatives, strategy, ctx):
    ad = meta["ads"][i - 1]
    feedback = "\n\nCritic feedback to fix:\n- " + "\n- ".join(verdict["issues"]) + "\nInstructions: " + verdict["instructions"]
    if verdict["fix"] in ("copy", "both"):
        new = await run.step(f"meta_fix_{i}", f"Rewriting copy for ad {i} from critic notes", run.validated(
            f"meta_fix_{i}", ctx + "\n\nCurrent ad:\n" + json.dumps(ad, indent=2) + feedback +
            "\n\nReturn the improved ad. Hard limits: primary_text 125, headline 40, description 30 chars.",
            META_AD, SYSTEM, meta_violations, model="sonnet", effort="medium"))
        new["segment_id"] = ad["segment_id"]
        meta["ads"][i - 1] = ad = new
    if verdict["fix"] in ("creative", "both"):
        c = creatives[i - 1]
        d = run.state["direction"]["directions"][c.get("direction_index", 0)]
        v = await design(run, i, "fix", ad, strategy, d, extra=feedback)
        c.setdefault("variants", []).append(v)
        shutil.copyfile(run.dir / v["png"], run.dir / "creatives" / f"m{i}.png")
    creatives[i - 1]["fixed"] = verdict["issues"]


async def fix_google(run, gi, verdict, google, ctx):
    grp = google["ad_groups"][gi - 1]
    one = {**google, "ad_groups": [grp]}
    new = await run.step(f"google_fix_{gi}", f"Rewriting Google ad group {gi} from critic notes", run.validated(
        f"google_fix_{gi}", ctx + "\n\nCurrent ad group (return a campaign object with just this one ad group):\n" +
        json.dumps(one, indent=2) + "\n\nCritic feedback:\n- " + "\n- ".join(verdict["issues"]) +
        "\nInstructions: " + verdict["instructions"] +
        "\n\nKeep exactly 15 headlines of 30 chars max and 4 descriptions of 90 chars max.",
        GOOGLE, SYSTEM, google_violations, model="sonnet", effort="medium"))
    google["ad_groups"][gi - 1] = new["ad_groups"][0]


# ---------- deterministic assembly ----------

LEAD_SCRIPT = """
<script>
document.addEventListener('submit', async (e) => {
  const f = e.target; if (!f.matches('form')) return;
  e.preventDefault();
  const data = Object.fromEntries(new FormData(f));
  data.utm = Object.fromEntries(new URLSearchParams(location.search));
  await fetch('/api/runs/%s/leads', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});
  f.innerHTML = '<p style="font-size:1.2em">Thanks, you are on the list.</p>';
});
</script>
"""


def write_landing(run, html):
    # The model writes the page; the lead capture wiring is ours, so it always works.
    script = LEAD_SCRIPT % run.id
    html = html.replace("</body>", script + "</body>") if "</body>" in html else html + script
    (run.dir / "landing" / "index.html").write_text(html, encoding="utf-8")


def build_plan(run, brief, strategy, meta, google):
    budget = float(brief["monthly_budget_usd"] or 0)
    total_share = sum(c["budget_share"] for c in strategy["channels"]) or 1
    camp = slug(brief["brand_name"] + "-" + brief["product"])
    landing = f"{BASE_URL}/runs/{run.id}/landing/index.html"
    channels = [{**c, "monthly_usd": round(budget * c["budget_share"] / total_share),
                 "daily_usd": round(budget * c["budget_share"] / total_share / 30, 2)}
                for c in strategy["channels"]]
    meta_links = [f"{landing}?utm_source=meta&utm_medium=paid_social&utm_campaign={camp}&utm_content={a['segment_id']}"
                  for a in meta["ads"]]
    google_links = [f"{landing}?utm_source=google&utm_medium=cpc&utm_campaign={camp}&utm_content={quote(slug(g['name']))}"
                    for g in google["ad_groups"]]
    return {"campaign_slug": camp, "monthly_budget_usd": budget, "channels": channels,
            "meta_links": meta_links, "google_links": google_links, "export": f"/api/runs/{run.id}/export.zip"}


def export(run):
    s = run.state
    plan = s["plan"]
    segs = {x["id"]: x for x in s["strategy"]["segments"]}
    meta_share = next((c for c in plan["channels"] if c["channel"] == "meta"), {"daily_usd": 0})
    per_ad_daily = round(meta_share["daily_usd"] / max(len(s["meta"]["ads"]), 1), 2)

    with open(run.dir / "meta_ads.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Campaign Name", "Ad Set Name", "Daily Budget", "Age Min", "Age Max", "Locations", "Interests",
                    "Ad Name", "Primary Text", "Headline", "Description", "Call to Action", "Image File", "Website URL"])
        for i, (ad, link) in enumerate(zip(s["meta"]["ads"], plan["meta_links"]), 1):
            t = segs.get(ad["segment_id"], {}).get("meta_targeting", {})
            w.writerow([plan["campaign_slug"], segs.get(ad["segment_id"], {}).get("name", ad["segment_id"]), per_ad_daily,
                        int(t.get("age_min", 18)), int(t.get("age_max", 65)), "; ".join(t.get("locations", [])),
                        "; ".join(t.get("interests", [])), f"ad-{i}", ad["primary_text"], ad["headline"],
                        ad["description"], ad["cta"], f"m{i}.png", link])

    with open(run.dir / "google_ads.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        head = ["Campaign", "Ad Group", "Row Type", "Keyword", "Criterion Type"] + \
               [f"Headline {n}" for n in range(1, 16)] + [f"Description {n}" for n in range(1, 5)] + \
               ["Path 1", "Path 2", "Final URL"]
        w.writerow(head)
        blank = [""] * (15 + 4 + 3)
        for g, link in zip(s["google"]["ad_groups"], plan["google_links"]):
            for k in g["keywords"]:
                w.writerow([s["google"]["campaign_name"], g["name"], "Keyword", k["text"], k["match"].title()] + blank)
            w.writerow([s["google"]["campaign_name"], g["name"], "Ad", "", ""] +
                       (g["headlines"] + [""] * 15)[:15] + (g["descriptions"] + [""] * 4)[:4] +
                       [g["path1"], g["path2"], link])
        for n in s["google"]["negative_keywords"]:
            w.writerow([s["google"]["campaign_name"], "", "Negative keyword", n, "Phrase"] + blank)

    keep = {k: s.get(k) for k in ("description", "brief", "research", "strategy", "meta", "google",
                                   "creatives", "critic", "plan")}
    (run.dir / "campaign.json").write_text(json.dumps(keep, indent=2), encoding="utf-8")
    with zipfile.ZipFile(run.dir / "export.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for name in ("campaign.json", "meta_ads.csv", "google_ads.csv", "landing/index.html"):
            z.write(run.dir / name, name)
        for png in sorted((run.dir / "creatives").glob("*.png")):
            z.write(png, f"creatives/{png.name}")
