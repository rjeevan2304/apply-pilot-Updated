"""
Unified LLM client for ApplyPilot.

Auto-detects provider from environment:
  GEMINI_API_KEY  -> Google Gemini (default: gemini-3.5-flash)
  OPENAI_API_KEY  -> OpenAI (default: gpt-4o-mini)
  LLM_URL         -> Local llama.cpp / Ollama compatible endpoint
  ANTHROPIC_API_KEY -> Claude API: the automatic fallback when Gemini/OpenAI/local
                     is rate limited or out of quota; the main provider if it's the
                     only key (or LLM_PROVIDER=anthropic). ANTHROPIC_MODEL picks the model.
  LLM_PROVIDER=kie + KIE_API_KEY -> kie.ai (Claude / DeepSeek), with
                     LLM_FALLBACK_MODEL used on token/credit limits

LLM_MODEL env var overrides the model name for any provider.
"""

import logging
import os
import json
import random
import subprocess
import threading
import time

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

def _detect_provider() -> tuple[str, str, str]:
    """Return (base_url, model, api_key) based on environment variables.

    Reads env at call time (not module import time) so that load_env() called
    in _bootstrap() is always visible here.
    """
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    local_url = os.environ.get("LLM_URL", "")
    model_override = os.environ.get("LLM_MODEL", "")

    if gemini_key and not local_url:
        return (
            "https://generativelanguage.googleapis.com/v1beta/openai",
            model_override or "gemini-3.5-flash",
            gemini_key,
        )

    if openai_key and not local_url:
        return (
            "https://api.openai.com/v1",
            model_override or "gpt-4o-mini",
            openai_key,
        )

    if local_url:
        return (
            local_url.rstrip("/"),
            model_override or "local-model",
            os.environ.get("LLM_API_KEY", ""),
        )

    raise RuntimeError(
        "No LLM provider configured. "
        "Set GEMINI_API_KEY, OPENAI_API_KEY, or LLM_URL in your environment."
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class ClaudeCLIError(RuntimeError):
    """Claude Code CLI invocation or response failed."""


def claude_cli_path() -> str | None:
    """Locate the `claude` binary, including where its installer puts it.

    Scheduled services (systemd) often lack ~/.local/bin on PATH, so check the
    usual install locations too. CLAUDE_BIN overrides everything.
    """
    import shutil
    from pathlib import Path

    env = os.environ.get("CLAUDE_BIN", "").strip()
    if env:
        return env
    found = shutil.which("claude")
    if found:
        return found
    for cand in (Path.home() / ".local/bin/claude", Path.home() / ".claude/local/claude",
                 Path("/usr/local/bin/claude")):
        if cand.exists():
            return str(cand)
    return None


# Messages the CLI gives when the subscription's usage limit is reached.
_CLI_LIMIT_HINTS = ("usage limit", "limit reached", "out of extra usage",
                    "hit your limit", "limit will reset", "resets at")
_CLI_AUTH_HINTS = ("not logged in", "please run /login", "invalid api key", "authentication",
                   "oauth token", "login")


class ClaudeCLIClient:
    """Text-only LLM client backed by the authenticated Claude Code CLI.

    Works with a Claude subscription (Pro/Max) after `claude` has been logged
    in once on this machine; no API credit needed. Model: CLAUDE_MODEL (an
    alias such as haiku / sonnet / opus, or a full model ID).
    """

    def __init__(self, model: str = "sonnet") -> None:
        self.model = model
        self.timeout = int(os.environ.get("CLAUDE_TIMEOUT", "240"))
        self.binary = claude_cli_path() or "claude"

    @staticmethod
    def _classify(detail: str) -> None:
        low = detail.lower()
        if any(h in low for h in _CLI_LIMIT_HINTS):
            raise ClaudeUsageLimit(f"Claude subscription usage limit reached: {detail[:500]}")
        if any(h in low for h in _CLI_AUTH_HINTS):
            raise ClaudeCLIError(
                "Claude Code is not logged in on this machine. Run `claude` once in a terminal, "
                f"log in with your Claude account, then retry. Details: {detail[:300]}")

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        # Preserve the existing system/user message structure in one prompt.
        sections = []
        for message in messages:
            role = message.get("role", "user").upper()
            content = message.get("content", "")
            sections.append(f"## {role}\n{content}")
        prompt = "\n\n".join(sections)

        command = [
            self.binary, "-p",
            "--model", self.model,
            "--output-format", "json",
            "--tools", "",
            "--strict-mcp-config",
            "--no-session-persistence",
        ]

        try:
            completed = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ClaudeCLIError(
                f"Claude CLI timed out after {self.timeout} seconds"
            ) from exc
        except OSError as exc:
            raise ClaudeCLIError(
                f"Could not start Claude CLI: {exc}"
            ) from exc

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            try:  # JSON error bodies carry the message in "result"
                detail = json.loads(completed.stdout).get("result") or detail
            except Exception:
                pass
            self._classify(str(detail))
            raise ClaudeCLIError(
                f"Claude CLI exited with code {completed.returncode}: {detail[:1500]}"
            )

        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ClaudeCLIError(
                f"Claude CLI returned invalid JSON: {completed.stdout[:1000]}"
            ) from exc

        if response.get("is_error"):
            self._classify(str(response.get("result", "")))
            raise ClaudeCLIError(
                f"Claude CLI reported an error: {response.get('result', 'Unknown error')}"
            )

        result = response.get("result")
        if not isinstance(result, str) or not result.strip():
            raise ClaudeCLIError("Claude CLI returned an empty response")

        return result.strip()

    def ask(self, prompt: str, **kwargs) -> str:
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        pass


class KieLimitError(RuntimeError):
    """kie.ai refused the request because of a token, context or credit limit."""


# Phrases kie.ai / upstream models use when a limit (not a transient error) is hit.
_LIMIT_HINTS = (
    "token", "context length", "context_length", "too long", "max_tokens", "maximum context",
    "credit", "insufficient", "balance", "quota", "exceeded your", "limit exceeded",
)


def _is_limit_error(status: int, text: str) -> bool:
    if status in (402, 413):
        return True
    if status in (400, 403, 422, 429):
        low = text.lower()
        return any(h in low for h in _LIMIT_HINTS)
    return False


class KieClaudeClient:
    """kie.ai client (Claude via the Messages API, other models via the Responses API).

    POST https://api.kie.ai/claude/v1/messages with Bearer auth. kie.ai can
    return errors as HTTP 200 with a `code` field in the body, so check both.

    Fallback (LLM_FALLBACK_MODEL): used immediately when the request hits a
    token/context/credit limit, and after retries are exhausted on rate limits
    or server errors.
    """

    def __init__(self, model: str, api_key: str, url: str = "") -> None:
        self.model = model
        self.api_key = api_key
        # DeepSeek on kie.ai uses the OpenAI Responses API; Claude uses Messages.
        self.responses_api = not self.model.lower().startswith("claude")
        default = ("https://api.kie.ai/openai/v1/responses" if self.responses_api
                   else "https://api.kie.ai/claude/v1/messages")
        self.url = url or default
        self._client = httpx.Client(timeout=_TIMEOUT)
        self._is_fallback = False

    def _fallback(self, messages: list[dict], temperature: float, max_tokens: int, reason: str) -> str | None:
        fb = os.environ.get("LLM_FALLBACK_MODEL", "").strip()
        if not fb or fb == self.model or self._is_fallback:
            return None
        log.warning("kie.ai %s failed (%s); falling back to %s", self.model, reason[:160], fb)
        return _fallback_client(fb, self.api_key).chat(messages, temperature=temperature, max_tokens=max_tokens)

    def chat(self, messages: list[dict], temperature: float = 0.0, max_tokens: int = 4096) -> str:
        system = "\n\n".join(m.get("content", "") for m in messages if m.get("role") == "system")
        convo = [{"role": m["role"], "content": m.get("content", "")}
                 for m in messages if m.get("role") in ("user", "assistant")]
        if self.responses_api:
            payload: dict = {
                "model": self.model,
                "input": convo,
                # reasoning tokens count against this limit (thinking is on by default)
                "max_output_tokens": max_tokens + 4096,
                "stream": False,
            }
            if system:
                payload["instructions"] = system
        else:
            payload = {
                "model": self.model,
                "messages": convo,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": False,
            }
            if system:
                payload["system"] = system
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}

        last_err = "unknown"
        for attempt in range(_KIE_RETRIES):
            try:
                with _KIE_SLOTS:  # cap in-flight requests; kie.ai 429s on too many pending
                    resp = self._client.post(self.url, json=payload, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_err = f"{type(exc).__name__}"
                time.sleep(_kie_backoff(attempt))
                continue
            try:
                data = resp.json()
            except Exception:
                data = {}
            code = data.get("code") if isinstance(data, dict) else None
            status = code if isinstance(code, int) and code != 200 else resp.status_code

            # Token / context / credit limits: retrying the same model won't help.
            if _is_limit_error(status, resp.text):
                last_err = f"limit (HTTP/code {status}): {resp.text[:200]}"
                result = self._fallback(messages, temperature, max_tokens, last_err)
                if result is not None:
                    return result
                raise KieLimitError(f"kie.ai {self.model} hit a limit: {resp.text[:500]}")

            if status in (429, 500, 502, 503, 504):
                last_err = f"HTTP/code {status}: {resp.text[:200]}"
                time.sleep(_kie_backoff(attempt, resp.headers.get("Retry-After")))
                continue
            if status != 200:
                raise RuntimeError(f"kie.ai error {status}: {resp.text[:500]}")
            body = data.get("data", data) if isinstance(data, dict) else {}

            # Output cut off by the token limit (Responses API) with nothing usable.
            incomplete = (body.get("incomplete_details") or {}) if isinstance(body, dict) else {}
            truncated = (isinstance(body, dict) and body.get("status") == "incomplete"
                         and "token" in str(incomplete.get("reason", "")).lower())

            if isinstance(body, dict) and isinstance(body.get("output"), list):
                text = "".join(
                    c.get("text", "")
                    for item in body["output"] if isinstance(item, dict) and item.get("type") == "message"
                    for c in (item.get("content") or []) if isinstance(c, dict) and c.get("type") in ("output_text", "text")
                )
                if text.strip():
                    return text.strip()
                # Some responses carry the answer only inside a reasoning item.
                text = "".join(
                    c.get("text", "")
                    for item in body["output"] if isinstance(item, dict) and item.get("type") == "reasoning"
                    for c in (item.get("content") or []) if isinstance(c, dict)
                )
                if text.strip() and not truncated:
                    return text.strip()
            if isinstance(body, dict) and isinstance(body.get("output_text"), str) and body["output_text"].strip():
                return body["output_text"].strip()
            blocks = body.get("content") if isinstance(body, dict) else None
            if isinstance(blocks, list):
                text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type", "text") == "text")
                if text.strip():
                    return text.strip()
            choices = body.get("choices") if isinstance(body, dict) else None
            if choices:
                return choices[0]["message"]["content"].strip()
            if truncated:
                last_err = f"output token limit reached: {incomplete}"
                result = self._fallback(messages, temperature, max_tokens, last_err)
                if result is not None:
                    return result
                raise KieLimitError(f"kie.ai {self.model} ran out of output tokens")
            # Empty/incomplete body (e.g. status "incomplete" echoing the request): transient, retry.
            last_err = f"unexpected response shape: {resp.text[:200]}"
            time.sleep(_kie_backoff(attempt))
            continue

        # Retries exhausted: try the fallback model once, if configured and different.
        result = self._fallback(messages, temperature, max_tokens, last_err)
        if result is not None:
            return result
        raise RuntimeError(f"kie.ai request failed after retries: {last_err}")

    def ask(self, prompt: str, **kwargs) -> str:
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        self._client.close()


class LLMQuotaExhausted(RuntimeError):
    """A provider's hard quota / credit is used up; more requests won't succeed today."""


class GeminiQuotaExhausted(LLMQuotaExhausted):
    """Raised when Gemini reports a hard daily/monthly quota or billing limit."""


class GeminiRateLimited(RuntimeError):
    """Gemini is still rate limiting (429/503) after a retry; raised only when a fallback exists."""


class ClaudeCreditExhausted(LLMQuotaExhausted):
    """The Anthropic API account is out of credit or over its spend limit."""


class ClaudeUsageLimit(LLMQuotaExhausted):
    """The Claude subscription (used via Claude Code) hit its usage limit for now."""


def _is_hard_gemini_quota_error(resp: httpx.Response) -> bool:
    """Distinguish exhausted quotas from temporary request-rate limits."""
    try:
        body = resp.json()
        details = str(body).lower()
    except Exception:
        details = resp.text.lower()

    hard_limit_indicators = (
        "perday", "per_day", "per day",
        "daily quota", "quota for the day",
        "permonth", "per_month", "per month",
        "billing account", "billing is not enabled",
        "enable billing",
    )
    return any(term in details for term in hard_limit_indicators)


_MAX_RETRIES = 5
_TIMEOUT = 120  # seconds
_GEMINI_MIN_MAX_TOKENS = int(os.environ.get("GEMINI_MIN_MAX_TOKENS", "8192"))

# kie.ai client: more patient retries, jittered backoff, bounded concurrency.
_KIE_RETRIES = int(os.environ.get("KIE_RETRIES", "7"))
_KIE_SLOTS = threading.BoundedSemaphore(int(os.environ.get("KIE_MAX_CONCURRENCY", "2")))


def _kie_backoff(attempt: int, retry_after: str | None = None) -> float:
    """Seconds to wait before retry: honour Retry-After, else exponential (5s..90s) with jitter."""
    if retry_after:
        try:
            return min(float(retry_after), 120.0)
        except (TypeError, ValueError):
            pass
    base = min(5 * 2 ** attempt, 90)
    return base * (0.75 + random.random() * 0.5)


_fallback_clients: dict = {}


def _fallback_client(model: str, api_key: str):
    if model not in _fallback_clients:
        c = KieClaudeClient(model, api_key, os.environ.get("KIE_URL", ""))
        c._is_fallback = True
        _fallback_clients[model] = c
    return _fallback_clients[model]


# Base wait on first 429/503 (doubles each retry, caps at 60s).
# Gemini free tier is 15 RPM = 4s minimum between requests; 10s gives headroom.
_RATE_LIMIT_BASE_WAIT = 60


_GEMINI_COMPAT_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
_GEMINI_NATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta"


class LLMClient:
    """Thin LLM client supporting OpenAI-compatible and native Gemini endpoints.

    For Gemini keys, starts on the OpenAI-compat layer. On a 403 (which
    happens with preview/experimental models not exposed via compat), it
    automatically switches to the native generateContent API and stays there
    for the lifetime of the process.
    """

    def __init__(self, base_url: str, model: str, api_key: str) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self._client = httpx.Client(timeout=_TIMEOUT)
        self._last_request_at = 0.0
        self._request_interval = 15.0
        # True once we've confirmed the native Gemini API works for this model
        self._use_native_gemini: bool = False
        self._is_gemini: bool = base_url.startswith(_GEMINI_COMPAT_BASE)
        # Set by get_client() when a fallback provider exists: give up on a
        # rate limit after one short retry instead of waiting up to ~12 minutes.
        self.fail_fast_on_rate_limit: bool = False

    # -- Native Gemini API --------------------------------------------------

    def _chat_native_gemini(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call the native Gemini generateContent API.

        Used automatically when the OpenAI-compat endpoint returns 403,
        which happens for preview/experimental models not exposed via compat.

        Converts OpenAI-style messages to Gemini's contents/systemInstruction
        format transparently.
        """
        contents: list[dict] = []
        system_parts: list[dict] = []

        for msg in messages:
            role = msg["role"]
            text = msg.get("content", "")
            if role == "system":
                system_parts.append({"text": text})
            elif role == "user":
                contents.append({"role": "user", "parts": [{"text": text}]})
            elif role == "assistant":
                # Gemini uses "model" instead of "assistant"
                contents.append({"role": "model", "parts": [{"text": text}]})

        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}

        url = f"{_GEMINI_NATIVE_BASE}/models/{self.model}:generateContent"
        resp = self._client.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            params={"key": self.api_key},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

    # -- OpenAI-compat API --------------------------------------------------

    def _chat_compat(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call the OpenAI-compatible endpoint."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
        )

        # 403 on Gemini compat = model not available on compat layer.
        # Raise a specific sentinel so chat() can switch to native API.
        if resp.status_code == 403 and self._is_gemini:
            raise _GeminiCompatForbidden(resp)

        return self._handle_compat_response(resp)

    @staticmethod
    def _handle_compat_response(resp: httpx.Response) -> str:
        resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]
        content = choice["message"].get("content") or ""
        if choice.get("finish_reason") == "length":
            log.warning("LLM answer was cut off at the output-token limit (%d chars returned).", len(content))
        return content

    # -- public API ---------------------------------------------------------

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        """Send a chat completion request and return the assistant message text."""
        # Gemini 2.5+ models think before answering, and on the OpenAI-compatible
        # endpoint that thinking counts toward max_tokens; small limits (e.g. 2048
        # for a tailored resume) can cut the answer off. Give them room.
        if self._is_gemini:
            max_tokens = max(max_tokens, _GEMINI_MIN_MAX_TOKENS)
        # Qwen3 optimization: prepend /no_think to skip chain-of-thought
        # reasoning, saving tokens on structured extraction tasks.
        if "qwen" in self.model.lower() and messages:
            first = messages[0]
            if first.get("role") == "user" and not first["content"].startswith("/no_think"):
                messages = [{"role": first["role"], "content": f"/no_think\n{first['content']}"}] + messages[1:]

        for attempt in range(_MAX_RETRIES):
            elapsed = time.monotonic() - self._last_request_at
            remaining = self._request_interval - elapsed
            if remaining > 0:
                time.sleep(remaining)
            self._last_request_at = time.monotonic()

            try:
                # Route to native Gemini if we've already confirmed it's needed
                if self._use_native_gemini:
                    return self._chat_native_gemini(messages, temperature, max_tokens)

                return self._chat_compat(messages, temperature, max_tokens)

            except _GeminiCompatForbidden as exc:
                # Model not available on OpenAI-compat layer — switch to native.
                log.warning(
                    "Gemini compat endpoint returned 403 for model '%s'. "
                    "Switching to native generateContent API. "
                    "(Preview/experimental models are often compat-only on native.)",
                    self.model,
                )
                self._use_native_gemini = True
                # Retry immediately with native — don't count as a rate-limit wait
                try:
                    return self._chat_native_gemini(messages, temperature, max_tokens)
                except httpx.HTTPStatusError as native_exc:
                    raise RuntimeError(
                        f"Both Gemini endpoints failed. Compat: 403 Forbidden. "
                        f"Native: {native_exc.response.status_code} — "
                        f"{native_exc.response.text[:200]}"
                    ) from native_exc

            except httpx.HTTPStatusError as exc:
                resp = exc.response

                if (
                    self._is_gemini
                    and resp.status_code == 429
                    and _is_hard_gemini_quota_error(resp)
                ):
                    raise GeminiQuotaExhausted(
                        f"Gemini hard quota/billing limit reached "
                        f"(HTTP 429): {resp.text[:1000]}"
                    ) from exc

                if resp.status_code in (429, 503) and self.fail_fast_on_rate_limit and attempt >= 1:
                    raise GeminiRateLimited(
                        f"Still rate limited (HTTP {resp.status_code}) after a retry: {resp.text[:300]}"
                    ) from exc

                if resp.status_code in (429, 503) and attempt < _MAX_RETRIES - 1:
                    # Respect Retry-After header if provided (Gemini sends this).
                    retry_after = (
                        resp.headers.get("Retry-After")
                        or resp.headers.get("X-RateLimit-Reset-Requests")
                    )
                    if retry_after:
                        try:
                            wait = float(retry_after)
                        except (ValueError, TypeError):
                            wait = _RATE_LIMIT_BASE_WAIT * (2 ** attempt)
                    else:
                        wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 300)
                    if self.fail_fast_on_rate_limit:
                        wait = min(wait, 60)

                    log.warning(
                        "LLM rate limited (HTTP %s). Waiting %ds before retry %d/%d. "
                        "Tip: Gemini free tier = 15 RPM. Consider a paid account "
                        "or switching to a local model.",
                        resp.status_code, wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

            except httpx.TimeoutException:
                if attempt < _MAX_RETRIES - 1:
                    wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 300)
                    log.warning(
                        "LLM request timed out, retrying in %ds (attempt %d/%d)",
                        wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

        raise RuntimeError("LLM request failed after all retries")

    def ask(self, prompt: str, **kwargs) -> str:
        """Convenience: single user prompt -> assistant response."""
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        self._client.close()


class _GeminiCompatForbidden(Exception):
    """Sentinel: Gemini OpenAI-compat returned 403. Switch to native API."""
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"Gemini compat 403: {response.text[:200]}")


# ---------------------------------------------------------------------------
# Anthropic (Claude API) client
# ---------------------------------------------------------------------------

# Models that accept server-side refusal fallbacks (fallbacks="default").
_SERVER_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
# Current Claude models think before answering, and thinking counts toward
# max_tokens, so callers' small limits (e.g. 512 for scoring) would truncate.
_CLAUDE_MIN_MAX_TOKENS = 16000


class AnthropicClient:
    """Claude via the official Anthropic SDK (ANTHROPIC_API_KEY).

    Model: ANTHROPIC_MODEL (default claude-haiku-5-5). Optional ANTHROPIC_EFFORT
    (low | medium | high | xhigh | max) trades depth for cost.
    """

    def __init__(self, model: str = "", api_key: str = "") -> None:
        import anthropic  # imported lazily so Gemini-only setups don't need it

        self._anthropic = anthropic
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "").strip() or "claude-haiku-5-5"
        self.effort = os.environ.get("ANTHROPIC_EFFORT", "").strip() or None
        self._client = anthropic.Anthropic(api_key=api_key or None, max_retries=4)

    def chat(self, messages: list[dict], temperature: float = 0.0, max_tokens: int = 4096) -> str:
        # `temperature` is accepted for interface compatibility but not sent:
        # current Claude models reject sampling parameters.
        anthropic = self._anthropic
        system = "\n\n".join(m.get("content", "") for m in messages if m.get("role") == "system")
        convo = [{"role": m["role"], "content": m.get("content", "")}
                 for m in messages if m.get("role") in ("user", "assistant")]
        kwargs: dict = {
            "model": self.model,
            "max_tokens": max(max_tokens, _CLAUDE_MIN_MAX_TOKENS),
            "messages": convo,
        }
        if system:
            kwargs["system"] = system
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}

        try:
            if self.model in _SERVER_FALLBACK_MODELS:
                # On a safety-classifier refusal, re-run server-side on Anthropic's
                # recommended fallback model instead of returning the refusal.
                resp = self._client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            else:
                resp = self._client.messages.create(**kwargs)
        except anthropic.AuthenticationError as exc:
            raise RuntimeError(f"ANTHROPIC_API_KEY was rejected: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            msg = str(getattr(exc, "message", exc)).lower()
            if exc.status_code == 402 or "credit balance" in msg or "spend limit" in msg:
                raise ClaudeCreditExhausted(f"Claude API credit/spend limit reached: {exc.message}") from exc
            raise

        if resp.stop_reason == "refusal":
            raise RuntimeError(f"Claude declined the request ({getattr(resp, 'stop_details', None)})")
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        if not text:
            raise RuntimeError(f"Claude returned no text (stop_reason={resp.stop_reason})")
        return text

    def ask(self, prompt: str, **kwargs) -> str:
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        self._client.close()


class FallbackClient:
    """Use `primary` (e.g. Gemini free tier); switch to `fallback` (Claude) when it's limited.

    - Hard quota exhausted (daily/monthly): switch for the rest of the run.
    - Rate limited even after a retry, or timing out: use the fallback for that request only.
    """

    def __init__(self, primary, fallback, primary_name: str, fallback_name: str) -> None:
        self.primary = primary
        self.fallback = fallback
        self.primary_name = primary_name
        self.fallback_name = fallback_name
        self._primary_exhausted = False
        self.model = getattr(primary, "model", primary_name)

    def chat(self, messages: list[dict], temperature: float = 0.0, max_tokens: int = 4096) -> str:
        if self._primary_exhausted:
            return self.fallback.chat(messages, temperature=temperature, max_tokens=max_tokens)
        try:
            return self.primary.chat(messages, temperature=temperature, max_tokens=max_tokens)
        except LLMQuotaExhausted as exc:
            self._primary_exhausted = True
            log.warning("%s quota exhausted; using %s for the rest of this run. (%s)",
                        self.primary_name, self.fallback_name, str(exc)[:200])
        except (GeminiRateLimited, httpx.TimeoutException) as exc:
            log.warning("%s rate limited/slow; using %s for this request. (%s)",
                        self.primary_name, self.fallback_name, str(exc)[:200])
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                log.warning("%s returned 404 (model %r not found? set LLM_MODEL); using %s.",
                            self.primary_name, self.model, self.fallback_name)
                return self.fallback.chat(messages, temperature=temperature, max_tokens=max_tokens)
            if exc.response.status_code not in (429, 503):
                raise
            log.warning("%s returned HTTP %s; using %s for this request.",
                        self.primary_name, exc.response.status_code, self.fallback_name)
        return self.fallback.chat(messages, temperature=temperature, max_tokens=max_tokens)

    def ask(self, prompt: str, **kwargs) -> str:
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        for c in (self.primary, self.fallback):
            try:
                c.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance = None  # LLMClient | AnthropicClient | FallbackClient | ...


_model_clients: dict = {}


def get_client_for(model_env: str):
    """Client for a specific stage, e.g. get_client_for("LLM_SCORE_MODEL").

    Uses the model named in that env var (kie.ai provider only); falls back to the default client.
    """
    model = os.environ.get(model_env, "").strip()
    if not model or os.environ.get("LLM_PROVIDER", "").strip().lower() != "kie":
        return get_client()
    if model not in _model_clients:
        key = os.environ.get("KIE_API_KEY", "")
        if not key:
            raise RuntimeError("KIE_API_KEY is not set in ~/.applypilot/.env")
        log.info("LLM provider: kie.ai  model: %s  (%s)", model, model_env)
        _model_clients[model] = KieClaudeClient(model, key, os.environ.get("KIE_URL", ""))
    return _model_clients[model]


def get_client():
    """Return the configured module-level LLM client singleton."""
    global _instance
    if _instance is None:
        provider = os.environ.get("LLM_PROVIDER", "").strip().lower()
        if provider == "claude_cli":
            model = os.environ.get("CLAUDE_MODEL", "sonnet")
            log.info("LLM provider: Claude Code CLI  model: %s", model)
            _instance = ClaudeCLIClient(model)
            if not claude_cli_path():
                log.warning("LLM_PROVIDER=claude_cli but the `claude` command wasn't found. "
                            "Install Claude Code and log in, or set CLAUDE_BIN.")
        elif provider == "kie":
            key = os.environ.get("KIE_API_KEY", "")
            if not key:
                raise RuntimeError("LLM_PROVIDER=kie but KIE_API_KEY is not set in ~/.applypilot/.env")
            model = os.environ.get("LLM_MODEL", "").strip() or "claude-haiku-5-5"
            log.info("LLM provider: kie.ai  model: %s", model)
            _instance = KieClaudeClient(model, key, os.environ.get("KIE_URL", ""))
        elif provider in ("anthropic", "claude"):
            _instance = AnthropicClient()
            log.info("LLM provider: Claude API  model: %s", _instance.model)
        elif not any(os.environ.get(k) for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "LLM_URL")) \
                and os.environ.get("ANTHROPIC_API_KEY"):
            _instance = AnthropicClient()
            log.info("LLM provider: Claude API  model: %s", _instance.model)
        else:
            base_url, model, api_key = _detect_provider()
            log.info("LLM provider: %s  model: %s", base_url, model)
            primary = LLMClient(base_url, model, api_key)
            _instance = primary
            # Claude API as automatic fallback (e.g. Gemini free tier first, then Claude).
            fb = os.environ.get("LLM_FALLBACK_PROVIDER", "").strip().lower()
            name = "Gemini" if primary._is_gemini else base_url
            fallback = fallback_name = None
            if fb in ("claude_cli", "claude-cli", "claude_code", "subscription"):
                # Claude Pro/Max subscription through the logged-in Claude Code CLI.
                if claude_cli_path():
                    fallback = ClaudeCLIClient(os.environ.get("CLAUDE_MODEL", "").strip() or "haiku")
                    fallback_name = f"Claude Code CLI ({fallback.model})"
                else:
                    log.warning("LLM_FALLBACK_PROVIDER=claude_cli but the `claude` command wasn't "
                                "found; running without a fallback. Install Claude Code and log in.")
            elif os.environ.get("ANTHROPIC_API_KEY") and fb not in ("none", "off", "false"):
                fallback = AnthropicClient()
                fallback_name = f"Claude API ({fallback.model})"
            if fallback is not None:
                primary.fail_fast_on_rate_limit = True
                log.info("LLM fallback: %s (used when %s is limited)", fallback_name, name)
                _instance = FallbackClient(primary, fallback, name, fallback_name)
    return _instance
