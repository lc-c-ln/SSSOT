# SSSOT — 소스 기준 문서 자동 갱신

Spring/Java 소스를 단일 진실 공급원(Single Source of Truth)으로 삼아 **컴포넌트 정의서, API 목록, 설정표, 운영자 매뉴얼**(docx / md / xlsx)을 갱신하는 도구입니다.

- 외부 패키지 설치 없음 (Python 3.11+ 표준 라이브러리만 사용)
- 표 갱신은 규칙 기반이라 AI 없이도 동작
- AI(로컬 Ollama 기본, OpenAI 호환 서버·Claude 선택)는 신규 설명 생성과 매뉴얼 본문 점검에만 사용
- CLI와 로컬 웹 UI 제공 (웹 UI에서 변경 항목별로 승인 후 반영)

## 빠른 시작

```bash
python -m ssot analyze <스프링 프로젝트 루트>                   # 소스에서 추출된 항목 확인
python -m ssot sync --src <루트> --doc 정의서.docx            # 미리보기 (문서 미수정)
python -m ssot sync --src <루트> --doc 정의서.docx --apply    # 정의서.updated.docx 저장
python -m ssot web                                            # 웹 UI: http://127.0.0.1:8765
```

자세한 옵션, 표 인식 규칙, AI 설정은 **[USAGE.md](USAGE.md)** 를 보세요.

## 동작 원칙

- 사람이 쓴 **설명은 덮어쓰지 않습니다.** 빈 칸만 채우고, 바뀐 항목이 있으면 "확인 필요"로 알립니다.
- 소스에 없는 행은 **삭제하지 않고** 알림만 합니다.
- AI는 문단을 다시 쓰지 않고 "틀린 값 → 올바른 값"만 제안하며, 올바른 값이 소스에 있는지 코드가 검증합니다.
- 원본은 수정하지 않고 `*.updated.*` 로 저장합니다. docx/xlsx는 해당 표·문단만 수정하고 나머지 서식은 보존합니다.

## 개발

```bash
pip install pytest
python -m pytest
```

구조: `ssot/java_analyzer.py`(Java 구조 분석) · `facts.py`(Spring 사실 추출) · `docs/`(md·docx·xlsx 어댑터) · `tablemap.py`(표 헤더 인식) · `sync.py`(대조·갱신 엔진) · `llm.py`(AI 연동) · `web.py`(웹 UI 서버)

## 한계

Lombok 생성 메서드, 상수로 된 매핑 경로, 멀티모듈 설정 병합 미지원 · xlsx는 시트당 표 1개 · docx 중첩 표 미검증. 웹 UI는 로컬 전용(인증 없음). 자세한 내용은 [USAGE.md](USAGE.md)의 "알려진 한계" 참고.
