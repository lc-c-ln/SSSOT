"""실제 Oracle(RDS) 메타데이터 조회 (선택 기능, 기본 꺼짐).

- 딕셔너리 뷰(ALL_TAB_COLUMNS 등)만 읽는다. 업무 테이블의 데이터 행은 읽지 않는다. 임의 SQL 을 DB 로 보내는 경로 없음.
- 접속 정보는 환경변수에서만 읽는다: SSOT_DB_<접두어>_USER / _PASSWORD / _DSN (예: host:1521/ORCL) / _OWNER(선택, 기본 USER)
- python-oracledb(Thin 모드) 가 설치된 경우에만 동작한다. 읽기 전용 계정(SELECT_CATALOG_ROLE 또는 대상 스키마 SELECT)을 사용하세요.
"""
from __future__ import annotations

import os

from .schema import FK, Column, Schema

COLS_SQL = ("SELECT c.table_name, c.column_name, c.data_type, c.data_length, c.data_precision, c.data_scale, c.nullable "
            "FROM all_tab_columns c WHERE c.owner = :owner ORDER BY c.table_name, c.column_id")
CCOMMENT_SQL = "SELECT table_name, column_name, comments FROM all_col_comments WHERE owner = :owner AND comments IS NOT NULL"
TCOMMENT_SQL = "SELECT table_name, comments FROM all_tab_comments WHERE owner = :owner AND comments IS NOT NULL"
PK_SQL = ("SELECT cc.table_name, cc.column_name FROM all_constraints k JOIN all_cons_columns cc "
          "ON k.owner = cc.owner AND k.constraint_name = cc.constraint_name "
          "WHERE k.owner = :owner AND k.constraint_type = 'P'")
FK_SQL = ("SELECT k.table_name, k.constraint_name, cc.column_name, cc.position, r.table_name, rc.column_name "
          "FROM all_constraints k JOIN all_cons_columns cc ON k.owner = cc.owner AND k.constraint_name = cc.constraint_name "
          "JOIN all_constraints r ON k.r_owner = r.owner AND k.r_constraint_name = r.constraint_name "
          "JOIN all_cons_columns rc ON r.owner = rc.owner AND r.constraint_name = rc.constraint_name AND rc.position = cc.position "
          "WHERE k.owner = :owner AND k.constraint_type = 'R' ORDER BY k.table_name, k.constraint_name, cc.position")


def env_names(prefix: str) -> dict[str, str]:
    p = f"SSOT_DB_{prefix.upper()}_"
    return {k: p + k for k in ("USER", "PASSWORD", "DSN", "OWNER")}


def configured(prefix: str) -> bool:
    n = env_names(prefix)
    return bool(prefix and all(os.environ.get(n[k]) for k in ("USER", "PASSWORD", "DSN")))


def connect(prefix: str):
    try:
        import oracledb  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError("python-oracledb 가 설치되어 있지 않습니다 (pip install oracledb). 문서 모드로 동작합니다.") from e
    n = env_names(prefix)
    return oracledb.connect(user=os.environ[n["USER"]], password=os.environ[n["PASSWORD"]], dsn=os.environ[n["DSN"]])


def load_from_db(prefix: str, connection=None) -> Schema:
    """connection 을 주입할 수 있다(테스트용 DB-API 호환 객체)."""
    n = env_names(prefix)
    owner = (os.environ.get(n["OWNER"]) or os.environ.get(n["USER"], "")).upper()
    conn = connection or connect(prefix)
    sch = Schema(f"DB:{owner}")
    try:
        cur = conn.cursor()

        def rows(sql: str):
            cur.execute(sql, {"owner": owner})
            return cur.fetchall()

        for tn, cn, dt, dl, dp, ds, nl in rows(COLS_SQL):
            t = sch.table(tn, sch.source)
            if dt == "NUMBER" and dp:
                typ = f"NUMBER({dp}{',' + str(ds) if ds else ''})"
            elif dt in ("VARCHAR2", "CHAR", "NVARCHAR2", "NCHAR", "RAW"):
                typ = f"{dt}({dl})"
            else:
                typ = dt
            t.columns.append(Column(cn.upper(), typ, nl == "Y", False, ""))
        for tn, cn, cm in rows(CCOMMENT_SQL):
            t = sch.tables.get(tn.upper())
            c = t.col(cn) if t else None
            if c:
                c.comment = cm
        for tn, cm in rows(TCOMMENT_SQL):
            if tn.upper() in sch.tables:
                sch.tables[tn.upper()].comment = cm
        for tn, cn in rows(PK_SQL):
            t = sch.tables.get(tn.upper())
            c = t.col(cn) if t else None
            if c:
                c.pk = True
        grouped: dict[tuple[str, str], FK] = {}
        for tn, cname, col, _pos, rt, rcol in rows(FK_SQL):
            fk = grouped.setdefault((tn.upper(), cname), FK([], rt.upper(), []))
            fk.cols.append(col.upper())
            fk.ref_cols.append(rcol.upper())
        for (tn, _), fk in grouped.items():
            if tn in sch.tables:
                sch.tables[tn].fks.append(fk)
    finally:
        if connection is None:
            conn.close()
    return sch
