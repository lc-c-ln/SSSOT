"""Claude API 호출 (표준 라이브러리 urllib). 키가 없으면 LLM 기능은 자동으로 꺼진다."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

DEFAULT_MODEL = "claude-sonnet-5-5"


class LLM:
    def complete(self, system: str, user: str, max_tokens: int = 2000, json_mode: bool = False) -> str:
        raise NotImplementedError


class AnthropicLLM(LLM):
    def __init__(self, api_key: str | None = None, model: str | None = None, timeout: int = 120):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model or os.environ.get("SSOT_MODEL", DEFAULT_MODEL)
        self.base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        self.timeout = timeout
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY 환경변수가 필요합니다.")

    def complete(self, system: str, user: str, max_tokens: int = 2000, json_mode: bool = False) -> str:
        body = json.dumps({
            "model": self.model, "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": user}],
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}/v1/messages", data=body, method="POST",
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


class OpenAICompatLLM(LLM):
    """OpenAI 호환 /v1/chat/completions. Ollama(기본), LM Studio, Groq, OpenRouter, Gemini 호환 엔드포인트 공용."""

    def __init__(self, base_url: str | None = None, model: str | None = None, api_key: str | None = None,
                 timeout: int = 600):
        self.base = (base_url or os.environ.get("SSOT_BASE_URL", "http://localhost:11434/v1")).rstrip("/")
        self.model = model or os.environ.get("SSOT_MODEL", "qwen2.5:7b")
        self.api_key = api_key or os.environ.get("SSOT_API_KEY", "ollama")
        self.timeout = timeout  # 로컬 모델은 첫 호출(모델 로딩)이 느리므로 넉넉히

    def complete(self, system: str, user: str, max_tokens: int = 2000, json_mode: bool = False) -> str:
        body = json.dumps({
            "model": self.model, "max_tokens": max_tokens, "temperature": 0.1,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}/chat/completions", data=body, method="POST",
            headers={"Authorization": f"Bearer {self.api_key}", "content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise RuntimeError(f"LLM 서버에 연결할 수 없습니다 ({self.base}): {e.reason}. "
                               "Ollama 사용 시 `ollama serve` 실행과 `ollama pull <모델>` 을 확인하세요.") from e
        return data["choices"][0]["message"]["content"] or ""


def make_llm(provider: str = "ollama", model: str | None = None, base_url: str | None = None) -> LLM:
    if provider == "anthropic":
        if os.environ.get("SSOT_LOCAL_ONLY") == "1":
            raise RuntimeError("SSOT_LOCAL_ONLY=1 이므로 외부 LLM(anthropic)은 사용할 수 없습니다.")
        return AnthropicLLM(model=model)
    return OpenAICompatLLM(base_url=base_url, model=model)


def extract_json(text: str) -> dict:
    """응답에서 첫 JSON 객체를 꺼낸다 (코드펜스/설명이 섞여도 동작)."""
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b < a:
        raise ValueError("JSON 응답을 찾지 못했습니다.")
    return json.loads(re.sub(r",\s*([}\]])", r"\1", text[a:b + 1]))
