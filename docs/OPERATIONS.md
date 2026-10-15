# 운영 가이드

## 1. 설치 (폐쇄망 포함)
- Python 3.11 이상. 필수 외부 패키지 없음(표준 라이브러리만). Git 2.31 이상이 PATH 에 있어야 합니다.
- Ollama 설치 후 모델을 미리 받아 둡니다(폐쇄망이면 인터넷 PC 에서 받아 `~/.ollama` 를 옮깁니다).
  - 분류·수정안: `ollama pull qwen2.5-coder:7b` (GPU 여유가 있으면 14b/32b 가 품질이 좋습니다)
  - 임베딩(선택, 영향 분석 정확도 향상): `ollama pull bge-m3`
- 실제 Oracle 메타데이터 조회를 쓸 때만 `oracledb` 휠을 반입해 설치합니다(`pip install oracledb`, Thin 모드라 Oracle Client 불필요).

## 2. 실행
```bash
# 이 PC 에서만 사용
python -m ssot sr

# 사내 다른 PC/GitLab 웹훅에서 접속해야 할 때: 로그인 필수
export SSOT_USERS="kim:비밀번호,lee:sha256:<해시>"     # Windows: set / setx
export SSOT_WEBHOOK_SECRET="긴-임의-문자열"             # GitLab 웹훅 사용 시
python -m ssot sr --host 0.0.0.0 --port 8766 --db D:\ssot\ssot_sr.db
```
- `SSOT_USERS` 가 있으면 모든 화면이 로그인(브라우저 Basic 인증)을 요구하고, 로그의 요청자/승인자는 **입력한 이름이 아니라 로그인한 계정**으로 기록됩니다. 5회 연속 실패하면 5분간 차단됩니다.
- 비밀번호 해시: `python -c "import hashlib;print(hashlib.sha256('비밀번호'.encode()).hexdigest())"` → `이름:sha256:<해시>`
- Basic 인증은 암호화되지 않으므로 사내망에서도 HTTPS 리버스 프록시(nginx 등) 뒤에 두는 것을 권장합니다.
- 외부 LLM 차단: `SSOT_LOCAL_ONLY=1`. 로컬/사설망이 아닌 Ollama 주소는 기본 거부됩니다.

| 환경변수 | 의미 |
|---|---|
| `OLLAMA_HOST` | Ollama 주소 (기본 http://localhost:11434) |
| `SSOT_MODEL` / `SSOT_EMBED_MODEL` | 생성 / 임베딩 모델 (기본 qwen2.5-coder:7b / bge-m3) |
| `SSOT_NUM_CTX` | 컨텍스트 윈도우 토큰 수 (GPU 메모리에 맞춰 조정) |
| `SSOT_USERS` | 로그인 계정 목록 |
| `SSOT_WEBHOOK_SECRET` | GitLab 웹훅 Secret token (없으면 웹훅 거부) |
| `SSOT_GIT_EMAIL` | 승인 커밋의 작성자 이메일 (기본 ssot@localhost) |
| `SSOT_SCHEMA_PRIORITY=doc` | 스키마 문서와 DB 가 다를 때 문서 우선 (기본 DB 우선) |
| 프로젝트별 | GitLab 토큰 변수(예: `GITLAB_TOKEN_MEMBER`), DB 변수(`SSOT_DB_<접두어>_USER/PASSWORD/DSN/OWNER`) |

## 3. 프로젝트 등록 (화면: 프로젝트 설정)
1. 대상 저장소를 서버 PC 에 clone 하고 그 경로를 입력합니다. 시스템은 이 폴더를 직접 수정하지 않고 임시 worktree 에서만 작업합니다. 기본 브랜치는 **로컬 사본** 기준이므로 주기적으로 `git pull` 하세요.
2. GitLab 주소(`https://gitlab.company.com/group/project`)와 토큰이 든 환경변수 *이름*을 입력합니다. 토큰은 프로젝트 Access Token 이며 권한은 `api`, `write_repository` 만 주세요(Developer 이상 역할).
3. 스키마 문서(DDL/표)와 갱신할 문서 경로를 입력합니다. 저장 후 표시되는 연결 확인 결과(✓/✗)를 모두 확인합니다.

## 4. GitLab 웹훅 (문서 자동 갱신)
프로젝트 *Settings → Webhooks*: URL `http://<서버>:8766/api/hooks/gitlab`, Secret token = `SSOT_WEBHOOK_SECRET`, Trigger = *Push events*(기본 브랜치). 웹훅은 로그인 대신 이 토큰으로 인증하며, 토큰이 서버에 없으면 항상 거부됩니다. 서버에서 GitLab 으로의 `git fetch/push` 와 MR 생성 API 호출이 가능해야 합니다.

## 5. Oracle(RDS) 읽기 전용 계정 (선택 기능)
```sql
CREATE USER ssot_ro IDENTIFIED BY "…";
GRANT CREATE SESSION TO ssot_ro;
GRANT SELECT_CATALOG_ROLE TO ssot_ro;     -- 딕셔너리 뷰만 필요. 업무 테이블 SELECT 권한은 주지 않습니다.
```
RDS 에서는 `SELECT_CATALOG_ROLE` 부여가 제한될 수 있습니다. 그 경우 대상 스키마 소유자가 `ALL_*` 뷰로 보이도록 최소한의 `GRANT SELECT ON <테이블>` 만 부여하세요(메타데이터 조회에는 데이터 행을 읽지 않습니다). 시스템은 DB 로 임의 SQL 을 보내지 않으며 제안한 SQL 은 담당자가 직접 실행합니다.

## 6. 백업·감사
- 모든 데이터는 `--db` 로 지정한 SQLite 파일 하나입니다. 서버를 멈추거나 `sqlite3 ssot_sr.db ".backup backup.db"` 로 정기 백업하세요.
- 감사 로그는 수정·삭제가 막혀 있고 해시 체인으로 변조를 검증합니다(화면 상단 "감사 로그 무결성"). SR 별 CSV 를 내려받을 수 있습니다.
- 프롬프트/응답의 비밀번호·토큰 패턴은 `***` 로 가려 저장합니다(`--no-mask` 로 끔).

## 7. 성능·자원 가이드
- 10만 줄 규모 저장소: 첫 영향 분석 약 5초(색인 구축), 이후 1초 미만(메모리 캐시). 임베딩은 처음 한 번만 Ollama 로 계산하고 SQLite 에 캐시합니다(파일이 바뀐 청크만 재계산). 처음 임베딩에는 수 분이 걸릴 수 있습니다.
- LLM 호출은 서버에서 한 번에 하나씩 처리합니다(GPU 하나 가정). 요청이 몰리면 대기열처럼 순서대로 처리됩니다.
- 컨텍스트가 모자라면 `SSOT_NUM_CTX` 를 올리거나(메모리 사용 증가) 더 작은 모델/양자화(4bit)를 사용하세요. 큰 파일은 후보 위치 주변만 모델에 보냅니다.

## 8. 문제 해결
| 증상 | 확인 |
|---|---|
| 상단에 "Ollama 연결 안 됨" | Ollama 실행 여부, `OLLAMA_HOST`, 방화벽 |
| "AI 응답 대기" | Ollama 를 켠 뒤 상세 화면의 *AI 재분류* |
| "수정안이 저장소에 적용되지 않습니다" | 로컬 clone 을 `git pull` 하고 *수정안 다시 생성* |
| 승인 후 "GitLab 반영 대기" | 토큰 환경변수, GitLab 주소, 네트워크 확인 후 *GitLab에 push 후 MR 생성* |
| 문서 제안이 안 생김 | 프로젝트의 문서 경로 설정, 웹훅 Secret, *문서 갱신* 탭의 "지금 스캔" |
| 스키마 문서를 못 읽음 | 헤더에 `테이블명`, `컬럼명` 열이 있는지 확인(없으면 문서 샘플 공유) |
