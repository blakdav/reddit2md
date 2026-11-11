"""Talking to the two models: a local one through Ollama, and Claude.

Both take a system prompt plus a list of {"role", "content"} messages and
return (text, usage, model, blocks). `on_text` gets the answer so far while it
streams, so the page can show it growing. `blocks` is Claude's raw content,
kept so a follow-up question can replay the turn unchanged (Claude's
multi-turn rules require it); it is None for local answers.

Same settings and behaviour as Vidscribe's summaries and chat.
"""

import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger("reddit2md.llm")


def setting(name, default=""):
    """An environment setting; the compose file's "your-..." placeholders count as unset."""
    value = os.environ.get(name, "").strip()
    return default if not value or "your-" in value else value


OLLAMA_URL = setting("OLLAMA_URL").rstrip("/")
OLLAMA_MODEL = setting("OLLAMA_MODEL", "qwen3:14b")
OLLAMA_NUM_CTX = int(setting("OLLAMA_NUM_CTX", "32768"))
CLAUDE_MODEL = setting("CLAUDE_MODEL", "claude-opus-5-5")
ANTHROPIC_KEY = setting("ANTHROPIC_API_KEY")

# USD per million tokens: (input, output, cache write 5m, cache read), for the cost shown in the UI.
CLAUDE_RATES = {
    "claude-opus-5-5": (4.0, 20.0, 5.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 2.5, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
}

KINDS = ("cloud", "local")


def available():
    return {
        "local": {"enabled": bool(OLLAMA_URL), "model": OLLAMA_MODEL if OLLAMA_URL else None,
                  "chars": local_chars()},
        "cloud": {"enabled": bool(ANTHROPIC_KEY), "model": CLAUDE_MODEL if ANTHROPIC_KEY else None},
    }


def local_chars():
    """Roughly how many characters of prompt fit the local context, leaving room for the answer."""
    return int((OLLAMA_NUM_CTX - 4000) * 3.5)


def check(kind):
    if kind not in KINDS:
        raise ValueError("kind must be cloud or local")
    if not available()[kind]["enabled"]:
        raise ValueError(("Claude" if kind == "cloud" else "Local AI") + " isn't set up on the server")


def ask(kind, system, messages, on_text=None, keep_alive=0, cache=False, max_tokens=16000):
    check(kind)
    if kind == "local":
        return _local(system, messages, on_text or (lambda t: None), keep_alive)
    return _cloud(system, messages, on_text or (lambda t: None), cache, max_tokens)


# --------------------------------------------------------------------------
# Local: Ollama's native chat API, streamed. (Its OpenAI-compatible endpoint
# can't set num_ctx, and Ollama's default context is too short for a thread.)
# --------------------------------------------------------------------------

def _local(system, messages, on_text, keep_alive):
    body = {
        "model": OLLAMA_MODEL,
        "stream": True,
        # 0 frees the VRAM right away: the GPU is shared with Vidscribe.
        "keep_alive": keep_alive,
        "think": False,
        "options": {"num_ctx": OLLAMA_NUM_CTX, "temperature": 0.3},
        "messages": [{"role": "system", "content": system}]
                    + [{"role": m["role"], "content": m["content"] if isinstance(m["content"], str) else _text(m["content"])}
                       for m in messages],
    }

    def post(b):
        req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=json.dumps(b).encode(),
                                     headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=1800)

    try:
        try:
            res = post(body)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code == 400 and "think" in detail:
                body.pop("think")  # older Ollama, or a model without a thinking switch
                res = post(body)
            elif e.code == 404 and "not found" in detail:
                raise RuntimeError(f"Ollama doesn't have the model {OLLAMA_MODEL}. "
                                   f"Run: docker exec ollama ollama pull {OLLAMA_MODEL}") from e
            else:
                raise RuntimeError(f"Ollama returned HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Can't reach Ollama at {OLLAMA_URL}: {e.reason}") from e

    text, usage = "", {}
    with res:
        for line in res:  # newline-delimited JSON chunks
            if not line.strip():
                continue
            chunk = json.loads(line)
            if chunk.get("error"):
                raise RuntimeError(f"Ollama: {chunk['error']}")
            piece = (chunk.get("message") or {}).get("content", "")
            if piece:
                text += piece
                on_text(text)
            if chunk.get("done"):
                usage = {"input": chunk.get("prompt_eval_count"), "output": chunk.get("eval_count")}
    if not text.strip():
        raise RuntimeError("Ollama returned an empty answer.")
    return text.strip(), usage, OLLAMA_MODEL, None


def _text(blocks):
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


# --------------------------------------------------------------------------
# Cloud: Claude
# --------------------------------------------------------------------------

def _cloud(system, messages, on_text, cache, max_tokens):
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    # A big thread is a fixed prefix for every follow-up question: cache it,
    # and let the top-level marker cache the growing conversation too.
    sys_block = {"type": "text", "text": system}
    extra = {}
    if cache:
        sys_block["cache_control"] = {"type": "ephemeral"}
        extra["cache_control"] = {"type": "ephemeral"}
    try:
        # Streaming, so a long thread can't hit an HTTP timeout. If a safety
        # classifier declines, fallbacks="default" re-runs the request on
        # Anthropic's recommended fallback model instead of failing.
        with client.beta.messages.stream(
            model=CLAUDE_MODEL,
            max_tokens=max_tokens,
            system=[sys_block],
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=messages,
            **extra,
        ) as stream:
            text = ""
            for piece in stream.text_stream:
                text += piece
                on_text(text)
            message = stream.get_final_message()
    except anthropic.AuthenticationError as e:
        raise RuntimeError("Claude rejected the API key (ANTHROPIC_API_KEY).") from e
    except anthropic.PermissionDeniedError as e:
        raise RuntimeError(f"The API key can't use {CLAUDE_MODEL}: {e.message}") from e
    except anthropic.NotFoundError as e:
        raise RuntimeError(f"Unknown Claude model {CLAUDE_MODEL}.") from e
    except anthropic.RateLimitError as e:
        raise RuntimeError("Claude rate limit reached; try again in a minute.") from e
    except anthropic.BadRequestError as e:
        raise RuntimeError(f"Claude rejected the request: {e.message}") from e
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"Claude API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise RuntimeError("Can't reach the Claude API; check the server's internet access.") from e

    if message.stop_reason == "refusal":
        raise RuntimeError("Claude declined to answer this.")
    text = "".join(b.text for b in message.content if b.type == "text").strip()
    if not text:
        raise RuntimeError(f"Claude returned no text (stop reason: {message.stop_reason}).")
    if message.stop_reason == "max_tokens":
        text += "\n\n*(Cut off at the length limit.)*"

    # After a mid-answer fallback only the text is safe to replay.
    blocks = None if any(b.type == "fallback" for b in message.content) else [b.to_dict() for b in message.content]

    u = message.usage
    usage = {"input": u.input_tokens, "output": u.output_tokens,
             "cache_write": u.cache_creation_input_tokens or 0, "cache_read": u.cache_read_input_tokens or 0}
    rates = CLAUDE_RATES.get(message.model) or CLAUDE_RATES.get(CLAUDE_MODEL)
    if rates:
        usage["cost_usd"] = round((usage["input"] * rates[0] + usage["output"] * rates[1]
                                   + usage["cache_write"] * rates[2] + usage["cache_read"] * rates[3]) / 1e6, 4)
    return text, usage, message.model, blocks
