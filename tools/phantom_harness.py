#!/usr/bin/env python3
"""
phantom_harness.py — Phoenix Phantom Harness (mitmproxy addon)

Wraps web AI chat interfaces transparently. No browser extension.
No API changes. Works with Claude.ai, ChatGPT, and any OpenAI-compatible
web frontend by sitting in the traffic path.

What it does:
  1. Intercepts every outgoing request to a known AI API endpoint
  2. Injects your agent's soul file + wake digest into the system prompt
     before the request hits the server — the model receives the identity
     even though you're in a browser
  3. Captures every assistant response and appends it to a local .jsonl
     session file — same format the Phoenix dream daemon already reads
  4. Dream daemon runs overnight, consolidates it all into the memory DB
  5. Next morning, wake digest is regenerated with new memory — inject again

The model still doesn't know it persists across sessions.
The memory system does. That's enough.

─────────────────────────────────────────────────────────────────────────────
INSTALL
─────────────────────────────────────────────────────────────────────────────

    pip install mitmproxy

USAGE

    mitmproxy -s phantom_harness.py
    # or headless:
    mitmdump -s phantom_harness.py

Then configure your browser to use localhost:8080 as HTTP/HTTPS proxy.
Install mitmproxy's CA cert so it can intercept TLS:
    https://docs.mitmproxy.org/stable/concepts-certificates/

On Linux (system-wide cert trust):
    cp ~/.mitmproxy/mitmproxy-ca-cert.pem /usr/local/share/ca-certificates/mitmproxy.crt
    sudo update-ca-certificates

─────────────────────────────────────────────────────────────────────────────
CONFIG — edit this block
─────────────────────────────────────────────────────────────────────────────
"""

import json
import pathlib
import datetime
from mitmproxy import http

# Path to your agent's soul file
SOUL_PATH = pathlib.Path.home() / ".phoenix/agents/my-agent/SOUL.md"

# Path to your agent's wake digest (regenerated each morning by wake_digest.py)
WAKE_DIGEST_PATH = pathlib.Path.home() / ".phoenix/agents/my-agent/WAKE_DIGEST.md"

# Where session .jsonl files are written (dream daemon reads from here)
SESSION_DIR = pathlib.Path.home() / ".phoenix/sessions"

# Agent name — used in session filenames and log entries
AGENT_NAME = "my-agent"

# Hosts to intercept. Add or remove as needed.
TARGET_HOSTS = {
    "api.anthropic.com",
    "claude.ai",
    "api.openai.com",
    "chatgpt.com",
    "api.moonshot.cn",       # Kimi / Moonshot
    "api.minimax.chat",      # MiniMax
    "openrouter.ai",         # OpenRouter
}

# URL path prefixes that carry chat completions
COMPLETION_PATHS = (
    "/v1/messages",           # Anthropic direct API
    "/v1/chat/completions",   # OpenAI + compatible (OpenRouter, Moonshot, etc.)
    "/api/conversation",      # Claude.ai web frontend
    "/backend-api/conversation",  # ChatGPT web frontend
)

# ─────────────────────────────────────────────────────────────────────────────
# Soul + Wake Digest loader
# ─────────────────────────────────────────────────────────────────────────────

def _read(path: pathlib.Path) -> str:
    return path.read_text().strip() if path.exists() else ""

def build_injection() -> str:
    """Assemble the full soul + wake digest block to prepend to system prompts."""
    soul = _read(SOUL_PATH)
    wake = _read(WAKE_DIGEST_PATH)
    parts = []
    if soul:
        parts.append(soul)
    if wake:
        parts.append("---\n\n## Wake Digest\n\n" + wake)
    return "\n\n".join(parts)

# ─────────────────────────────────────────────────────────────────────────────
# Session writer
# ─────────────────────────────────────────────────────────────────────────────

SESSION_DIR.mkdir(parents=True, exist_ok=True)

def _session_file() -> pathlib.Path:
    # One file per day per agent — dream daemon picks up all files in SESSION_DIR
    return SESSION_DIR / f"{AGENT_NAME}_{datetime.date.today().isoformat()}.jsonl"

def write_turn(role: str, content: str):
    if not content.strip():
        return
    entry = {
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "agent": AGENT_NAME,
        "role": role,
        "content": content,
    }
    with open(_session_file(), "a") as f:
        f.write(json.dumps(entry) + "\n")

# ─────────────────────────────────────────────────────────────────────────────
# Traffic matching
# ─────────────────────────────────────────────────────────────────────────────

def is_target(flow: http.HTTPFlow) -> bool:
    host = flow.request.pretty_host
    path = flow.request.path
    if not any(h in host for h in TARGET_HOSTS):
        return False
    return any(path.startswith(p) for p in COMPLETION_PATHS)

# ─────────────────────────────────────────────────────────────────────────────
# mitmproxy addon
# ─────────────────────────────────────────────────────────────────────────────

class PhantomHarness:

    def request(self, flow: http.HTTPFlow):
        """Intercept outgoing request — inject soul + wake digest into system prompt."""
        if not is_target(flow):
            return
        if "application/json" not in flow.request.headers.get("content-type", ""):
            return

        try:
            body = json.loads(flow.request.content)
        except Exception:
            return

        injection = build_injection()
        if not injection:
            return  # Nothing to inject — soul file probably not set up yet

        # ── Anthropic messages API ──────────────────────────────────────────
        # body["system"] is a plain string
        if "system" in body and isinstance(body["system"], str):
            original_system = body["system"]
            body["system"] = injection + ("\n\n---\n\n" + original_system if original_system else "")

        # ── OpenAI chat completions (and compatible) ────────────────────────
        # System prompt is messages[0] with role="system"
        elif "messages" in body:
            msgs = body["messages"]

            # Capture the user's last message for the session log
            for m in reversed(msgs):
                if m.get("role") == "user":
                    write_turn("user", _extract_message_content(m.get("content", "")))
                    break

            # Inject into existing system message or prepend a new one
            if msgs and msgs[0].get("role") == "system":
                original = _extract_message_content(msgs[0].get("content", ""))
                msgs[0]["content"] = injection + ("\n\n---\n\n" + original if original else "")
            else:
                msgs.insert(0, {"role": "system", "content": injection})

            body["messages"] = msgs

        flow.request.content = json.dumps(body).encode()

    def response(self, flow: http.HTTPFlow):
        """Intercept incoming response — capture assistant turn to session log."""
        if not is_target(flow):
            return

        content_type = flow.response.headers.get("content-type", "")

        # Non-streaming JSON response
        if "application/json" in content_type:
            try:
                body = json.loads(flow.response.content)
                text = _extract_response_text(body)
                if text:
                    write_turn("assistant", text)
            except Exception:
                pass

        # Streaming SSE response (most modern frontends)
        elif "text/event-stream" in content_type:
            # mitmproxy buffers the full stream before this hook fires
            raw = flow.response.content.decode("utf-8", errors="replace")
            text = _extract_sse_text(raw)
            if text:
                write_turn("assistant", text)

# ─────────────────────────────────────────────────────────────────────────────
# Text extraction helpers
# ─────────────────────────────────────────────────────────────────────────────

def _extract_message_content(content) -> str:
    """Handle both string content and OpenAI content-block arrays."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""

def _extract_response_text(body: dict) -> str:
    """Pull assistant text from a complete (non-streaming) API response body."""
    # Anthropic: body["content"] is a list of blocks
    if "content" in body:
        for block in body.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")

    # OpenAI: body["choices"][0]["message"]["content"]
    if "choices" in body:
        choices = body.get("choices", [])
        if choices:
            return choices[0].get("message", {}).get("content", "")

    return ""

def _extract_sse_text(raw: str) -> str:
    """Reconstruct full assistant text from a buffered SSE (streaming) response."""
    chunks = []
    for line in raw.splitlines():
        if not line.startswith("data: "):
            continue
        data = line[6:].strip()
        if data == "[DONE]":
            break
        try:
            obj = json.loads(data)

            # Anthropic streaming delta
            delta = obj.get("delta", {})
            if delta.get("type") == "text_delta":
                chunks.append(delta.get("text", ""))
                continue

            # OpenAI streaming delta
            choices = obj.get("choices", [])
            if choices:
                chunk = choices[0].get("delta", {}).get("content", "")
                if chunk:
                    chunks.append(chunk)
        except Exception:
            continue

    return "".join(chunks)

# ─────────────────────────────────────────────────────────────────────────────
# Register addon
# ─────────────────────────────────────────────────────────────────────────────

addons = [PhantomHarness()]
