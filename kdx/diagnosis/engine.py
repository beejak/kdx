import json
import re
from json import JSONDecoder

from anthropic import Anthropic, APIStatusError
from pydantic import ValidationError

from kdx.collector.types import DiagnosisContext, DiagnosisError, DiagnosisResult
from kdx.config import Settings
from kdx.diagnosis.prompts import SYSTEM_PROMPT, build_context_message


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


def diagnose(ctx: DiagnosisContext, settings: Settings) -> DiagnosisResult:
    client = Anthropic(api_key=settings.anthropic_api_key, timeout=30.0)
    user_content = build_context_message(ctx)
    try:
        msg = client.messages.create(
            model=settings.model,
            max_tokens=settings.max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
    except APIStatusError as e:
        status = getattr(e, "status_code", None)
        if status is None and getattr(e, "response", None) is not None:
            status = getattr(e.response, "status_code", None)
        if status == 529:
            raise DiagnosisError("Claude is overloaded, try again") from e
        raise DiagnosisError(str(e)) from e
    except Exception as e:
        raise DiagnosisError(str(e)) from e
    block = msg.content[0]
    raw_text = block.text
    try:
        parsed = _extract_json(raw_text)
        return DiagnosisResult.model_validate(parsed)
    except (json.JSONDecodeError, ValidationError, DiagnosisError, ValueError) as e:
        snippet = raw_text[:500]
        raise DiagnosisError(f"Invalid diagnosis response: {snippet}") from e
