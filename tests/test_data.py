import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from helpers import build_xlsx  # noqa: E402
from ssot.dataflow import analyze_data_sr, derived_sql, find_tables, report_executed, validate_sql  # noqa: E402
from ssot.llm import LLM  # noqa: E402
from ssot.oracle_meta import load_from_db  # noqa: E402
from ssot.schema import load_schema, merge, parse_ddl  # noqa: E402
from ssot.store import Store  # noqa: E402

DDL = """
-- 회원
CREATE TABLE ACME.TB_MEMBER (
  MEMBER_ID NUMBER(10) NOT NULL,
  STATUS_CD VARCHAR2(1) DEFAULT 'A' NOT NULL,
  WITHDRAW_DT DATE,
  NAME VARCHAR2(100 CHAR),
  CONSTRAINT PK_MEMBER PRIMARY KEY (MEMBER_ID)
);
CREATE TABLE TB_MEMBER_COUPON (ID NUMBER PRIMARY KEY, MEMBER_ID NUMBER NOT NULL REFERENCES TB_MEMBER(MEMBER_ID), COUPON_CD VARCHAR2(10));
CREATE TABLE TB_ORDER (ORDER_ID NUMBER, AMT NUMBER, CONSTRAINT FK_O FOREIGN KEY (ORDER_ID) REFERENCES TB_MEMBER (MEMBER_ID));
COMMENT ON TABLE TB_MEMBER IS '회원';
COMMENT ON COLUMN TB_MEMBER.STATUS_CD IS '회원 상태 (A 정상 / W 탈퇴); 주의';
COMMENT ON COLUMN TB_MEMBER.WITHDRAW_DT IS '탈퇴 일시';
COMMENT ON TABLE TB_ORDER IS '주문';
"""


@pytest.fixture
def sch():
    return parse_ddl(DDL)


def test_parse_ddl(sch):
    m = sch.tables["TB_MEMBER"]
    assert [c.name for c in m.columns] == ["MEMBER_ID", "STATUS_CD", "WITHDRAW_DT", "NAME"]   # DDL 순서 유지
    assert m.comment == "회원" and m.col("STATUS_CD").comment.endswith("주의") and m.col("MEMBER_ID").pk
    assert m.col("STATUS_CD").nullable is False and m.col("NAME").type == "VARCHAR2(100 CHAR)"
    assert sch.tables["TB_MEMBER_COUPON"].fks[0].ref_table == "TB_MEMBER"
    rel = {r["table"] for r in sch.related(["TB_MEMBER"])}
    assert rel == {"TB_MEMBER_COUPON", "TB_ORDER"}


def test_parse_markdown_flat_and_per_table(tmp_path):
    flat = tmp_path / "s.md"
    flat.write_text("""# 테이블 정의서

| 테이블명 | 컬럼명 | 데이터타입 | 길이 | PK | NULL여부 | 설명 | 참조 |
|---|---|---|---|---|---|---|---|
| TB_MEMBER | MEMBER_ID | NUMBER | 10 | Y | N | 회원번호 | |
| TB_MEMBER | STATUS_CD | VARCHAR2 | 1 | | N | 회원 상태 | |
| TB_COUPON | MEMBER_ID | NUMBER | 10 | | N | 회원번호 | TB_MEMBER.MEMBER_ID |
""", encoding="utf-8")
    s = load_schema(flat)
    assert s.tables["TB_MEMBER"].col("MEMBER_ID").pk and s.tables["TB_MEMBER"].col("STATUS_CD").type == "VARCHAR2(1)"
    assert s.tables["TB_MEMBER"].col("STATUS_CD").nullable is False
    assert s.tables["TB_COUPON"].fks[0].ref_table == "TB_MEMBER"
    per = tmp_path / "p.md"
    per.write_text("""### TB_MEMBER (회원)

| 컬럼명 | 타입 | 설명 |
|---|---|---|
| MEMBER_ID | NUMBER | 회원번호 |
| STATUS_CD | VARCHAR2 | 상태 |
""", encoding="utf-8")
    assert [c.name for c in load_schema(per).tables["TB_MEMBER"].columns] == ["MEMBER_ID", "STATUS_CD"]


def test_parse_xlsx_sheet_per_table(tmp_path):
    x = tmp_path / "s.xlsx"
    build_xlsx(x, "TB_MEMBER", [["컬럼명", "데이터타입", "설명"], ["MEMBER_ID", "NUMBER", "회원번호"], ["STATUS_CD", "VARCHAR2(1)", "상태"]])
    s = load_schema(x)
    assert s.tables["TB_MEMBER"].col("STATUS_CD").comment == "상태"


def test_bad_schema_doc_message(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("| a | b |\n|---|---|\n| 1 | 2 |\n", encoding="utf-8")
    with pytest.raises(ValueError, match="컬럼명"):
        load_schema(f)
    with pytest.raises(ValueError):
        load_schema(tmp_path / "nope.xlsx")


class FakeConn:
    """ALL_* 뷰 조회 결과를 흉내 내는 DB-API 가짜."""

    def cursor(self):
        return self

    def execute(self, sql, params):
        assert params == {"owner": "ACME"}
        self.sql = sql

    def fetchall(self):
        q = self.sql
        if "all_tab_columns" in q:
            return [("TB_MEMBER", "MEMBER_ID", "NUMBER", 22, 10, 0, "N"), ("TB_MEMBER", "STATUS_CD", "VARCHAR2", 1, None, None, "N"),
                    ("TB_MEMBER", "EXTRA_COL", "DATE", 7, None, None, "Y")]
        if "all_col_comments" in q:
            return [("TB_MEMBER", "STATUS_CD", "회원 상태")]
        if "all_tab_comments" in q:
            return [("TB_MEMBER", "회원")]
        if "'P'" in q:
            return [("TB_MEMBER", "MEMBER_ID")]
        return []


def test_oracle_metadata_reader_and_merge(monkeypatch, sch):
    monkeypatch.setenv("SSOT_DB_M_USER", "ro")
    db = load_from_db("M", connection=FakeConn()) if False else None
    monkeypatch.setenv("SSOT_DB_M_OWNER", "acme")
    db = load_from_db("M", connection=FakeConn())
    t = db.tables["TB_MEMBER"]
    assert t.col("MEMBER_ID").type == "NUMBER(10)" and t.col("MEMBER_ID").pk and t.col("STATUS_CD").type == "VARCHAR2(1)"
    assert t.col("EXTRA_COL").nullable is True
    merged, diffs = merge(db, sch)
    assert any("EXTRA_COL" in d and "DDL" in d for d in diffs)       # DB 에만 있는 컬럼
    assert any("TB_ORDER" in d for d in diffs)                          # 문서에만 있는 테이블
    assert "TB_ORDER" in merged.tables


# ---- SQL 검증 ----
ALLOWED = {"TB_MEMBER", "TB_MEMBER_COUPON"}


def v(sch, sql):
    return validate_sql(sql, sch, ALLOWED)


def test_validate_accepts_safe_update_and_derives_select(sch):
    r = v(sch, "UPDATE TB_MEMBER SET STATUS_CD = 'W' WHERE STATUS_CD = 'A' AND WITHDRAW_DT >= TO_DATE('2024-03-01','YYYY-MM-DD')")
    assert not r["blocked"] and not r["warnings"] and r["kind"] == "update" and r["table"] == "TB_MEMBER"
    d = derived_sql(r, 143)
    assert [x["kind"] for x in d] == ["count", "preview", "backup"]
    assert d[0]["sql"].startswith("SELECT COUNT(*)") and "STATUS_CD = 'A'" in d[0]["sql"]
    assert "FETCH FIRST 20 ROWS ONLY" in d[1]["sql"] and "TB_MEMBER_BAK_SR143" in d[2]["sql"]


@pytest.mark.parametrize("sql,reason", [
    ("UPDATE TB_MEMBER SET STATUS_CD = 'W'", "WHERE"),
    ("DELETE FROM TB_MEMBER", "WHERE"),
    ("UPDATE TB_MEMBER SET STATUS_CD='W' WHERE 1=1", "항상 참"),
    ("DROP TABLE TB_MEMBER", "허용되지 않는"),
    ("TRUNCATE TABLE TB_MEMBER", "허용되지 않는"),
    ("UPDATE TB_MEMBER SET STATUS_CD='W' WHERE MEMBER_ID=1; DELETE FROM TB_MEMBER WHERE MEMBER_ID=2", "여러 문장"),
    ("UPDATE TB_MEMBER SET STATUS_CD='W' WHERE MEMBER_ID=1 -- x", "주석"),
    ("UPDATE SYS.USER$ SET X=1 WHERE Y=2", "SYS"),
    ("SELECT * FROM TB_MEMBER", "UPDATE / DELETE / INSERT"),
    ("BEGIN NULL; END", "허용되지 않는"),
])
def test_validate_blocks_dangerous(sch, sql, reason):
    r = v(sch, sql)
    assert r["blocked"] and any(reason in b for b in r["blocked"]), r


def test_validate_strings_do_not_trigger_blocks_and_warns_on_unknown(sch):
    ok = v(sch, "UPDATE TB_MEMBER SET NAME = 'DROP; -- /* x */' WHERE MEMBER_ID = :id")
    assert not ok["blocked"] and any("바인드" in w for w in ok["warnings"])
    w = v(sch, "UPDATE TB_MEMBER SET NO_SUCH_COL = 1 WHERE MEMBER_ID = 3")["warnings"]
    assert any("NO_SUCH_COL" in x for x in w)
    w = v(sch, "UPDATE TB_NOPE SET A = 1 WHERE B = 2")["warnings"]
    assert any("스키마에 없는 테이블" in x for x in w)
    w = v(sch, "UPDATE TB_ORDER SET AMT = 0 WHERE ORDER_ID = 1")["warnings"]
    assert any("식별된" in x for x in w)


# ---- 전체 흐름 ----
class DataLLM(LLM):
    model = "fake"

    def __init__(self, pick=None, sql=None, fail=False):
        self.pick = pick if pick is not None else {"tables": [{"name": "TB_MEMBER", "columns": ["STATUS_CD", "WITHDRAW_DT", "NOPE"], "reason": "상태 컬럼"},
                                                             {"name": "TB_HALLUCINATED", "columns": [], "reason": "?"}]}
        self.sql = sql if sql is not None else {"statements": [
            {"title": "탈퇴 정정", "sql": "UPDATE TB_MEMBER SET STATUS_CD='W' WHERE STATUS_CD='A' AND WITHDRAW_DT >= TO_DATE('2024-03-01','YYYY-MM-DD')"},
            {"title": "위험", "sql": "UPDATE TB_MEMBER SET STATUS_CD='W'"}], "notes": ["쿠폰 확인"]}
        self.fail = fail

    def complete(self, system, user, max_tokens=2000, json_mode=False):
        if self.fail:
            raise RuntimeError("down")
        return json.dumps(self.pick if "고르는" in system else self.sql)


@pytest.fixture
def ready(tmp_path):
    doc = tmp_path / "schema.sql"
    doc.write_text(DDL, encoding="utf-8")
    s = Store(tmp_path / "d.db")
    s.upsert_project("acme/member", str(tmp_path), schema_doc=str(doc))
    sr = s.create_sr("탈퇴 회원 상태값 정정", "2024-03-01 장애로 탈퇴 처리됐지만 회원 상태가 A 로 남은 회원을 W 로 정정", "이요청", "acme/member")
    return s, sr


def test_analyze_data_flow(ready):
    s, sr = ready
    assert analyze_data_sr(s, DataLLM(), sr) == "data_ready"
    d = s.get_data(sr)
    t = d["tables"][0]
    assert t["name"] == "TB_MEMBER" and t["columns"] == ["STATUS_CD", "WITHDRAW_DT"]         # 환각 컬럼/테이블 제거
    assert [x["table"] for x in d["related"]] and {r["table"] for r in d["related"]} == {"TB_MEMBER_COUPON", "TB_ORDER"}
    kinds = [x["kind"] for x in d["statements"]]
    assert kinds == ["update", "count", "preview", "backup", "blocked"]                        # 위험한 문장은 blocked
    blocked = d["statements"][-1]
    assert blocked["blocked"] and any("WHERE" in w for w in blocked["warnings"])
    assert any("실행하지 않습니다" in n for n in d["notes"])
    ev = [a["event"] for a in s.audit_rows(sr)]
    assert "data.tables_identified" in ev and "data.sql_generated" in ev and "data.analyzed" in ev
    assert s.get_sr(sr)["status"] == "data_ready"


def test_analyze_falls_back_when_model_fails(ready):
    s, sr = ready
    assert analyze_data_sr(s, DataLLM(fail=True), sr) == "data_ready"
    d = s.get_data(sr)
    assert d["tables"][0]["by"] == "search" and d["tables"][0]["name"] == "TB_MEMBER"            # 검색 점수로 대체
    assert any("SQL 초안을 만들지 못했습니다" in n for n in d["notes"])


def test_errors_and_report(ready, tmp_path):
    s, sr = ready
    s2 = s.create_sr("t", "b", "r", "")
    assert analyze_data_sr(s, DataLLM(), s2) == "data_error"
    s.upsert_project("noschema", str(tmp_path))
    s3 = s.create_sr("t", "b", "r", "noschema")
    assert analyze_data_sr(s, DataLLM(), s3) == "data_error" and "스키마" in s.get_sr(s3)["reason"]
    analyze_data_sr(s, DataLLM(), sr)
    with pytest.raises(ValueError):
        report_executed(s, sr, "", "12", "")
    with pytest.raises(ValueError):
        report_executed(s, sr, "박담당", "열두건", "")
    report_executed(s, sr, "박담당", "12", "COMMIT 완료")
    last = s.audit_rows(sr)[-1]
    assert last["event"] == "data.executed_reported" and last["detail"]["by_system"] is False and last["detail"]["affected_rows"] == "12"
    assert s.get_sr(sr)["status"] == "data_done"


def test_find_tables_uses_korean_comments(ready):
    s, sr = ready
    from ssot.schema import load_schema
    sch = load_schema(s.get_project("acme/member")["schema_doc"])
    picked, log = find_tables(s, DataLLM(fail=True), sch, "주문 금액 정정", "주문 건의 금액을 수정")
    assert picked[0]["name"] == "TB_ORDER"


def test_web_data_flow(ready):
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from ssot.sr_web import App, make_handler
    s, sr = ready
    app = App(s, DataLLM(), sync_mode=True)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(path, data=None):
        req = urllib.request.Request(base + path, data=json.dumps(data).encode() if data is not None else None,
                                     headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    try:
        # 담당자가 수동으로 '데이터 수정' 분류 -> 분석 실행 -> 보고
        assert call(f"/api/sr/{sr}/data/analyze", {"actor": "김"})[0] == 400                     # 아직 분류 전
        call(f"/api/sr/{sr}/classify", {"actor": "김담당", "type": "data"})
        assert call(f"/api/sr/{sr}/data/analyze", {"actor": "김담당"})[0] == 200
        code, d = call(f"/api/sr/{sr}")
        assert d["sr"]["status"] == "data_ready" and d["data"]["tables"][0]["name"] == "TB_MEMBER"
        code, d = call(f"/api/sr/{sr}/data/report", {"actor": "박담당", "affected": "12"})
        assert code == 200 and d["sr"]["status"] == "data_done"
        # 프로젝트 저장 시 DB 접두어 검증
        assert call("/api/projects", {"name": "p2", "local_path": "/x", "db_env_prefix": "bad prefix!"})[0] == 400
        code, r = call("/api/projects", {"name": "p2", "local_path": "/x", "db_env_prefix": "MEMBER"})
        assert code == 200 and r["check"]["db_configured"] is False
    finally:
        srv.shutdown()
