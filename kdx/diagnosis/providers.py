from typing import Protocol, runtime_checkable

from kdx.collector.types import DiagnosisError


@runtime_checkable
class LLMProvider(Protocol):
    def complete(self, system: str, user: str, max_tokens: int) -> str: ...


class AnthropicProvider:
    def __init__(self, api_key: str, model: str, timeout: float):
        from anthropic import Anthropic

        self._client = Anthropic(api_key=api_key, timeout=timeout)
        self._model = model

    def complete(self, system: str, user: str, max_tokens: int) -> str:
        from anthropic import APIStatusError

        try:
            msg = self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
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
        return msg.content[0].text


class OpenAICompatibleProvider:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float):
        from openai import OpenAI

        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        self._model = model

    def complete(self, system: str, user: str, max_tokens: int) -> str:
        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except Exception as e:
            raise DiagnosisError(str(e)) from e
        return resp.choices[0].message.content or ""
