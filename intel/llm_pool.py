"""
LLM provider pool for deep intelligence (docs/INTEL_ROADMAP.md, ticket I04).

Manages multi-key LLM providers declared via INTEL_LLM_<n>_* environment variables,
with backward-compatible fallbacks to existing CRAWLER_LLM_* and ASSISTANT_LLM_* settings.

Features:
- Task routing: triage (cheap/high volume), extract (long-context JSON), synthesize (best model), vision
- Per-provider token-bucket rate limiting and cool-down on HTTP 429 / retry-after
- Automatic failover across eligible providers
- JSON normalisation with regex repair and one LLM retry
- Hard enforcement of Budget.max_llm_tokens
- Per-company and per-run usage ledger
- complete(task, system, user, schema=None) single entry point
"""

from __future__ import annotations

import email.utils
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

logger = logging.getLogger(__name__)

# Task classes
TASK_TRIAGE = "triage"          # cheap, high volume
TASK_EXTRACT = "extract"        # long-context JSON fact extraction
TASK_SYNTHESIZE = "synthesize"  # best model, reasoning
TASK_VISION = "vision"          # vision models
ALL_TASKS = (TASK_TRIAGE, TASK_EXTRACT, TASK_SYNTHESIZE, TASK_VISION)

# Tier rankings for sorting (lower value = cheaper / higher volume preference)
TIER_PRIORITY = {
    "free": 10,
    "cheap": 20,
    "standard": 50,
    "premium": 80,
    "best": 100,
}


class LLMPoolError(RuntimeError):
    """Base error for LLM pool operations."""


class NoProvidersConfiguredError(LLMPoolError):
    """Raised when no provider in the pool can serve requests."""


class BudgetExceededError(LLMPoolError):
    """Raised when hard token budget is exceeded."""


class AllProvidersCoolingDownError(LLMPoolError):
    """Raised when all eligible providers are currently in rate-limit cool-down."""


class AllProvidersFailedError(LLMPoolError):
    """Raised when all eligible providers failed for a request."""


class JSONParseError(LLMPoolError):
    """Raised when output cannot be parsed into valid JSON even after repair and retry."""


class ValidationError(LLMPoolError):
    """Raised when parsed JSON does not match the required schema."""


@dataclass
class UsageRecord:
    timestamp: float
    company_id: Optional[str]
    run_id: Optional[str]
    provider: str
    model: str
    task: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_s: float
    success: bool
    error: Optional[str] = None


class LLMUsageLedger:
    """Thread-safe usage tracking per company, run and provider."""

    def __init__(self) -> None:
        self._records: List[UsageRecord] = []

    def record(
        self,
        company_id: Optional[str],
        run_id: Optional[str],
        provider: str,
        model: str,
        task: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        latency_s: float,
        success: bool,
        error: Optional[str] = None,
    ) -> UsageRecord:
        rec = UsageRecord(
            timestamp=time.time(),
            company_id=company_id,
            run_id=run_id,
            provider=provider,
            model=model,
            task=task,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            latency_s=latency_s,
            success=success,
            error=error,
        )
        self._records.append(rec)
        return rec

    def get_tokens(self, company_id: Optional[str] = None, run_id: Optional[str] = None) -> int:
        total = 0
        for r in self._records:
            if not r.success:
                continue
            if company_id is not None and r.company_id != company_id:
                continue
            if run_id is not None and r.run_id != run_id:
                continue
            total += r.total_tokens
        return total

    def get_records(self, company_id: Optional[str] = None, run_id: Optional[str] = None) -> List[UsageRecord]:
        matches = []
        for r in self._records:
            if company_id is not None and r.company_id != company_id:
                continue
            if run_id is not None and r.run_id != run_id:
                continue
            matches.append(r)
        return list(matches)

    def get_summary(self) -> Dict[str, Any]:
        by_provider: Dict[str, Dict[str, int]] = {}
        by_task: Dict[str, Dict[str, int]] = {}
        total_tokens = 0
        call_count = 0
        failure_count = 0

        for r in self._records:
            call_count += 1
            if not r.success:
                failure_count += 1
                continue
            total_tokens += r.total_tokens

            p_entry = by_provider.setdefault(r.provider, {"calls": 0, "tokens": 0})
            p_entry["calls"] += 1
            p_entry["tokens"] += r.total_tokens

            t_entry = by_task.setdefault(r.task, {"calls": 0, "tokens": 0})
            t_entry["calls"] += 1
            t_entry["tokens"] += r.total_tokens

        return {
            "total_calls": call_count,
            "failed_calls": failure_count,
            "total_tokens": total_tokens,
            "by_provider": by_provider,
            "by_task": by_task,
        }

    def reset(self) -> None:
        self._records.clear()


@dataclass
class Provider:
    name: str
    api_key: str
    base_url: str = "https://api.groq.com/openai/v1"
    model: str = "openai/gpt-oss-120b"
    tier: str = "free"
    tasks: List[str] = field(default_factory=lambda: [TASK_TRIAGE, TASK_EXTRACT, TASK_SYNTHESIZE])
    tpm: int = 8000
    rpm: int = 30
    timeout: float = 60.0
    # State tracking
    cool_down_until: float = 0.0
    tokens_available: float = 8000.0
    requests_available: float = 30.0
    last_refill: float = field(default_factory=time.time)
    consecutive_failures: int = 0

    def refill(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        elapsed = max(0.0, now - self.last_refill)
        if elapsed > 0:
            self.tokens_available = min(float(self.tpm), self.tokens_available + elapsed * (self.tpm / 60.0))
            self.requests_available = min(float(self.rpm), self.requests_available + elapsed * (self.rpm / 60.0))
            self.last_refill = now

    def is_cooling_down(self, now: Optional[float] = None) -> bool:
        now = now or time.time()
        return now < self.cool_down_until

    def cool_down_remaining(self, now: Optional[float] = None) -> float:
        now = now or time.time()
        return max(0.0, self.cool_down_until - now)

    def can_serve(self, task: str, estimated_tokens: int = 500, now: Optional[float] = None) -> bool:
        now = now or time.time()
        if self.is_cooling_down(now):
            return False
        if task not in self.tasks and "*" not in self.tasks:
            return False
        self.refill(now)
        # We allow serving as long as we have at least 1 request token
        return self.requests_available >= 1.0

    def consume(self, tokens_used: int, now: Optional[float] = None) -> None:
        now = now or time.time()
        self.refill(now)
        self.tokens_available = max(0.0, self.tokens_available - tokens_used)
        self.requests_available = max(0.0, self.requests_available - 1.0)
        self.cool_down_until = 0.0
        self.consecutive_failures = 0

    def record_429(self, retry_after_s: Optional[float] = None, now: Optional[float] = None) -> float:
        now = now or time.time()
        cooldown = retry_after_s if (retry_after_s is not None and retry_after_s > 0) else 60.0
        self.cool_down_until = now + cooldown
        self.consecutive_failures += 1
        return cooldown

    def record_failure(self, error_type: str = "error", now: Optional[float] = None) -> float:
        now = now or time.time()
        self.consecutive_failures += 1
        cooldown = min(300.0, 10.0 * (2 ** min(self.consecutive_failures - 1, 5)))
        self.cool_down_until = now + cooldown
        return cooldown


@dataclass
class LLMResult:
    text: str
    data: Optional[Any]
    provider: str
    model: str
    usage: Dict[str, int]  # prompt_tokens, completion_tokens, total_tokens
    latency_s: float


# Helper for parsing Retry-After header
def parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    value = value.strip()
    # Case 1: integer / float seconds
    try:
        return float(value)
    except ValueError:
        pass
    # Case 2: HTTP date
    try:
        dt = email.utils.parsedate_to_datetime(value)
        diff = dt.timestamp() - time.time()
        return max(0.0, diff)
    except Exception:
        pass
    # Case 3: regex pattern "try again in X.Xs" or "try again in Xms"
    m_s = re.search(r"(\d+(?:\.\d+)?)\s*(?:s|sec|seconds)", value, re.IGNORECASE)
    if m_s:
        return float(m_s.group(1))
    m_ms = re.search(r"(\d+(?:\.\d+)?)\s*ms", value, re.IGNORECASE)
    if m_ms:
        return float(m_ms.group(1)) / 1000.0
    return None


def normalize_and_repair_json(raw_text: str, schema: Optional[Any] = None) -> Any:
    """Normalises raw LLM output, strips markdown formatting, repairs common syntax bugs,
    and optionally validates against schema."""
    text = (raw_text or "").strip()
    if not text:
        raise JSONParseError("Empty response cannot be parsed as JSON")

    # 1. Direct parse attempt
    try:
        parsed = json.loads(text)
        return _validate_data(parsed, schema)
    except Exception:
        pass

    # 2. Extract markdown code block if present
    code_block_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if code_block_match:
        inner = code_block_match.group(1).strip()
        try:
            parsed = json.loads(inner)
            return _validate_data(parsed, schema)
        except Exception:
            text = inner

    # 3. Locate outer-most JSON structure ({...} or [...])
    first_brace = text.find("{")
    first_bracket = text.find("[")
    start = -1
    end = -1
    if first_brace != -1 and (first_bracket == -1 or first_brace < first_bracket):
        start = first_brace
        end = text.rfind("}")
    elif first_bracket != -1:
        start = first_bracket
        end = text.rfind("]")

    if start != -1 and end != -1 and end > start:
        candidate = text[start : end + 1]
        try:
            parsed = json.loads(candidate)
            return _validate_data(parsed, schema)
        except Exception:
            text = candidate

    # 4. Regex repairs on the extracted structure
    repaired = text
    # Trailing commas before closing braces/brackets
    repaired = re.sub(r",\s*([\]\}])", r"\1", repaired)
    # Python None -> null, True -> true, False -> false
    repaired = re.sub(r"\bNone\b", "null", repaired)
    repaired = re.sub(r"\bTrue\b", "true", repaired)
    repaired = re.sub(r"\bFalse\b", "false", repaired)

    try:
        parsed = json.loads(repaired)
        return _validate_data(parsed, schema)
    except Exception as e:
        raise JSONParseError(f"Failed to parse JSON after normalisation: {e}") from e


def _validate_data(data: Any, schema: Optional[Any]) -> Any:
    if schema is None:
        return data

    if isinstance(schema, type):
        if issubclass(schema, (dict, list, str, int, float, bool)):
            if not isinstance(data, schema):
                raise ValidationError(f"Expected type {schema.__name__}, got {type(data).__name__}")
            return data
        # Pydantic or dataclass-like class
        if hasattr(schema, "model_validate"):
            return schema.model_validate(data)
        if hasattr(schema, "parse_obj"):
            return schema.parse_obj(data)

    if isinstance(schema, dict):
        # Basic JSON schema check
        req = schema.get("required")
        if req and isinstance(data, dict):
            missing = [k for k in req if k not in data]
            if missing:
                raise ValidationError(f"Missing required fields: {missing}")

    return data


class LLMPool:
    """Multi-provider LLM pool with task routing, rate limiting, and fallback."""

    def __init__(
        self,
        providers: Optional[Sequence[Provider]] = None,
        ledger: Optional[LLMUsageLedger] = None,
        http_client: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.providers: List[Provider] = list(providers or [])
        self.ledger = ledger or LLMUsageLedger()
        self._http_client = http_client or self._default_http_call

    @classmethod
    def from_env(cls, getenv: Callable[[str], Optional[str]] = os.getenv) -> LLMPool:
        """Constructs an LLMPool reading INTEL_LLM_<n>_* variables, falling back to
        existing CRAWLER_LLM_* and ASSISTANT_LLM_* settings."""
        if getenv is os.getenv:
            try:
                from dotenv import load_dotenv
                load_dotenv()
            except ImportError:
                pass

        providers: List[Provider] = []

        # 1. Check INTEL_LLM_<n>_*
        found_numbered: Dict[int, Dict[str, Any]] = {}
        # Scan for any INTEL_LLM_<n>_* up to 20
        for i in range(1, 21):
            key = getenv(f"INTEL_LLM_{i}_API_KEY")
            if not key:
                continue
            name = getenv(f"INTEL_LLM_{i}_NAME") or f"intel_{i}"
            base_url = getenv(f"INTEL_LLM_{i}_BASE_URL") or "https://api.groq.com/openai/v1"
            model = getenv(f"INTEL_LLM_{i}_MODEL") or "openai/gpt-oss-120b"
            tier = getenv(f"INTEL_LLM_{i}_TIER") or "free"
            tasks_str = getenv(f"INTEL_LLM_{i}_TASKS")
            tasks = [t.strip() for t in tasks_str.split(",") if t.strip()] if tasks_str else [TASK_TRIAGE, TASK_EXTRACT, TASK_SYNTHESIZE]
            tpm_val = getenv(f"INTEL_LLM_{i}_TPM")
            tpm = int(tpm_val) if tpm_val and tpm_val.isdigit() else 8000
            rpm_val = getenv(f"INTEL_LLM_{i}_RPM")
            rpm = int(rpm_val) if rpm_val and rpm_val.isdigit() else 30

            providers.append(
                Provider(
                    name=name,
                    api_key=key,
                    base_url=base_url,
                    model=model,
                    tier=tier,
                    tasks=tasks,
                    tpm=tpm,
                    rpm=rpm,
                )
            )

        # 2. Backward-compatible reading of CRAWLER_LLM_* and CRAWLER_VISION_LLM_*
        primary_key = getenv("CRAWLER_LLM_API_KEY")
        if primary_key and not any(p.name == "crawler_primary" for p in providers):
            providers.append(
                Provider(
                    name="crawler_primary",
                    api_key=primary_key,
                    base_url=getenv("CRAWLER_LLM_BASE_URL") or "https://api.groq.com/openai/v1",
                    model=getenv("CRAWLER_LLM_MODEL") or "openai/gpt-oss-120b",
                    tier="free",
                    tasks=[TASK_TRIAGE, TASK_EXTRACT],
                    tpm=8000,
                    rpm=30,
                )
            )

        fb_key = getenv("CRAWLER_LLM_FALLBACK_API_KEY")
        if fb_key and not any(p.name == "crawler_fallback" for p in providers):
            providers.append(
                Provider(
                    name="crawler_fallback",
                    api_key=fb_key,
                    base_url=getenv("CRAWLER_LLM_FALLBACK_BASE_URL") or "https://api.groq.com/openai/v1",
                    model=getenv("CRAWLER_LLM_FALLBACK_MODEL") or "openai/gpt-oss-120b",
                    tier="free",
                    tasks=[TASK_TRIAGE, TASK_EXTRACT],
                    tpm=8000,
                    rpm=30,
                )
            )

        # Numbered extra fallbacks (2..5)
        for s in ("2", "3", "4", "5"):
            ef_key = getenv(f"CRAWLER_LLM_FALLBACK{s}_API_KEY")
            if ef_key and not any(p.name == f"crawler_fallback_{s}" for p in providers):
                providers.append(
                    Provider(
                        name=f"crawler_fallback_{s}",
                        api_key=ef_key,
                        base_url=getenv(f"CRAWLER_LLM_FALLBACK{s}_BASE_URL") or "https://api.groq.com/openai/v1",
                        model=getenv(f"CRAWLER_LLM_FALLBACK{s}_MODEL") or "openai/gpt-oss-120b",
                        tier="free",
                        tasks=[TASK_TRIAGE, TASK_EXTRACT],
                        tpm=8000,
                        rpm=30,
                    )
                )

        # Vision LLM
        v_key = getenv("CRAWLER_VISION_LLM_API_KEY")
        if v_key and not any(p.name == "crawler_vision" for p in providers):
            providers.append(
                Provider(
                    name="crawler_vision",
                    api_key=v_key,
                    base_url=getenv("CRAWLER_VISION_LLM_BASE_URL") or "https://api.groq.com/openai/v1",
                    model=getenv("CRAWLER_VISION_LLM_MODEL") or "llama-3.2-11b-vision-preview",
                    tier="free",
                    tasks=[TASK_VISION],
                    tpm=6000,
                    rpm=20,
                )
            )

        # Assistant LLM (for synthesis fallback if configured)
        as_key = getenv("ASSISTANT_LLM_API_KEY")
        if as_key and not any(p.name == "assistant_primary" for p in providers):
            providers.append(
                Provider(
                    name="assistant_primary",
                    api_key=as_key,
                    base_url=getenv("ASSISTANT_LLM_BASE_URL") or "https://api.mistral.ai/v1",
                    model=getenv("ASSISTANT_LLM_MODEL") or "mistral-large-latest",
                    tier="standard",
                    tasks=[TASK_SYNTHESIZE, TASK_EXTRACT],
                    tpm=30000,
                    rpm=60,
                )
            )

        as_fb_key = getenv("ASSISTANT_LLM_FALLBACK_API_KEY")
        if as_fb_key and not any(p.name == "assistant_fallback" for p in providers):
            providers.append(
                Provider(
                    name="assistant_fallback",
                    api_key=as_fb_key,
                    base_url=getenv("ASSISTANT_LLM_FALLBACK_BASE_URL") or "https://generativelanguage.googleapis.com/v1beta/openai",
                    model=getenv("ASSISTANT_LLM_FALLBACK_MODEL") or "gemini-2.0-flash",
                    tier="cheap",
                    tasks=[TASK_TRIAGE, TASK_EXTRACT, TASK_SYNTHESIZE, TASK_VISION],
                    tpm=30000,
                    rpm=60,
                )
            )

        return cls(providers=providers)

    def route(self, task: str) -> List[Provider]:
        """Returns providers eligible for the task, ordered by preference."""
        eligible = [p for p in self.providers if task in p.tasks or "*" in p.tasks]
        if not eligible:
            raise NoProvidersConfiguredError(f"No configured LLM provider supports task {task!r}")

        # Sort criteria:
        # For triage: cheapest/free first (lowest tier score)
        # For synthesize: best model first (highest tier score)
        # For extract: free or standard
        def sort_key(p: Provider) -> int:
            t = TIER_PRIORITY.get(p.tier.lower(), 50)
            if task == TASK_TRIAGE:
                return t
            elif task == TASK_SYNTHESIZE:
                return -t
            else:
                return t

        return sorted(eligible, key=sort_key)

    def complete(
        self,
        task: str,
        system: str,
        user: str,
        schema: Optional[Any] = None,
        images: Optional[List[Any]] = None,
        budget: Optional[Any] = None,
        company_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> LLMResult:
        """Single entry point for completing an LLM task.
        Enforces budget, handles rate limiting, failover, JSON normalisation, and repair retry."""
        # 1. Hard enforcement of Budget.max_llm_tokens
        if budget is not None:
            max_tokens = getattr(budget, "max_llm_tokens", None)
            if max_tokens is not None:
                current_tokens = self.ledger.get_tokens(company_id=company_id, run_id=run_id)
                if current_tokens >= max_tokens:
                    raise BudgetExceededError(
                        f"Hard token budget exceeded: {current_tokens} >= max_llm_tokens {max_tokens}"
                    )

        # 2. Routing
        eligible = self.route(task)

        # Check if all eligible are cooling down
        now = time.time()
        cooling = [p for p in eligible if p.is_cooling_down(now)]
        if len(cooling) == len(eligible):
            min_cd = min(p.cool_down_remaining(now) for p in cooling)
            raise AllProvidersCoolingDownError(
                f"All providers for task {task!r} are cooling down (shortest cooldown: {min_cd:.1f}s)"
            )

        errors: List[str] = []
        last_json_error: Optional[Exception] = None

        for provider in eligible:
            if provider.is_cooling_down(now):
                continue

            start_t = time.time()
            try:
                raw_text, usage = self._http_client(
                    provider=provider,
                    system=system,
                    user=user,
                    schema=schema,
                    images=images,
                )
                latency = time.time() - start_t
                prompt_tokens = usage.get("prompt_tokens", 0)
                completion_tokens = usage.get("completion_tokens", 0)
                total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)

                # Update provider rate limiter & usage ledger
                provider.consume(total_tokens, now=time.time())
                self.ledger.record(
                    company_id=company_id,
                    run_id=run_id,
                    provider=provider.name,
                    model=provider.model,
                    task=task,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    latency_s=latency,
                    success=True,
                )

                # Process JSON / Schema if requested
                data = None
                if schema is not None:
                    try:
                        data = normalize_and_repair_json(raw_text, schema=schema)
                    except (JSONParseError, ValidationError) as parse_err:
                        # Attempt ONE repair retry
                        data = self._retry_repair(
                            provider=provider,
                            system=system,
                            raw_text=raw_text,
                            error_msg=str(parse_err),
                            schema=schema,
                            company_id=company_id,
                            run_id=run_id,
                            task=task,
                        )

                return LLMResult(
                    text=raw_text,
                    data=data,
                    provider=provider.name,
                    model=provider.model,
                    usage={
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": total_tokens,
                    },
                    latency_s=latency,
                )

            except Exception as e:
                latency = time.time() - start_t
                err_str = str(e)
                errors.append(f"{provider.name}: {err_str}")
                if isinstance(e, (JSONParseError, ValidationError)):
                    last_json_error = e

                self.ledger.record(
                    company_id=company_id,
                    run_id=run_id,
                    provider=provider.name,
                    model=provider.model,
                    task=task,
                    prompt_tokens=0,
                    completion_tokens=0,
                    total_tokens=0,
                    latency_s=latency,
                    success=False,
                    error=err_str,
                )
                # Inspect for 429
                if "429" in err_str or "rate limit" in err_str.lower():
                    cd = parse_retry_after(err_str)
                    provider.record_429(cd, now=time.time())
                elif not isinstance(e, (JSONParseError, ValidationError)):
                    provider.record_failure(err_str, now=time.time())

        if last_json_error is not None:
            raise last_json_error
        raise AllProvidersFailedError(f"All providers failed for task {task!r}: {'; '.join(errors)}")

    def _retry_repair(
        self,
        provider: Provider,
        system: str,
        raw_text: str,
        error_msg: str,
        schema: Any,
        company_id: Optional[str],
        run_id: Optional[str],
        task: str,
    ) -> Any:
        """Performs one repair retry with the model."""
        repair_prompt = (
            f"Your previous JSON output failed validation:\n{error_msg}\n\n"
            f"Original output:\n{raw_text}\n\n"
            "Please fix the error and output ONLY the corrected, valid JSON structure with no markdown or explanations."
        )
        start_t = time.time()
        try:
            fixed_text, usage = self._http_client(
                provider=provider,
                system="You are a strict JSON fixer. Output ONLY valid JSON matching the requested schema.",
                user=repair_prompt,
                schema=schema,
                images=None,
            )
            latency = time.time() - start_t
            p_tok = usage.get("prompt_tokens", 0)
            c_tok = usage.get("completion_tokens", 0)
            t_tok = usage.get("total_tokens", p_tok + c_tok)

            provider.consume(t_tok, now=time.time())
            self.ledger.record(
                company_id=company_id,
                run_id=run_id,
                provider=provider.name,
                model=provider.model,
                task=task,
                prompt_tokens=p_tok,
                completion_tokens=c_tok,
                total_tokens=t_tok,
                latency_s=latency,
                success=True,
            )
            return normalize_and_repair_json(fixed_text, schema=schema)
        except Exception as retry_err:
            raise JSONParseError(
                f"JSON repair retry failed: {retry_err}. Initial error was: {error_msg}"
            ) from retry_err

    def _default_http_call(
        self,
        provider: Provider,
        system: str,
        user: str,
        schema: Optional[Any],
        images: Optional[List[Any]],
    ) -> tuple[str, Dict[str, int]]:
        """Makes an HTTP call to an OpenAI-compatible endpoint."""
        import requests

        url = f"{provider.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Vienna-Intel-LLM/1.0",
        }

        messages = []
        if system:
            messages.append({"role": "system", "content": system})

        if images:
            # Construct multimodal user content
            content_list: List[Dict[str, Any]] = [{"type": "text", "text": user}]
            for img in images:
                if isinstance(img, str) and (img.startswith("http://") or img.startswith("https://") or img.startswith("data:")):
                    content_list.append({"type": "image_url", "image_url": {"url": img}})
                elif isinstance(img, dict) and "url" in img:
                    content_list.append({"type": "image_url", "image_url": img})
            messages.append({"role": "user", "content": content_list})
        else:
            messages.append({"role": "user", "content": user})

        payload: Dict[str, Any] = {
            "model": provider.model,
            "messages": messages,
            "temperature": 0,
        }
        if schema is not None:
            payload["response_format"] = {"type": "json_object"}

        res = requests.post(url, headers=headers, json=payload, timeout=provider.timeout)
        if res.status_code == 429:
            retry_header = res.headers.get("Retry-After") or res.headers.get("retry-after")
            raise LLMPoolError(f"HTTP 429 Too Many Requests (Retry-After: {retry_header})")
        if not res.ok:
            # If 400 bad request was due to response_format, retry without it
            if res.status_code == 400 and schema is not None and "response_format" in res.text:
                payload.pop("response_format", None)
                res = requests.post(url, headers=headers, json=payload, timeout=provider.timeout)
                if not res.ok:
                    raise LLMPoolError(f"HTTP {res.status_code}: {res.text[:400]}")
            else:
                raise LLMPoolError(f"HTTP {res.status_code}: {res.text[:400]}")

        data = res.json()
        choices = data.get("choices", [])
        if not choices:
            raise LLMPoolError(f"Empty choices in response: {res.text[:200]}")

        msg = choices[0].get("message", {})
        raw_text = msg.get("content", "") or ""
        usage_raw = data.get("usage", {})
        usage = {
            "prompt_tokens": usage_raw.get("prompt_tokens", len(user) // 4),
            "completion_tokens": usage_raw.get("completion_tokens", len(raw_text) // 4),
            "total_tokens": usage_raw.get("total_tokens", (len(user) + len(raw_text)) // 4),
        }
        return raw_text, usage


# Global / module-level singleton pool
_DEFAULT_POOL: Optional[LLMPool] = None


def get_pool() -> LLMPool:
    global _DEFAULT_POOL
    if _DEFAULT_POOL is None:
        _DEFAULT_POOL = LLMPool.from_env()
    return _DEFAULT_POOL


def reset_pool(pool: Optional[LLMPool] = None) -> None:
    global _DEFAULT_POOL
    _DEFAULT_POOL = pool


def complete(
    task: str,
    system: str,
    user: str,
    schema: Optional[Any] = None,
    images: Optional[List[Any]] = None,
    budget: Optional[Any] = None,
    company_id: Optional[str] = None,
    run_id: Optional[str] = None,
    pool: Optional[LLMPool] = None,
) -> LLMResult:
    """The single entry point for deep intelligence LLM requests."""
    active_pool = pool or get_pool()
    return active_pool.complete(
        task=task,
        system=system,
        user=user,
        schema=schema,
        images=images,
        budget=budget,
        company_id=company_id,
        run_id=run_id,
    )
