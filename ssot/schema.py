"""DB 스키마 모델 + 파서. 별도 제공되는 스키마 문서(md/docx/xlsx 표, .sql DDL)와 실 DB 메타데이터(선택)를 같은 모델로 다룬다."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .docs import open_document
from .tablemap import norm_header


@dataclass
class Column:
    name: str
    type: str = ""
    nullable: bool | None = None
    pk: bool = False
    comment: str = ""


@dataclass
class FK:
    cols: list[str]
    ref_table: str
    ref_cols: list[str]


@dataclass
class Table:
    name: str
    comment: str = ""
    columns: list[Column] = field(default_factory=list)
    fks: list[FK] = field(default_factory=list)
    source: str = ""

    def col(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name.upper()), None)


class Schema:
    def __init__(self, source: str = ""):
        self.tables: dict[str, Table] = {}
        self.source = source

    def table(self, name: str, source: str = "") -> Table:
        key = _id(name)
        if key not in self.tables:
            self.tables[key] = Table(key, source=source)
        return self.tables[key]

    def related(self, names: list[str]) -> list[dict]:
        """선택 테이블과 FK 로 연결된 테이블(양방향)."""
        want = {n.upper() for n in names}
        out: dict[str, dict] = {}
        for t in self.tables.values():
            for fk in t.fks:
                if t.name in want and fk.ref_table in self.tables and fk.ref_table not in want:
                    out.setdefault(fk.ref_table, {"table": fk.ref_table, "via": []})["via"].append(
                        f"{t.name}.{','.join(fk.cols)} → {fk.ref_table}.{','.join(fk.ref_cols)} (참조 대상)")
                if fk.ref_table in want and t.name not in want:
                    out.setdefault(t.name, {"table": t.name, "via": []})["via"].append(
                        f"{t.name}.{','.join(fk.cols)} → {fk.ref_table}.{','.join(fk.ref_cols)} (이 테이블을 참조)")
        return list(out.values())


def _id(s: str) -> str:
    s = s.strip().strip('"`[]')
    s = s.split(".")[-1].strip('"`[]')            # OWNER.TABLE -> TABLE
    return s.upper()


# ---------------------------------------------------------------- DDL
def _strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"--[^\n]*", " ", sql)


def _split_top(s: str, sep: str = ",") -> list[str]:
    out, depth, cur, q = [], 0, [], ""
    for ch in s:
        if q:
            cur.append(ch)
            q = "" if ch == q else q
            continue
        if ch in "'\"":
            q = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == sep and depth == 0:
            out.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur).strip())
    return out


def _cols(s: str) -> list[str]:
    return [_id(c) for c in s.split(",") if c.strip()]


def _unq(s: str) -> str:
    return s.replace("''", "'")


IDENT = r'(?:"[^"]+"|[A-Za-z_][\w$#]*)(?:\s*\.\s*(?:"[^"]+"|[A-Za-z_][\w$#]*))?'


def parse_ddl(text: str, source: str = "DDL") -> Schema:
    sch = Schema(source)
    body = re.sub(r"COMMENT\s+ON\s+(?:TABLE|COLUMN)\s+.*?IS\s+'(?:[^']|'')*'", " ", text, flags=re.I | re.S)
    body = _strip_comments(body)
    for stmt in body.split(";"):
        m = re.match(rf"\s*CREATE\s+(?:GLOBAL\s+TEMPORARY\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?({IDENT})\s*\((.*)\)[^)]*$", stmt, re.I | re.S)
        if m:
            t = sch.table(m.group(1), source)
            pk_cols: list[str] = []
            for item in _split_top(m.group(2)):
                u = item.upper().lstrip()
                if re.match(r"(CONSTRAINT\s+\S+\s+)?PRIMARY\s+KEY", u):
                    pk_cols += _cols(re.search(r"\(([^)]*)\)", item).group(1))
                elif re.match(r"(CONSTRAINT\s+\S+\s+)?FOREIGN\s+KEY", u):
                    f = re.search(rf"FOREIGN\s+KEY\s*\(([^)]*)\)\s*REFERENCES\s+({IDENT})\s*\(([^)]*)\)", item, re.I)
                    if f:
                        t.fks.append(FK(_cols(f.group(1)), _id(f.group(2)), _cols(f.group(3))))
                elif re.match(r"(CONSTRAINT|UNIQUE|CHECK|INDEX|KEY)\b", u):
                    continue
                else:
                    cm = re.match(rf'\s*({IDENT})\s+([A-Za-z_][\w ]*?(?:\([^)]*\))?(?:\s+(?:WITH(?:OUT)?\s+(?:LOCAL\s+)?TIME\s+ZONE))?)(?=\s|,|$)(.*)$', item, re.S)
                    if not cm:
                        continue
                    rest = cm.group(3)
                    col = Column(_id(cm.group(1)), re.sub(r"\s+", " ", cm.group(2)).strip().upper(),
                                 None if not re.search(r"NOT\s+NULL|\bNULL\b", rest, re.I) else not re.search(r"NOT\s+NULL", rest, re.I),
                                 bool(re.search(r"PRIMARY\s+KEY", rest, re.I)))
                    ref = re.search(rf"REFERENCES\s+({IDENT})\s*\(([^)]*)\)", rest, re.I)
                    if ref:
                        t.fks.append(FK([col.name], _id(ref.group(1)), _cols(ref.group(2))))
                    existing = t.col(col.name)
                    if existing:
                        existing.type, existing.nullable, existing.pk = col.type, col.nullable, col.pk
                    else:
                        t.columns.append(col)
            for c in pk_cols:
                col = t.col(c)
                if col:
                    col.pk = True
            continue
        a = re.match(rf"\s*ALTER\s+TABLE\s+({IDENT})\s+ADD\s+(?:CONSTRAINT\s+\S+\s+)?(PRIMARY\s+KEY|FOREIGN\s+KEY)\s*\(([^)]*)\)(.*)$", stmt, re.I | re.S)
        if a:
            t = sch.table(a.group(1), source)
            if a.group(2).upper().startswith("PRIMARY"):
                for c in _cols(a.group(3)):
                    col = t.col(c)
                    if col:
                        col.pk = True
            else:
                r = re.search(rf"REFERENCES\s+({IDENT})\s*\(([^)]*)\)", a.group(4), re.I)
                if r:
                    t.fks.append(FK(_cols(a.group(3)), _id(r.group(1)), _cols(r.group(2))))
    # 주석은 테이블/컬럼 정의 뒤에 붙여야 컬럼 순서가 DDL 그대로 유지된다
    for m in re.finditer(rf"COMMENT\s+ON\s+TABLE\s+({IDENT})\s+IS\s+'((?:[^']|'')*)'", text, re.I):
        sch.table(m.group(1), source).comment = _unq(m.group(2))
    for m in re.finditer(rf"COMMENT\s+ON\s+COLUMN\s+({IDENT})\s+IS\s+'((?:[^']|'')*)'", text, re.I):
        full = re.sub(r"\s+", "", m.group(1))
        tbl, _, col = full.rpartition(".")
        t = sch.table(tbl, source)
        c = t.col(_id(col))
        if c:
            c.comment = _unq(m.group(2))
        else:
            t.columns.append(Column(_id(col), comment=_unq(m.group(2))))
    return sch


# ---------------------------------------------------------------- 문서 표
SYN = {
    "table": ["테이블명", "테이블", "테이블물리명", "물리테이블명", "테이블id", "table", "tablename", "tablenm", "엔티티명", "엔티티", "entity"],
    "tcomment": ["테이블설명", "테이블논리명", "테이블한글명", "테이블comment", "tablecomment", "테이블명한글"],
    "col": ["컬럼명", "컬럼", "컬럼물리명", "물리컬럼명", "컬럼id", "column", "columnname", "columnnm", "필드명", "필드", "속성명(물리)", "속성물리명"],
    "comment": ["컬럼설명", "컬럼논리명", "컬럼한글명", "논리명", "설명", "한글명", "코멘트", "comment", "description", "비고", "속성명", "속성설명", "항목명"],
    "type": ["데이터타입", "데이터유형", "타입", "자료형", "type", "datatype", "dtype"],
    "length": ["길이", "크기", "length", "size", "자릿수"],
    "pk": ["pk", "기본키", "primarykey", "키"],
    "null": ["null", "null여부", "nullable", "널", "널여부", "필수", "필수여부", "notnull"],
    "fk": ["fk", "외래키", "참조", "참조테이블", "참조컬럼", "references", "fk정보", "참조정보"],
}


def _hmap(header: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    norm = [norm_header(h) for h in header]
    for key, names in SYN.items():
        wanted = {norm_header(n) for n in names}
        for i, h in enumerate(norm):
            if h in wanted and i not in out.values():
                out[key] = i
                break
    return out


def _truthy(v: str) -> bool:
    return v.strip().upper() in ("Y", "YES", "O", "○", "●", "V", "✓", "TRUE", "1", "PK", "P", "X")


def parse_doc(path: Path) -> Schema:
    sch = Schema(f"문서:{path.name}")
    doc = open_document(path)
    for t in doc.tables():
        hm = _hmap(t.header)
        if "col" not in hm:
            continue
        label_name = re.sub(r"\s표$", "", re.sub(r"^시트\s*'(.*)'$", r"\1", t.label or "")).strip()
        lm = re.search(r"[A-Za-z][A-Za-z0-9_$#]*", label_name)
        label_name = lm.group() if lm else label_name
        cur = ""
        for row in t.rows():
            cell = lambda k: row[hm[k]].strip() if k in hm and hm[k] < len(row) else ""
            tname = cell("table") or cur or label_name
            if not tname or not cell("col"):
                continue
            tn = re.split(r"[\s(（]", tname.strip())[0] if re.match(r"^[A-Za-z_]", tname.strip()) else tname.strip()
            cur = cell("table") or cur
            tbl = sch.table(tn, sch.source)
            if cell("tcomment") and not tbl.comment:
                tbl.comment = cell("tcomment")
            typ = cell("type")
            if cell("length") and typ and "(" not in typ:
                typ = f"{typ}({cell('length')})"
            nul = cell("null")
            col = Column(_id(cell("col")), typ.upper(),
                         None if not nul else (not _truthy(nul) if "필수" in " ".join(t.header) or "notnull" in norm_header(t.header[hm["null"]]) else _truthy(nul) or nul.upper() in ("NULL", "NULLABLE")),
                         _truthy(cell("pk")), cell("comment"))
            tbl.columns.append(col)
            fk = cell("fk")
            m = re.match(rf"^\(?\s*({IDENT})\s*[.(]\s*({IDENT})\s*\)?", fk) if fk and not _truthy(fk) else None
            if m:
                tbl.fks.append(FK([col.name], _id(m.group(1)), [_id(m.group(2))]))
    return sch


def load_schema(path: str | Path) -> Schema:
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"스키마 문서를 찾을 수 없습니다: {p}")
    if p.suffix.lower() in (".sql", ".ddl", ".txt"):
        sch = parse_ddl(p.read_text(encoding="utf-8", errors="replace"), f"DDL:{p.name}")
    else:
        sch = parse_doc(p)
    if not sch.tables:
        raise ValueError("스키마 문서에서 테이블/컬럼 정보를 찾지 못했습니다. 헤더에 '테이블명', '컬럼명' 열이 있는지 확인하세요.")
    return sch


def merge(primary: Schema, secondary: Schema) -> tuple[Schema, list[str]]:
    """primary 를 기준으로 secondary 를 합치고, 서로 다른 점을 리포트한다."""
    out = Schema(f"{primary.source} + {secondary.source}")
    diffs: list[str] = []
    for name, t in primary.tables.items():
        out.tables[name] = t
        o = secondary.tables.get(name)
        if not o:
            diffs.append(f"{name}: {secondary.source} 에 없음")
            continue
        for c in t.columns:
            oc = o.col(c.name)
            if not oc:
                diffs.append(f"{name}.{c.name}: {secondary.source} 에 없음")
            elif c.type and oc.type and c.type.split("(")[0] != oc.type.split("(")[0]:
                diffs.append(f"{name}.{c.name}: 타입 불일치 {c.type} ≠ {oc.type}")
            if oc and not c.comment and oc.comment:
                c.comment = oc.comment
        for oc in o.columns:
            if not t.col(oc.name):
                diffs.append(f"{name}.{oc.name}: {primary.source} 에 없음")
        if not t.comment:
            t.comment = o.comment
        if not t.fks:
            t.fks = o.fks
    for name, o in secondary.tables.items():
        if name not in out.tables:
            out.tables[name] = o
            diffs.append(f"{name}: {primary.source} 에 없음")
    return out, diffs
