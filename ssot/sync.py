"""소스 Fact 와 문서를 대조해 갱신하는 엔진.

- 표(컴포넌트 정의서·API 목록·설정표): 결정적(규칙 기반) 갱신. LLM 불필요.
- 서술(운영자 매뉴얼 본문): LLM 이 사실과 어긋난 문장만 골라 수정.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass

from .docs.base import Document, Table
from .facts import Fact, canon_stereotype, norm_class, norm_config, norm_path
from .llm import LLM, extract_json
from .tablemap import ALIASES, NUMBER_COLS, classify, norm_header, row_key

EXACT = {"stereotype", "package", "http_method", "path", "controller", "handler", "value"}
SOFT = {"methods", "fields", "params", "returns"}
TOKEN = re.compile(r"[A-Za-z_]\w*|\d+|[가-힣]+")


@dataclass
class Change:
    action: str        # update | fill | add | missing | review | narrative
    where: str
    key: str
    field: str = ""
    old: str = ""
    new: str = ""
    source: str = ""
    note: str = ""
    op: dict | None = None   # 문서에 다시 적용할 수 있는 편집 명령 (웹 UI 의 선택 적용용). 없으면 알림 전용


def apply_ops(doc: Document, ops: list[dict]) -> None:
    """기록된 편집 명령을 새로 연 문서에 재적용한다. 줄/행 번호가 밀리지 않도록 문단 -> 셀 -> 행 추가 순으로 처리."""
    tables = doc.tables()
    for op in (o for o in ops if o["type"] == "para"):
        doc.set_paragraph(op["index"], op["text"])
    for op in (o for o in ops if o["type"] == "cell"):
        tables[op["t"]].set_cell(op["row"], op["col"], op["text"])
    for op in (o for o in ops if o["type"] == "row"):
        tables[op["t"]].add_row(op["values"])


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _tokens(s: str) -> set[str]:
    return {t.lower() for t in TOKEN.findall(s)}


def _same(field: str, doc: str, src: str, strict: bool) -> bool:
    if field == "stereotype":
        return canon_stereotype(doc) == canon_stereotype(src)
    if field == "http_method":
        return _ws(doc).upper().replace("`", "") == src.upper()
    if field == "path":
        return _ws(doc).replace("`", "") == src or (doc.strip() and _ws(doc).split(" ")[-1] == src)
    if field in EXACT:
        return _ws(doc).casefold().replace("`", "") == _ws(src).casefold()
    st, dt = _tokens(src), _tokens(doc)
    return dt == st if strict else st <= dt


class _Ctx:
    def __init__(self, table: Table, kind: str, colmap: dict[str, int], ti: int):
        self.table, self.kind, self.colmap, self.ti = table, kind, colmap, ti
        self.rows = table.rows()
        self.index: dict[str, int] = {}
        for r, cells in enumerate(self.rows):
            k = row_key(kind, colmap, cells)
            if k and k not in self.index:
                self.index[k] = r
        self.groups: Counter = Counter()


def _cell_for(ctx: _Ctx, field: str, fact: Fact) -> str:
    if field == "path" and "http_method" not in ctx.colmap and ctx.kind == "endpoint":
        return f"{fact.get('http_method')} {fact.get('path')}"
    return fact.get(field)


def _update_row(ctx: _Ctx, r: int, fact: Fact, strict: bool) -> list[Change]:
    out: list[Change] = []
    cells = ctx.rows[r]
    where = f"{ctx.table.label} {r + 1}행"
    drift = False
    for fld, col in ctx.colmap.items():
        if fld in ("name", "key"):
            continue
        src = _cell_for(ctx, fld, fact)
        doc = cells[col]
        if not src:
            continue
        if fld == "summary":
            if not doc.strip():
                ctx.table.set_cell(r, col, src)
                out.append(Change("fill", where, fact.label, "설명", "", src, fact.source,
                                  op={"type": "cell", "t": ctx.ti, "row": r, "col": col, "text": src}))
            continue
        if fld == "value" and src == "****":
            continue
        if not _same(fld, doc, src, strict):
            ctx.table.set_cell(r, col, src)
            out.append(Change("update", where, fact.label, fld, doc, src, fact.source,
                              op={"type": "cell", "t": ctx.ti, "row": r, "col": col, "text": src}))
            drift = True
    if drift and "summary" in ctx.colmap and cells[ctx.colmap["summary"]].strip():
        out.append(Change("review", where, fact.label, "설명", note="소스와 달라 위 항목을 수정했습니다. 기존 설명 문구가 여전히 맞는지 확인하세요.",
                          source=fact.source))
    return out


def _new_row(ctx: _Ctx, fact: Fact, summary: str) -> list[str]:
    n = len(ctx.table.header)
    values = [""] * n
    for fld, col in ctx.colmap.items():
        values[col] = summary if fld == "summary" else _cell_for(ctx, fld, fact)
    for ci, h in enumerate(ctx.table.header):
        if norm_header(h) in NUMBER_COLS and not values[ci]:
            last = [r[ci] for r in ctx.rows if r[ci].strip().isdigit()]
            values[ci] = str(int(last[-1]) + 1) if last else str(len(ctx.rows) + 1)
    return values


def describe(llm: LLM | None, fact: Fact) -> str:
    if fact.get("summary"):
        return fact.get("summary")
    if not llm:
        return ""
    prompt = json.dumps({k: v for k, v in fact.fields.items() if v}, ensure_ascii=False)
    text = llm.complete(
        "당신은 기술 문서 작성자입니다. 주어진 소스 정보로 정의서에 들어갈 한 줄 설명(한국어, 40자 이내, 명사형 종결)만 출력하세요. 모르는 내용은 지어내지 마세요.",
        prompt, max_tokens=200,
    )
    return _ws(text)


def sync_tables(doc: Document, facts: list[Fact], aliases=ALIASES, llm: LLM | None = None,
                strict: bool = False) -> list[Change]:
    changes: list[Change] = []
    ctxs: list[_Ctx] = []
    for ti, t in enumerate(doc.tables()):
        c = classify(t.header, aliases)
        if c:
            ctxs.append(_Ctx(t, c[0], c[1], ti))
    by_kind: dict[str, list[Fact]] = {}
    for f in facts:
        by_kind.setdefault(f.kind, []).append(f)

    matched: set[tuple[str, str]] = set()
    for ctx in ctxs:
        for f in by_kind.get(ctx.kind, []):
            r = ctx.index.get(f.match)
            if r is None:
                continue
            matched.add((f.kind, f.match))
            ctx.groups[f.group] += 1
            changes += _update_row(ctx, r, f, strict)

    for kind in {c.kind for c in ctxs}:
        kctx = [c for c in ctxs if c.kind == kind]
        for f in by_kind.get(kind, []):
            if (kind, f.match) in matched:
                continue
            best = max(kctx, key=lambda c: c.groups[f.group])
            summary = describe(llm, f) if "summary" in best.colmap else ""
            values = _new_row(best, f, summary)
            best.table.add_row(values)
            best.rows.append(values)
            best.groups[f.group] += 1
            changes.append(Change("add", f"{best.table.label} (신규 행)", f.label, new=summary or "(설명 없음)",
                                  source=f.source,
                                  note="" if summary else "소스에 설명(Javadoc)이 없어 비워 두었습니다.",
                                  op={"type": "row", "t": best.ti, "values": values}))
        keys = {f.match for f in by_kind.get(kind, [])}
        for c in kctx:
            for k, r in c.index.items():
                if k not in keys:
                    changes.append(Change("missing", f"{c.table.label} {r + 1}행", k,
                                          note="소스에서 찾지 못했습니다. 삭제/개명 여부를 확인하세요."))
    return changes


# ---------------------------------------------------------------- 서술형 문서

NARRATIVE_SYSTEM = """당신은 기술 문서 교정자입니다. <문단> 하나가 <소스사실>과 모순되는지 검사합니다.
규칙:
- 문단에 적힌 설정값·숫자·경로·클래스명·파라미터명이 소스사실과 다르면 모순입니다. 하나씩 대조하세요.
- 문단 전체를 다시 쓰지 마세요. 틀린 부분만 {"wrong": 문단에 적힌 그대로의 틀린 표현, "right": 소스사실에 근거한 올바른 표현} 로 알려주세요.
- wrong 은 문단에 실제로 있는 글자 그대로여야 하고, right 에 쓰는 값은 소스사실에 있는 값이어야 합니다.
- 소스사실로 확인되지 않는 내용은 건드리지 않습니다.
- 응답은 JSON 한 개만:
  모순 없음: {"ok": true}
  모순 있음: {"ok": false, "fixes": [{"wrong": "9090", "right": "8080"}], "reason": "server.port 값이 8080이므로"}"""

_VALUE_TOKEN = re.compile(r"[A-Za-z0-9_./{}\-]+")


def _mentions(text: str, fact: Fact) -> bool:
    if fact.kind == "class":
        return re.search(rf"\b{re.escape(fact.get('name'))}\b", text) is not None
    if fact.kind == "endpoint":
        return norm_path(fact.get("path")).replace("{}", "") in norm_path(text.replace(" ", "")).replace("{}", "") \
            if len(fact.get("path")) > 1 else False
    return fact.match in re.sub(r"[`\s\-_]", "", text).lower()


def _apply_fixes(text: str, fixes: list, facts_text: str) -> tuple[str, str]:
    """모델이 제안한 (wrong -> right) 교체를 검증 후 적용. (새 문단, 거절 사유) 반환."""
    new = text
    for fx in fixes:
        wrong, right = str(fx.get("wrong", "")), str(fx.get("right", ""))
        if not wrong or not right or wrong == right:
            continue
        if wrong not in new:
            return text, f"'{wrong}' 가 문단에 없어 적용하지 않았습니다."
        tokens = _VALUE_TOKEN.findall(right)
        if not tokens:
            return text, f"'{right}' 는 소스 사실로 검증할 수 없어 적용하지 않았습니다."
        missing = [t for t in tokens if t not in facts_text]
        if missing:
            return text, f"'{right}' 의 {missing} 이(가) 소스 사실에 없어 적용하지 않았습니다."
        new = new.replace(wrong, right)
    return new, ""


def sync_narrative(doc: Document, facts: list[Fact], llm: LLM) -> list[Change]:
    """문단 하나씩 점검한다. 모델은 '틀린 값 -> 올바른 값'만 제안하고, 교체는 코드가 하며 올바른 값이 소스 사실에 있는지 검증한다."""
    changes: list[Change] = []
    title = "(제목 없음)"
    for p in doc.paragraphs():
        if p.heading:
            title = p.text
            continue
        hit = [f for f in facts if _mentions(p.text, f)]
        if not hit:
            continue
        facts_json = json.dumps([{"종류": f.kind, **{k: v for k, v in f.fields.items() if v}} for f in hit],
                                ensure_ascii=False, indent=1)
        short = p.text[:30] + ("…" if len(p.text) > 30 else "")
        where = f"섹션 '{title}'"
        prompt = f"<소스사실>\n{facts_json}\n</소스사실>\n\n<문단>\n{p.text}\n</문단>"
        try:
            res = extract_json(llm.complete(NARRATIVE_SYSTEM, prompt, max_tokens=500))
        except (ValueError, KeyError) as e:
            changes.append(Change("review", where, short, note=f"LLM 응답을 해석하지 못했습니다 ({type(e).__name__})."))
            continue
        fixes = res.get("fixes")
        if res.get("ok") is not False or not isinstance(fixes, list) or not fixes:
            continue
        new, why = _apply_fixes(p.text, [f for f in fixes if isinstance(f, dict)], facts_json)
        if why:
            changes.append(Change("review", where, short, note=f"LLM 제안 거절: {why}"))
            continue
        if new == p.text:
            continue
        doc.set_paragraph(p.index, new)
        changes.append(Change("narrative", where, short, "본문", p.text, new, note=str(res.get("reason", "")),
                              op={"type": "para", "index": p.index, "text": new}))
    return changes
