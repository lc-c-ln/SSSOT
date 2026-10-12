# SSSOT 사용법

Spring/Java 소스를 기준으로 **컴포넌트 정의서, API 목록, 설정표, 운영자 매뉴얼**(docx / md / xlsx)을 갱신하는 CLI 도구입니다.

- 외부 패키지 설치 없음 (Python 3.11 이상, 표준 라이브러리만 사용)
- 표 갱신은 규칙 기반이라 AI 없이도 동작
- AI(Ollama 등)는 선택 사항: 신규 행 설명 생성, 운영자 매뉴얼 본문 점검에만 사용

---

## 1. 빠른 시작

프로젝트 폴더(`D:\SSSOT`)에서 실행합니다.

```bash
# 1) 소스에서 무엇이 추출되는지 확인
python -m ssot analyze C:\work\my-spring-app

# 2) 문서를 소스와 비교 (미리보기, 문서는 수정되지 않음)
python -m ssot sync --src C:\work\my-spring-app --doc 컴포넌트정의서.docx

# 3) 결과가 맞으면 저장 (원본은 그대로 두고 *.updated.docx 생성)
python -m ssot sync --src C:\work\my-spring-app --doc 컴포넌트정의서.docx --apply
```

> 항상 **2번(미리보기) → 리포트 확인 → 3번(저장)** 순서로 사용하세요.

---

## 1-1. 웹 UI로 사용하기

```bash
python -m ssot web            # http://127.0.0.1:8765 (포트 변경: --port 9000)
```

브라우저에서 아래 순서로 사용합니다.

1. **소스**: Spring 프로젝트 폴더 경로를 입력하거나 zip으로 올립니다.
2. **문서**: docx / md / xlsx 파일을 끌어다 놓습니다 (여러 개 가능).
3. **옵션**: AI 사용을 켜면 Ollama에 받아 둔 모델이 목록에 나타납니다.
4. **미리보기 실행** → 변경 목록이 표로 나옵니다 (이전 값은 빨강, 새 값은 초록).
5. **항목별로 체크**해서 반영할 것만 고르고 **선택 항목 적용하고 내려받기**를 누릅니다.

- 기본 선택: 수정·채움·추가 (AI의 본문 수정은 **기본 해제**이므로 내용을 확인한 뒤 직접 체크하세요).
- 체크박스가 없는 줄(확인 필요, 소스에 없음)은 사람이 판단할 알림입니다.
- 원본은 수정되지 않고 `이름.updated.확장자`가 내려받아집니다.
- 이 PC에서만 접속되도록 `127.0.0.1`에 열립니다. 다른 PC에서 쓰려면 `--host 0.0.0.0`을 줄 수 있지만 인증이 없으므로 사내망에서만 사용하세요.

---

## 2. 명령어

### `analyze` — 소스 분석 결과 확인

```bash
python -m ssot analyze <스프링 프로젝트 루트>
python -m ssot analyze <스프링 프로젝트 루트> --json     # 상세 JSON
```

추출 대상:

| 종류 | 내용 |
|---|---|
| 클래스 | `@RestController` `@Controller` `@Service` `@Repository` `@Entity` `@Configuration` `@Component` `@Mapper` 등 — 패키지, 메서드, 엔티티 필드, Javadoc 설명 |
| 엔드포인트 | HTTP 메서드, 경로(클래스+메서드 매핑 결합), 파라미터, 반환 타입, 설명 |
| 설정 | `application*.yml` / `application*.properties`, `@Value("${...}")` — 비밀번호·토큰류 값은 `****`로 마스킹 |

`test`, `build`, `target`, `.git`, `node_modules` 폴더는 제외됩니다.

### `sync` — 문서 갱신

```bash
python -m ssot sync --src <소스 루트> --doc <문서> [<문서> ...] [옵션]
```

| 옵션 | 설명 |
|---|---|
| `--doc` | 대상 문서. `.docx` `.md` `.xlsx` 여러 개 지정 가능 |
| `--apply` | 실제로 저장. 없으면 미리보기만 |
| `--out-dir <폴더>` | 저장 폴더 (기본: 원본과 같은 폴더, 파일명 `이름.updated.확장자`) |
| `--in-place` | 원본 덮어쓰기 (`.bak` 백업 자동 생성). `--apply`와 함께 사용 |
| `--report <파일.md>` | 변경 리포트를 파일로 저장 |
| `--check` | 불일치가 있으면 종료코드 1 (CI에서 문서 최신 여부 검사용) |
| `--strict` | 메서드/필드/파라미터 칸을 집합 일치로 엄격 비교 |
| `--config <ssot.toml>` | 표 헤더 별칭 설정 파일 |
| `--llm` | AI 사용 (아래 3장 참고) |
| `--provider` | `ollama`(기본) / `openai` / `anthropic` |
| `--model` | 모델명 (기본 `qwen2.5:7b`) |
| `--base-url` | OpenAI 호환 서버 주소 (기본 `http://localhost:11434/v1`) |

사용 예:

```bash
# 여러 문서를 한 번에, 리포트 파일로 저장
python -m ssot sync --src C:\work\app --doc 정의서.docx API목록.xlsx 매뉴얼.md --report report.md

# 저장 위치 지정
python -m ssot sync --src C:\work\app --doc 정의서.docx --apply --out-dir C:\work\out

# 원본 덮어쓰기 (정의서.docx.bak 백업 생성)
python -m ssot sync --src C:\work\app --doc 정의서.docx --apply --in-place

# CI: 문서가 소스와 어긋나면 실패
python -m ssot sync --src . --doc docs\정의서.md --check
```

---

## 3. AI(Ollama) 사용하기 — 선택

AI 없이도 표 갱신은 모두 동작합니다. AI는 아래 두 가지에만 필요합니다.

1. 소스에 Javadoc이 없는 신규 항목의 **한 줄 설명 생성**
2. **운영자 매뉴얼 본문**에서 소스와 다른 값(포트, 설정값 등) 찾기

### 준비 (최초 1회)

1. https://ollama.com/download 에서 Ollama 설치
2. 모델 받기 (PC 사양에 맞게)

| 모델 | 용량 | 권장 RAM | 비고 |
|---|---|---|---|
| `qwen2.5:3b` | 약 1.9GB | 4~8GB | 가벼움, 본문 점검은 일부 놓칠 수 있음 |
| `qwen2.5:7b` | 약 4.7GB | 8GB 이상 | 기본값 |
| `qwen2.5:14b` | 약 9GB | 16GB 이상 | 본문 점검 품질이 가장 좋음 |

```bash
ollama pull qwen2.5:7b
```

### 실행

```bash
python -m ssot sync --src C:\work\app --doc 매뉴얼.md --llm
python -m ssot sync --src C:\work\app --doc 매뉴얼.md --llm --model qwen2.5:3b
```

모델은 환경변수로도 지정할 수 있습니다: `SSOT_MODEL`, `SSOT_BASE_URL`

### 다른 AI 서비스

```bash
# OpenAI 호환 서버 (LM Studio, Groq, OpenRouter 등)
python -m ssot sync ... --llm --provider openai --base-url https://서버주소/v1 --model 모델명
#   키가 필요하면 환경변수 SSOT_API_KEY 설정

# Claude API
python -m ssot sync ... --llm --provider anthropic     # ANTHROPIC_API_KEY 필요
```

### 본문 점검 방식과 안전장치

- 소스 사실(클래스명, 경로, 설정키)이 언급된 **문단만**, **문단 하나씩** AI에 보냅니다.
- AI는 문단을 다시 쓰지 않고 **"틀린 값 → 올바른 값"만 제안**합니다. 교체는 프로그램이 해당 글자만 바꿉니다.
- 올바른 값이 소스 사실에 없거나, 틀린 값이 문단에 없으면 **적용하지 않고** 리포트에 "확인 필요"로 남깁니다.
- 작은 모델은 일부를 놓칠 수 있습니다. 대신 잘못된 내용이 문서에 들어가지는 않습니다.

---

## 4. 문서 작성 규칙 (표 인식)

표의 **헤더(첫 행)** 를 보고 표 종류와 열 의미를 자동으로 판단합니다.

| 표 종류 | 판별 조건 | 인식하는 열 이름 (일부) |
|---|---|---|
| **API 목록** | `경로`/`URL`/`path` 열이 있음 | 메서드(HTTP), 경로, 컨트롤러, 핸들러, 파라미터, 응답/반환, 설명 |
| **설정표** | `설정키`/`프로퍼티`/`key` 열 + `기본값`/`값` 열 | 설정키, 기본값, 설명 |
| **컴포넌트 정의서** | `클래스명`/`컴포넌트`/`class` 열이 있음 | 클래스명, 유형/구분, 패키지, 설명/역할, 주요 메서드, 필드 |

- 번호 열(`번호`, `No`, `순번`)은 신규 행 추가 시 자동으로 이어서 채웁니다.
- API 표에 메서드 열이 따로 없으면 경로 칸에 `GET /api/users` 형태로 적어도 인식합니다.
- 설정 키는 `ttl-seconds` / `ttlSeconds` / `ttl_seconds`를 같은 키로 봅니다.
- 유형 칸은 `컨트롤러`/`서비스`/`Service`/`@Service`를 같은 값으로 봅니다.

### 열 이름이 인식되지 않을 때

소스 루트(또는 `--config`로 지정한 경로)에 `ssot.toml`을 만들어 별칭을 추가합니다.

```toml
[aliases.endpoint]
path = ["연동주소"]
returns = ["응답 데이터"]

[aliases.class]
summary = ["컴포넌트 개요"]
name = ["모듈 ID"]
```

필드 이름: `class` → `name stereotype package summary methods fields` /
`endpoint` → `http_method path controller handler params returns summary` /
`config` → `key value summary`

---

## 5. 동작 규칙 (무엇을 바꾸고 무엇을 바꾸지 않나)

| 상황 | 동작 | 리포트 구분 |
|---|---|---|
| 표의 유형·패키지·경로·HTTP 메서드·설정값 등이 소스와 다름 | 해당 칸만 소스 값으로 수정 | 수정 |
| 메서드·필드·파라미터 칸에 소스 항목이 빠져 있음 | 소스 기준 목록으로 수정 (표기만 다르면 그대로 둠) | 수정 |
| 설명 칸이 비어 있음 | Javadoc(없으면 AI)으로 채움 | 채움 |
| **설명 칸에 이미 사람이 쓴 내용이 있음** | **덮어쓰지 않음.** 시그니처가 바뀌었으면 알림만 | 확인 필요 |
| 소스에는 있는데 표에 행이 없음 | 표 끝에 행 추가 (여러 표가 있으면 같은 유형/컨트롤러가 모인 표에 추가) | 추가 |
| 표에는 있는데 소스에 없음 | **삭제하지 않음.** 알림만 | 소스에 없음 |
| 본문 문단의 값이 소스와 다름 (`--llm`) | 틀린 값만 교체 | 본문 수정 |

- 기본은 미리보기이며, `--apply`를 줘야 파일이 만들어집니다.
- 같은 명령을 다시 실행하면 추가 변경이 없습니다 (이미 반영된 내용은 건드리지 않음).
- docx/xlsx는 해당 표·문단만 수정하고 나머지 서식과 파트는 그대로 보존합니다.

---

## 6. 리포트 읽는 법

```
수정 2 · 확인 필요 1 · 채움 1 · 추가 2

| 구분 | 위치 | 대상 | 항목 | 이전 | 이후 | 근거/비고 |
| 수정 | 컴포넌트 정의서 표 1행 | UserController | methods | get | get, list, register, update | src/.../UserController.java:13 |
```

- **위치**: 문서의 표(앞선 제목 기준)와 행 번호
- **근거/비고**: 값이 나온 소스 파일과 줄 번호

---

## 7. 알려진 한계

- Java 분석기는 구조 분석 방식이라 Lombok이 만드는 메서드, 상수로 지정한 매핑 경로, 멀티모듈 설정 병합은 반영하지 못합니다.
- xlsx는 시트당 표 1개만 인식하며, 병합 셀과 Excel '표' 개체의 범위 자동 확장은 지원하지 않습니다.
- docx의 중첩 표, 텍스트 상자, 변경 추적 중인 문서는 검증되지 않았습니다. 중요한 문서는 `--in-place`보다 `--out-dir` 저장 후 비교하세요.
- 작은 AI 모델(3b)은 본문의 틀린 값을 일부 놓칠 수 있습니다.

---

## 8. 문제 해결

| 증상 | 확인 |
|---|---|
| 표가 갱신되지 않음 | 헤더 열 이름이 4장 표에 있는지 확인. 없으면 `ssot.toml`에 별칭 추가 |
| `LLM 서버에 연결할 수 없습니다` | Ollama 실행 여부, `ollama pull <모델>` 완료 여부 확인 |
| AI 첫 호출이 오래 걸림 | 모델을 메모리에 올리는 시간이며 정상 (제한 시간 10분) |
| `확인 필요: LLM 제안 거절` | AI가 소스에 없는 값을 제안해 막은 것. 문서는 그대로이며 해당 문단을 직접 확인 |
| 소스에서 기대한 항목이 안 나옴 | `analyze`로 추출 결과 확인. `@RestController` 등 스테레오타입 어노테이션이 있는 클래스만 대상 |
| 테스트 실행 | `python -m pytest` (pytest 필요: `pip install pytest`) |
