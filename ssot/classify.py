"""SR 자동 분류 (소스 수정 / 데이터 수정). LLM 출력은 형식·범위를 검증하고, 애매하면 담당자 확인으로 넘긴다."""
from __future__ import annotations

from dataclasses import dataclass

from .llm import LLM, extract_json
from .ollama import OllamaError
from .store import Store

CONFIDENCE_THRESHOLD = 0.7
PROMPT_VERSION = "classify-v1"

SYSTEM = """당신은 IT 서비스 요청(SR)을 분류하는 도우미입니다.
요청을 아래 둘 중 하나로 분류하세요.
- code: 프로그램 소스(기능 추가/개선, 버그 수정, API/화면/배치 변경)를 고쳐야 하는 요청
- data: 소스 변경 없이 DB 데이터 값을 조회/정정/일괄 변경해야 하는 요청
둘 다 해당하거나 판단할 수 없으면 unknown 으로 답하세요.
반드시 JSON 한 개만 출력하세요: {"type":"code|data|unknown","confidence":0~1 숫자,"reason":"한두 문장 한국어 근거"}"""


@dataclass
class Classification:
    type: str
    confidence: float
    reason: str


def parse_classification(text: str) -> Classification:
    data = extract_json(text)
    t = str(data.get("type", "")).strip().lower()
    if t not in ("code", "data", "unknown"):
        raise ValueError(f"알 수 없는 type: {t!r}")
    try:
        conf = float(data.get("confidence", 0))
    except (TypeError, ValueError) as e:
        raise ValueError("confidence 가 숫자가 아닙니다.") from e
    return Classification(t, max(0.0, min(1.0, conf)), str(data.get("reason", ""))[:500])


def process_sr(store: Store, llm: LLM, sr_id: int) -> str:
    """분류를 수행하고 SR 상태를 갱신한다. 반환: 최종 status. LLM 호출 전문은 감사 로그에 남긴다."""
    sr = store.get_sr(sr_id)
    if not sr:
        raise ValueError(f"SR {sr_id} 없음")
    store.update_sr(sr_id, status="classifying")
    user = f"[제목]\n{sr['title']}\n\n[내용]\n{sr['body']}"
    detail = {"prompt_version": PROMPT_VERSION, "model": getattr(llm, "model", "?"),
              "system": SYSTEM, "user": user}
    try:
        raw = llm.complete(SYSTEM, user, max_tokens=300, json_mode=True)
    except (OllamaError, RuntimeError) as e:
        store.audit(sr_id, "system", "llm.error", {**detail, "error": str(e)})
        store.update_sr(sr_id, status="pending_llm", reason=f"LLM 호출 실패: {e}"[:300])
        return "pending_llm"
    detail.update(response=raw, options=getattr(llm, "last_call", {}))
    try:
        c = parse_classification(raw)
    except (ValueError, KeyError) as e:
        store.audit(sr_id, "system", "llm.invalid_response", {**detail, "error": str(e)})
        store.update_sr(sr_id, status="needs_review", sr_type="unknown", confidence=0,
                        reason="모델 응답 형식 오류: 담당자가 분류해 주세요.")
        return "needs_review"
    store.audit(sr_id, "system", "sr.auto_classified", {**detail, "type": c.type, "confidence": c.confidence})
    if c.type == "unknown" or c.confidence < CONFIDENCE_THRESHOLD:
        store.update_sr(sr_id, status="needs_review", sr_type=c.type, confidence=c.confidence, reason=c.reason)
        return "needs_review"
    store.update_sr(sr_id, status="classified", sr_type=c.type, confidence=c.confidence, reason=c.reason)
    return "classified"


def set_manual(store: Store, sr_id: int, actor: str, sr_type: str, note: str = "") -> None:
    if sr_type not in ("code", "data"):
        raise ValueError("유형은 code 또는 data 여야 합니다.")
    if not actor.strip():
        raise ValueError("담당자 이름이 필요합니다.")
    sr = store.get_sr(sr_id)
    if not sr:
        raise ValueError(f"SR {sr_id} 없음")
    store.update_sr(sr_id, status="classified", sr_type=sr_type, reason=(note or "담당자 지정")[:500])
    store.audit(sr_id, actor.strip(), "sr.manual_classified",
                {"from": sr["sr_type"], "to": sr_type, "note": note})
