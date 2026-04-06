import json
import re
from json import JSONDecoder

from pydantic import ValidationError

from kdx.collector.types import DiagnosisContext, DiagnosisError, DiagnosisResult
from kdx.diagnosis.prompts import RETRY_SYSTEM_PROMPT, SYSTEM_PROMPT, build_context_message
from kdx.diagnosis.providers import LLMProvider


def _extract_json(text: str) -> dict:
    dec = JSONDecoder()
    m = re.search(r"```(?:json)?\s*(\{)", text, re.DOTALL | re.IGNORECASE)
    if m:
        start = m.start(1)
        return dec.raw_decode(text[start:])[0]
    brace = text.find("{")
    if brace == -1:
        raise DiagnosisError(f"No JSON found in response: {text[:200]}")
    return dec.raw_decode(text[brace:])[0]


def diagnose(
    ctx: DiagnosisContext, provider: LLMProvider, max_tokens: int = 1024
) -> DiagnosisResult:
    user_content = build_context_message(ctx)
    for attempt in range(2):
        prompt = SYSTEM_PROMPT if attempt == 0 else RETRY_SYSTEM_PROMPT
        raw = provider.complete(prompt, user_content, max_tokens)
        try:
            parsed = _extract_json(raw)
            return DiagnosisResult.model_validate(parsed)
        except (json.JSONDecodeError, ValidationError, DiagnosisError, ValueError) as exc:
            if attempt == 1:
                raise DiagnosisError(f"Invalid diagnosis response: {raw[:500]}") from exc
    raise DiagnosisError("Diagnosis failed")
