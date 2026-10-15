"""로컬 Ollama 네이티브 API 클라이언트 (/api/chat, /api/embed, /api/tags). 표준 라이브러리만 사용.

폐쇄망 전제이므로 호스트가 루프백/사설망이 아니면 기본적으로 거부한다(SSOT_ALLOW_REMOTE_OLLAMA=1 로 해제).
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from .llm import LLM

DEFAULT_HOST = "http://localhost:11434"


class OllamaError(RuntimeError):
    """Ollama 호출 실패. retryable=True 면 연결/일시 오류라 나중에 재시도할 수 있다."""

    def __init__(self, msg: str, retryable: bool = True):
        super().__init__(msg)
        self.retryable = retryable


def _is_local(host: str) -> bool:
    name = urlparse(host).hostname or ""
    if name in ("localhost",) or name.endswith(".local") or name.endswith(".internal") or "." not in name and name:
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        try:
            ip = ipaddress.ip_address(socket.gethostbyname(name))
        except OSError:
            return False
    return ip.is_loopback or ip.is_private


class OllamaLLM(LLM):
    def __init__(self, host: str | None = None, model: str | None = None, timeout: int = 600,
                 retries: int = 2, num_ctx: int | None = None, embed_model: str | None = None,
                 sleep=time.sleep):
        self.host = (host or os.environ.get("OLLAMA_HOST") or DEFAULT_HOST).rstrip("/")
        if "://" not in self.host:
            self.host = "http://" + self.host
        if os.environ.get("SSOT_ALLOW_REMOTE_OLLAMA") != "1" and not _is_local(self.host):
            raise OllamaError(f"로컬/사설망이 아닌 Ollama 주소는 허용되지 않습니다: {self.host}", retryable=False)
        self.model = model or os.environ.get("SSOT_MODEL", "qwen2.5-coder:7b")
        self.embed_model = embed_model or os.environ.get("SSOT_EMBED_MODEL", "bge-m3")
        self.timeout, self.retries, self.sleep = timeout, retries, sleep
        self.num_ctx = num_ctx or (int(os.environ["SSOT_NUM_CTX"]) if os.environ.get("SSOT_NUM_CTX") else None)
        self.last_call: dict = {}   # 감사 로그용: 마지막 호출의 모델·옵션·소요시간

    def _post(self, path: str, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(f"{self.host}{path}", data=body, method="POST",
                                         headers={"content-type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                if e.code < 500:   # 모델 없음(404) 등은 재시도해도 소용없다
                    hint = f" `ollama pull {payload.get('model')}` 를 확인하세요." if e.code == 404 else ""
                    raise OllamaError(f"Ollama 오류 {e.code}: {detail}{hint}", retryable=False) from e
                last = e
            except (urllib.error.URLError, TimeoutError, ConnectionError, socket.timeout) as e:
                last = e
            if attempt < self.retries:
                self.sleep(2 ** (attempt + 1))
        raise OllamaError(f"Ollama 에 연결할 수 없습니다 ({self.host}): {last}. `ollama serve` 실행 여부를 확인하세요.")

    def complete(self, system: str, user: str, max_tokens: int = 2000, json_mode: bool = False) -> str:
        options: dict = {"temperature": 0, "num_predict": max_tokens}
        if self.num_ctx:
            options["num_ctx"] = self.num_ctx
        payload: dict = {"model": self.model, "stream": False, "options": options,
                         "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if json_mode:
            payload["format"] = "json"
        t0 = time.time()
        data = self._post("/api/chat", payload)
        self.last_call = {"model": self.model, "options": options, "json_mode": json_mode,
                          "seconds": round(time.time() - t0, 2)}
        return (data.get("message") or {}).get("content", "") or ""

    def embed(self, texts: list[str]) -> list[list[float]]:
        data = self._post("/api/embed", {"model": self.embed_model, "input": texts})
        return data.get("embeddings", [])

    def status(self) -> dict:
        try:
            with urllib.request.urlopen(f"{self.host}/api/tags", timeout=3) as r:
                models = [m["name"] for m in json.loads(r.read().decode()).get("models", [])]
        except Exception as e:  # noqa: BLE001 - 상태 표시용
            return {"ok": False, "models": [], "host": self.host, "error": str(e)}
        return {"ok": True, "models": models, "host": self.host, "model": self.model,
                "model_ready": any(m == self.model or m.split(":")[0] == self.model for m in models)}
