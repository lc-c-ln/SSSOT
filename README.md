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

## SR 접수 서버 (M1)

```bash
python -m ssot sr                # http://127.0.0.1:8766  (DB: ssot_sr.db, 로컬 Ollama 필요)
```

웹 폼으로 SR을 등록하면 로컬 Ollama(`/api/chat`)가 소스 수정/데이터 수정으로 분류합니다. 신뢰도가 낮거나 응답이 이상하면 담당자 확인 대기, Ollama가 꺼져 있으면 `AI 응답 대기`로 보존되어 재시도할 수 있습니다. 모든 LLM 호출(프롬프트·응답)과 상태 변경은 SQLite 감사 로그(수정·삭제 불가, 해시 체인, 비밀값 마스킹)에 남습니다. 설계는 [docs/PRD.md](docs/PRD.md) 참고.

**소스 미리 분석**: 프로젝트를 저장하면 소스를 즉시 분석(파싱·색인)해 DB에 저장해 둡니다. SR이 들어오면 이 색인으로 바로 검색하고, 그 사이 바뀐 파일만 다시 읽습니다(GitLab 웹훅으로 새 커밋이 들어와도 갱신). 프로젝트 카드의 **소스 분석** 버튼으로 임베딩을 미리 계산하고, *AI 요약 포함*을 켜면 역할이 분명한 클래스마다 Ollama가 한국어 요약을 만들어 둡니다(처음 한 번, 파일이 바뀐 클래스만 다시). 한국어 요청과 영문 코드명을 연결하는 정확도가 올라갑니다.

**전체 흐름 (소스 수정 건)**: 접수 → 자동 분류 → 영향 분석(후보 파일·점수·근거) → 담당자가 대상 파일 확정 → 수정안(Diff) 생성 → 승인 / 수정 요구 / 반려 → 승인 시 `sr/<번호>-<제목>` 브랜치에 커밋 → GitLab push + Merge Request 생성.
- 먼저 화면의 **프로젝트 설정** 탭에서 프로젝트(로컬 clone 경로, GitLab 주소, 기본 브랜치, 토큰이 들어 있는 *환경변수 이름*)를 등록하세요. 토큰 값 자체는 저장하지 않습니다. 필요 권한: `api`, `write_repository`.
- 수정안은 사용자의 작업 폴더가 아닌 임시 worktree 에서 만들고 `git apply --check` 로 검증합니다. 기본 브랜치의 **로컬 사본** 기준이므로 미리 `git pull` 해 두세요.
- 요청자 본인은 승인할 수 없습니다(`--allow-self-approve` 로 해제).
- 토큰이 없거나 GitLab 이 응답하지 않아도 로컬 브랜치 커밋까지는 유지되며, 화면의 버튼으로 나중에 push/MR 을 다시 시도할 수 있습니다.

**문서 자동 갱신 (M4)**: 프로젝트 설정에 *갱신할 문서 경로*(절대경로 또는 clone 기준 상대경로)를 등록하면, 기본 브랜치에 커밋이 들어올 때마다 그 시점의 소스와 문서를 대조해 **문서 갱신 제안** 탭에 변경 목록을 만듭니다. 항목별로 승인하면 `<이름>.updated.<확장자>` 로 저장합니다(원본은 수정하지 않음).
- GitLab 웹훅: 프로젝트 *Settings → Webhooks* 에 `http://<서버>:8766/api/hooks/gitlab`, *Push events*, Secret token 을 등록하고 서버를 `SSOT_WEBHOOK_SECRET=<같은 값>` 으로 실행하세요(`--host 0.0.0.0` 필요, 사내망에서만). 비밀값이 없으면 웹훅은 거부됩니다.
- 웹훅 없이도 *문서 갱신* 탭의 "지금 스캔"으로 최신 커밋을 처리할 수 있습니다.
- 커밋 메시지에 `SR-0142` 가 있으면 해당 SR의 처리 이력에 문서 변경이 함께 기록됩니다(승인 커밋의 메시지에 자동 포함됨).

**데이터 수정 건 (M5)**: 분류가 *데이터 수정*이면 프로젝트의 DB 스키마 문서(`.sql` DDL, md/docx/xlsx 표)에서 관련 테이블·컬럼을 찾고, FK 로 연결된 테이블(영향 범위)과 **Oracle 문법의 SQL 안**을 보여줍니다.
- SQL 은 **절대 실행하지 않습니다.** 변경문(UPDATE/DELETE/INSERT)은 코드가 검증해 WHERE 없는 변경, `1=1`, DDL, 여러 문장, 주석, SYS/DBMS_ 사용을 차단하고 스키마에 없는 이름은 경고합니다. 영향 건수 확인용 `SELECT COUNT(*)`·미리보기·백업 권고는 변경문의 WHERE 에서 코드가 직접 만듭니다.
- 스키마 문서 표는 헤더에 `테이블명`, `컬럼명` 열이 있으면 인식합니다(시트/제목이 테이블명인 형식도 지원). 인식이 안 되면 문서 샘플을 알려 주세요.
- 실제 DB 메타데이터 조회(선택): `pip install oracledb` 후 프로젝트 설정의 *환경변수 접두어*(예: `MEMBER`)를 지정하고 `SSOT_DB_MEMBER_USER`, `SSOT_DB_MEMBER_PASSWORD`, `SSOT_DB_MEMBER_DSN`(host:1521/서비스명), 선택 `SSOT_DB_MEMBER_OWNER` 를 설정합니다. **읽기 전용 계정**을 쓰세요. 딕셔너리 뷰만 조회하고 업무 데이터 행은 읽지 않습니다. 문서와 DB 가 다르면 불일치 목록을 보여주며 기본은 DB 우선입니다(`SSOT_SCHEMA_PRIORITY=doc` 로 반전).
- 담당자가 SQL 을 직접 실행한 뒤 *실행 완료로 기록*(처리 건수, 메모)하면 감사 로그에 "담당자 보고"로 남습니다.

운영(로그인·웹훅·Oracle 계정·백업·문제 해결)은 **[docs/OPERATIONS.md](docs/OPERATIONS.md)** 를 보세요. 외부 주소(`--host 0.0.0.0`)로 열려면 `SSOT_USERS` 로그인 설정이 필수입니다.

환경변수: `OLLAMA_HOST`, `SSOT_MODEL`(기본 qwen2.5-coder:7b), `SSOT_NUM_CTX`, `SSOT_LOCAL_ONLY=1`(외부 LLM 차단). 로컬/사설망이 아닌 Ollama 주소는 거부됩니다.

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
