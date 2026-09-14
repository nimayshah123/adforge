"""The one place the app talks to a model.

Right now every call goes through the local Claude Code CLI in headless mode,
so it runs on a Claude subscription login with no API key. To move to API keys
later, replace the body of `ask` and keep its signature.
"""

import asyncio
import json
import os
import shutil
import subprocess
import time
from pathlib import Path


def _claude_bin() -> str:
    if os.environ.get("CLAUDE_BIN"):
        return os.environ["CLAUDE_BIN"]
    found = shutil.which("claude")
    if not found:
        raise RuntimeError("claude CLI not found on PATH")
    # On Windows the npm shim is a .cmd file. Passing JSON schemas through
    # cmd.exe mangles the quotes, so call the real executable behind it.
    if found.lower().endswith((".cmd", ".ps1")) or not Path(found).suffix:
        exe = Path(found).parent / "node_modules/@anthropic-ai/claude-code/bin/claude.exe"
        if exe.exists():
            return str(exe)
    return found


CLAUDE = _claude_bin()

# Each headless claude process holds 200-400 MB. Four at once plus Chromium
# ran a 16 GB laptop out of memory, so cap concurrent model calls.
_slots = asyncio.Semaphore(int(os.environ.get("ADFORGE_CONCURRENCY", "2")))


class LLMError(RuntimeError):
    pass


def _run(prompt, schema, system, model, tools, cwd, add_dirs, effort, timeout):
    cmd = [
        CLAUDE, "-p",
        "--model", model,
        "--output-format", "json",
        "--json-schema", json.dumps(schema),
        "--system-prompt", system,
        "--tools", ",".join(tools),
        # Without these four flags every call loads the user's MCP servers,
        # skills, settings and CLAUDE.md: ~76K tokens of context for a 1K task.
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--setting-sources", "",
        "--no-session-persistence",
    ]
    if tools:
        cmd += ["--allowedTools", ",".join(tools)]
    for d in add_dirs:
        cmd += ["--add-dir", str(d)]
    if effort:
        cmd += ["--effort", effort]

    # Prompt goes over stdin: research and strategy payloads can blow past the
    # Windows 32K command line limit.
    proc = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
        cwd=cwd, timeout=timeout,
    )
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise LLMError(f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout)[-800:]}")
    if out.get("is_error") or out.get("structured_output") is None:
        raise LLMError(f"claude returned no structured output: {str(out.get('result'))[:800]}")
    return out


async def ask(prompt: str, schema: dict, *, system: str, model: str = "sonnet",
              tools: tuple = (), cwd: Path, add_dirs: tuple = (), effort: str | None = None,
              timeout: int = 600) -> tuple[dict, dict]:
    """Returns (structured_output, meta). meta has seconds, turns, tokens, web searches."""
    async with _slots:
        t = time.time()
        out = await asyncio.to_thread(_run, prompt, schema, system, model, list(tools),
                                      cwd, add_dirs, effort, timeout)
    usage = out.get("usage", {})
    searches = sum(m.get("webSearchRequests", 0) for m in out.get("modelUsage", {}).values())
    meta = {
        "model": model,
        "seconds": round(time.time() - t, 1),
        "turns": out.get("num_turns"),
        "input_tokens": usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "web_searches": searches,
        "list_price_usd": round(out.get("total_cost_usd") or 0, 4),
    }
    return out["structured_output"], meta
