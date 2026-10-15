"""데이터 수정 SR 처리: 스키마에서 관련 테이블/컬럼 식별 -> 영향 범위(FK) -> Oracle SQL 안 제시.

원칙: 이 모듈은 DB 에 SQL 을 실행하지 않는다. 모델이 만든 SQL 은 코드가 검증(허용 문장, WHERE 필수, 위험 구문 차단,
스키마 대조)하고, 영향 건수 확인용 SELECT 는 모델이 아니라 코드가 변경문의 WHERE 에서 직접 만든다.
"""
from __future__ import annotations

import re

from .impact import BM25, Chunk, embed_chunks, tokens
from .llm import LLM, extract_json
from .oracle_meta import configured, load_from_db
from .schema import Schema, load_schema, merge
from .store import Store

TOP_TABLES = 12
BLOCKED_WORDS = {"DROP", "TRUNCATE", "ALTER", "CREATE", "GRANT", "REVOKE", "EXEC", "EXECUTE", "BEGIN", "DECLARE",
                 "COMMIT", "ROLLBACK", "MERGE", "CALL", "SAVEPOINT", "RENAME", "PURGE", "FLASHBACK", "LOCK"}
KEYWORDS = set("""SELECT FROM WHERE AND OR NOT NULL IS IN LIKE BETWEEN EXISTS UPDATE SET DELETE INSERT INTO VALUES AS ON JOIN LEFT RIGHT
INNER OUTER FULL CROSS GROUP BY ORDER HAVING DISTINCT CASE WHEN THEN ELSE END ASC DESC FETCH FIRST NEXT ROWS ROW ONLY ROWNUM SYSDATE
SYSTIMESTAMP CURRENT_DATE CURRENT_TIMESTAMP COUNT SUM MIN MAX AVG NVL NVL2 COALESCE TO_DATE TO_CHAR TO_NUMBER TO_TIMESTAMP TRUNC ADD_MONTHS
MONTHS_BETWEEN SUBSTR INSTR LENGTH UPPER LOWER TRIM LTRIM RTRIM REPLACE DECODE INTERVAL DAY MONTH YEAR HOUR MINUTE SECOND DATE TIMESTAMP
ANY ALL UNION MINUS INTERSECT RETURNING DUAL NEXTVAL CURRVAL LISTAGG OVER PARTITION ROUND CEIL FLOOR MOD ABS CAST VARCHAR2 NUMBER YYYY MM DD
HH24 MI SS FM""".split())

SQL_SYSTEM = """당신은 Oracle 데이터 수정 SQL 초안을 작성하는 도우미입니다. 요청과 아래 스키마만 근거로 작성하세요.
규칙:
- 문장은 UPDATE / DELETE / INSERT 중 필요한 것만, 문장마다 한 개씩. UPDATE·DELETE 는 반드시 구체적인 WHERE 조건을 쓰세요.
- 스키마에 없는 테이블/컬럼을 쓰지 마세요. 요청에 없는 값은 지어내지 말고 :변수명 바인드 변수로 두세요.
- Oracle 문법을 쓰세요(날짜는 TO_DATE, 행 제한은 FETCH FIRST). COMMIT/ROLLBACK/DDL 은 쓰지 마세요.
- JSON 한 개만 출력: {"statements":[{"title":"무엇을 하는 문장인지","sql":"..."}],"notes":["담당자가 확인할 점"]}"""

PICK_SYSTEM = """당신은 DB 스키마에서 데이터 수정 요청과 관련된 테이블/컬럼을 고르는 도우미입니다.
후보 목록에 있는 이름만 사용하세요. JSON 한 개만 출력:
{"tables":[{"name":"테이블명","columns":["컬럼명"],"reason":"선정 근거 한 문장"}]}  (최대 5개 테이블)"""


# ---------------------------------------------------------------- 스키마 로드
def load_project_schema(proj: dict) -> tuple[Schema, list[str], str]:
    """(스키마, 문서-DB 불일치 목록, 출처 설명). 문서와 DB가 모두 있으면 DB 우선(SSOT_SCHEMA_PRIORITY=doc 으로 반전)."""
    import os
    doc = db = None
    notes: list[str] = []
    if proj["schema_doc"]:
        doc = load_schema(proj["schema_doc"])
    if proj.get("db_env_prefix"):
        if configured(proj["db_env_prefix"]):
            try:
                db = load_from_db(proj["db_env_prefix"])
            except Exception as e:  # noqa: BLE001 - DB 접속 실패 시 문서로 계속
                notes.append(f"DB 메타데이터 조회 실패, 문서만 사용합니다: {type(e).__name__}: {e}"[:300])
        else:
            notes.append("DB 접속 환경변수가 설정되지 않아 문서만 사용합니다.")
    if doc and db:
        prim, sec = (doc, db) if os.environ.get("SSOT_SCHEMA_PRIORITY") == "doc" else (db, doc)
        sch, diffs = merge(prim, sec)
        return sch, notes + diffs, sch.source
    sch = doc or db
    if not sch:
        raise ValueError("프로젝트에 DB 스키마 문서 경로(또는 DB 접속 설정)가 필요합니다.")
    return sch, notes, sch.source


# ---------------------------------------------------------------- 테이블 식별
def _table_text(t) -> str:
    return f"{t.name} {t.comment} " + " ".join(f"{c.name} {c.type} {c.comment}" for c in t.columns)


def find_tables(store: Store, llm: LLM, sch: Schema, title: str, body: str) -> tuple[list[dict], dict]:
    tabs = list(sch.tables.values())
    chunks = [Chunk(t.name, t.name, "", 0, _table_text(t), "") for t in tabs]
    bm = BM25([tokens(c.text) for c in chunks])
    q = {t: 1.0 for t in tokens(f"{title} {body}")}
    bs = [bm.score(i, q)[0] for i in range(len(chunks))]
    top = max(bs) or 1.0
    vs = [0.0] * len(chunks)
    used = False
    if hasattr(llm, "embed"):
        try:
            cv = embed_chunks(store, llm, chunks)
            qv = llm.embed([f"{title}\n{body}"[:1500]])[0]
            from .impact import _cos
            vs = [max(0.0, _cos(qv, v)) for v in cv]
            used = True
        except Exception:  # noqa: BLE001 - 임베딩 불가 시 키워드만
            pass
    vt = max(vs) or 1.0
    score = [((0.6 * bs[i] / top + 0.4 * vs[i] / vt) if used else bs[i] / top) for i in range(len(chunks))]
    order = sorted(range(len(tabs)), key=lambda i: -score[i])[:TOP_TABLES]
    order = [i for i in order if score[i] > 0]
    cand = [tabs[i] for i in order]
    log: dict = {"candidates": [t.name for t in cand], "vector": used}
    if not cand:
        return [], log
    listing = "\n".join(f"- {t.name} ({t.comment}): " + ", ".join(f"{c.name}[{c.comment or c.type}]" for c in t.columns[:40]) for t in cand)
    user = f"[요청 제목]\n{title}\n\n[요청 내용]\n{body}\n\n[후보 테이블]\n{listing}"
    picked: list[dict] = []
    try:
        raw = llm.complete(PICK_SYSTEM, user, max_tokens=800, json_mode=True)
        log.update(system=PICK_SYSTEM, user=user, response=raw)
        for it in extract_json(raw).get("tables", []):
            name = str(it.get("name", "")).upper()
            t = sch.tables.get(name)
            if not t or t not in cand:       # 후보에 없는 이름(환각)은 버린다
                continue
            cols = [str(c).upper() for c in it.get("columns", []) if t.col(str(c))]
            picked.append({"name": name, "comment": t.comment, "columns": cols, "reason": str(it.get("reason", ""))[:200], "by": "ai"})
    except Exception as e:  # noqa: BLE001
        log["error"] = f"{type(e).__name__}: {e}"
    if not picked:    # 모델 실패/전부 환각 -> 검색 점수 상위 3개 (컬럼은 담당자가 판단)
        picked = [{"name": t.name, "comment": t.comment, "columns": [], "reason": "검색 점수 상위 (AI 선정 실패)", "by": "search"}
                  for t in cand[:3]]
    return picked[:5], log


# ---------------------------------------------------------------- SQL 검증
def _mask_strings(sql: str) -> str:
    return re.sub(r"'(?:[^']|'')*'", lambda m: "'" + " " * (len(m.group()) - 2) + "'", sql)   # 길이 유지(원문 위치 계산용)


def _top_level_where(sql: str) -> str | None:
    m = _mask_strings(sql)
    depth, i = 0, 0
    while i < len(m):
        ch = m[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and re.match(r"\bWHERE\b", m[i:], re.I) and (i == 0 or not m[i - 1].isalnum()):
            return sql[i + 5:].strip()
        i += 1
    return None


def validate_sql(sql: str, sch: Schema, allowed: set[str]) -> dict:
    """반환: {sql, kind, blocked:[사유], warnings:[...], table, where}"""
    s = sql.strip()
    out = {"sql": "", "kind": "", "blocked": [], "warnings": [], "table": "", "where": ""}
    if s.endswith(";"):
        s = s[:-1].rstrip()
    masked = _mask_strings(s)
    if ";" in masked:
        out["blocked"].append("여러 문장이 들어 있습니다. 문장은 하나씩만 허용됩니다.")
    if "--" in masked or "/*" in masked:
        out["blocked"].append("SQL 주석은 허용되지 않습니다.")
    words = re.findall(r"[A-Za-z_][\w$#]*", masked)
    up = [w.upper() for w in words]
    bad = sorted({w for w in up if w in BLOCKED_WORDS})
    if bad:
        out["blocked"].append("허용되지 않는 구문: " + ", ".join(bad))
    if any(w.startswith(("DBMS_", "UTL_")) for w in up) or re.search(r"\b(SYS|SYSTEM)\s*\.", masked, re.I):
        out["blocked"].append("SYS/SYSTEM 객체나 DBMS_/UTL_ 패키지는 사용할 수 없습니다.")
    first = up[0] if up else ""
    if first not in ("UPDATE", "DELETE", "INSERT"):
        out["blocked"].append("UPDATE / DELETE / INSERT 문만 제안할 수 있습니다.")
    out["kind"] = first.lower()
    if first in ("UPDATE", "DELETE"):
        where = _top_level_where(s)
        if not where:
            out["blocked"].append("WHERE 조건이 없는 UPDATE/DELETE 는 허용되지 않습니다.")
        else:
            norm = re.sub(r"[\s()]", "", where.upper())
            if norm in ("1=1", "'1'='1'", "TRUE", "1<2", "0=0") or re.fullmatch(r"(\w+)=\1", norm):
                out["blocked"].append("항상 참이 되는 WHERE 조건은 허용되지 않습니다.")
            out["where"] = where
        m = re.match(r"\s*(?:UPDATE|DELETE\s+FROM|DELETE)\s+(" + r"[\w$#.\"]+" + r")", s, re.I)
        out["table"] = m.group(1).strip('"').split(".")[-1].upper() if m else ""
    elif first == "INSERT":
        m = re.match(r"\s*INSERT\s+INTO\s+([\w$#.\"]+)", s, re.I)
        out["table"] = m.group(1).strip('"').split(".")[-1].upper() if m else ""
    # 스키마 대조 (경고)
    tabs = {out["table"]} if out["table"] else set()
    alias: dict[str, str] = {}
    for m in re.finditer(r"\b(?:FROM|JOIN|UPDATE|INTO)\s+([\w$#.\"]+)(?:\s+(?:AS\s+)?([A-Za-z_]\w*))?", masked, re.I):
        t = m.group(1).strip('"').split(".")[-1].upper()
        tabs.add(t)
        if m.group(2) and m.group(2).upper() not in KEYWORDS:
            alias[m.group(2).upper()] = t
    cols: set[str] = set()
    for t in tabs:
        if t not in sch.tables:
            out["warnings"].append(f"스키마에 없는 테이블: {t}")
        else:
            cols |= {c.name for c in sch.tables[t].columns}
            if allowed and t not in allowed:
                out["warnings"].append(f"{t}: 식별된 대상/연관 테이블에 없습니다. 의도한 것인지 확인하세요.")
    known = tabs | set(alias) | cols | KEYWORDS
    bind = {b.upper() for b in re.findall(r":([A-Za-z_]\w*)", masked)}
    unknown = sorted({w.upper() for w in words if w.upper() not in known and w.upper() not in bind})
    if unknown:
        out["warnings"].append("스키마에서 확인되지 않은 식별자: " + ", ".join(unknown[:8]))
    if re.search(r":[A-Za-z_]\w*", masked):
        out["warnings"].append("바인드 변수(:이름)가 있습니다. 실행 전에 실제 값으로 바꾸세요.")
    out["sql"] = s + ";"
    return out


def derived_sql(v: dict, sr_id: int) -> list[dict]:
    """검증된 변경문에서 코드가 직접 만드는 확인용 SQL."""
    if v["kind"] not in ("update", "delete") or not v["table"] or not v["where"]:
        return []
    # 별칭이 있으면 그대로 유지해 WHERE 의 별칭 참조가 깨지지 않게 한다
    m = re.match(r"\s*(?:UPDATE|DELETE\s+FROM|DELETE)\s+[\w$#.\"]+(?:\s+(?:AS\s+)?([A-Za-z_]\w*))?", v["sql"], re.I)
    al = m.group(1) if m and m.group(1) and m.group(1).upper() not in KEYWORDS else ""
    frm = f"{v['table']} {al}".strip()
    w = v["where"].rstrip(";").strip()
    bak = f"{v['table'][:18]}_BAK_SR{sr_id}"
    return [
        {"kind": "count", "title": "① 영향 건수 확인 (SELECT)", "sql": f"SELECT COUNT(*)\n  FROM {frm}\n WHERE {w};", "warnings": []},
        {"kind": "preview", "title": "② 대상 행 미리보기 (SELECT, 최대 20건)",
         "sql": f"SELECT *\n  FROM {frm}\n WHERE {w}\n FETCH FIRST 20 ROWS ONLY;", "warnings": []},
        {"kind": "backup", "title": "③ 백업 권고 (DDL - 담당자 판단으로 실행)",
         "sql": f"CREATE TABLE {bak} AS\nSELECT * FROM {frm}\n WHERE {w};", "warnings": ["DDL 입니다. 운영 정책에 따라 실행 여부를 결정하세요."]},
    ]


# ---------------------------------------------------------------- 오케스트레이션
def analyze_data_sr(store: Store, llm: LLM, sr_id: int) -> str:
    sr = store.get_sr(sr_id)
    if not sr:
        raise ValueError(f"SR {sr_id} 없음")

    def fail(msg: str) -> str:
        store.audit(sr_id, "system", "data.error", {"error": msg})
        store.update_sr(sr_id, status="data_error", reason=msg[:300])
        return "data_error"

    proj = store.get_project(sr["project"]) if sr["project"] else None
    if not proj:
        return fail("대상 프로젝트를 지정하세요 (프로젝트 설정에서 DB 스키마 문서를 등록한 프로젝트).")
    store.update_sr(sr_id, status="data_analyzing")
    try:
        sch, diffs, source = load_project_schema(proj)
    except ValueError as e:
        return fail(str(e))
    except Exception as e:  # noqa: BLE001
        return fail(f"스키마 로드 실패: {type(e).__name__}: {e}")
    picked, plog = find_tables(store, llm, sch, sr["title"], sr["body"])
    store.audit(sr_id, "system", "data.tables_identified", {**plog, "picked": picked, "schema": source})
    if not picked:
        return fail("요청과 관련된 테이블을 스키마에서 찾지 못했습니다. 요청에 업무 용어(테이블/컬럼 설명에 있는 단어)를 더 적어 주세요.")
    names = [p["name"] for p in picked]
    related = sch.related(names)
    allowed = set(names) | {r["table"] for r in related}
    subset = "\n\n".join(
        f"[{n}] {sch.tables[n].comment}\n" + "\n".join(
            f"  {c.name} {c.type}{' PK' if c.pk else ''}{'' if c.nullable in (None, True) else ' NOT NULL'} {c.comment}" for c in sch.tables[n].columns)
        for n in names)
    user = f"[요청 제목]\n{sr['title']}\n\n[요청 내용]\n{sr['body']}\n\n[스키마]\n{subset}"
    stmts: list[dict] = []
    notes: list[str] = []
    try:
        raw = llm.complete(SQL_SYSTEM, user, max_tokens=1500, json_mode=True)
        store.audit(sr_id, "system", "data.sql_generated", {"model": getattr(llm, "model", "?"), "system": SQL_SYSTEM, "user": user, "response": raw})
        data = extract_json(raw)
        notes = [str(n)[:300] for n in data.get("notes", []) if isinstance(n, (str, int))][:8]
        for k, it in enumerate(data.get("statements", [])[:5], 1):
            if not isinstance(it, dict) or not str(it.get("sql", "")).strip():
                continue
            v = validate_sql(str(it["sql"]), sch, allowed)
            title = str(it.get("title", f"변경 안 {k}"))[:120]
            if v["blocked"]:
                stmts.append({"kind": "blocked", "title": title, "sql": str(it["sql"]).strip()[:2000],
                              "warnings": v["blocked"], "blocked": True})
                continue
            stmts.append({"kind": v["kind"], "title": f"변경 안: {title}", "sql": v["sql"], "warnings": v["warnings"], "blocked": False})
            stmts += derived_sql(v, sr_id)
    except Exception as e:  # noqa: BLE001 - SQL 생성 실패해도 테이블 식별 결과는 제공
        notes.append(f"SQL 초안을 만들지 못했습니다({type(e).__name__}). 식별된 테이블을 참고해 직접 작성하세요.")
    notes += ["이 시스템은 SQL 을 실행하지 않습니다. 담당자가 확인 후 직접 실행하고, COMMIT 은 영향 건수를 확인한 뒤 수행하세요.",
              "실행 전 대상 데이터 백업과 변경 이력 테이블 기록 규칙을 확인하세요."]
    table_view = [{**p, "columns_info": [{"name": c.name, "type": c.type, "comment": c.comment, "pk": c.pk}
                                         for c in sch.tables[p["name"]].columns]} for p in picked]
    store.save_data(sr_id, source, table_view, related, stmts, notes, diffs)
    store.audit(sr_id, "system", "data.analyzed", {"tables": names, "related": [r["table"] for r in related],
                                                   "statements": [(s["kind"], s["title"]) for s in stmts]})
    store.update_sr(sr_id, status="data_ready", reason="SQL 안 확인 대기")
    return "data_ready"


def report_executed(store: Store, sr_id: int, actor: str, affected: str, note: str) -> None:
    sr = store.get_sr(sr_id)
    if not sr or not store.get_data(sr_id):
        raise ValueError("데이터 분석 결과가 없습니다.")
    if not actor.strip():
        raise ValueError("담당자 이름이 필요합니다.")
    affected = affected.strip()
    if affected and not re.fullmatch(r"\d{1,12}", affected):
        raise ValueError("처리 건수는 숫자로 입력하세요.")
    store.audit(sr_id, actor.strip(), "data.executed_reported",
                {"affected_rows": affected, "note": note.strip(), "by_system": False,
                 "statements": [s["sql"] for s in store.get_data(sr_id)["statements"] if s["kind"] in ("update", "delete", "insert")]})
    store.update_sr(sr_id, status="data_done", reason=f"담당자 보고: 처리 {affected or '?'}건")
