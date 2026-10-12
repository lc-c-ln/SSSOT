"""Java 소스 구조 분석기 (표준 라이브러리 전용).

주석/문자열을 마스킹한 사본으로 괄호 구조를 추적하고, 값은 원본에서 읽는다.
Spring 어노테이션(컨트롤러 매핑, 스테레오타입)을 해석하는 데 필요한 수준만 지원한다.
tree-sitter 기반 분석기로 교체할 때는 `parse_java()`와 같은 시그니처만 맞추면 된다.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

MODIFIERS = {
    "public", "protected", "private", "abstract", "final", "static", "sealed",
    "non-sealed", "strictfp", "default", "synchronized", "native", "transient", "volatile",
}
TYPE_RE = re.compile(
    r"^(?:(?:public|protected|private|abstract|final|static|sealed|non-sealed|strictfp)\s+)*"
    r"(class|interface|enum|record|@interface)\s+(\w+)"
)


@dataclass
class Annotation:
    name: str
    args: str = ""


@dataclass
class Param:
    name: str
    type: str
    annotations: list[Annotation] = field(default_factory=list)


@dataclass
class MethodInfo:
    name: str
    return_type: str
    params: list[Param]
    modifiers: list[str]
    annotations: list[Annotation]
    summary: str
    line: int


@dataclass
class FieldInfo:
    name: str
    type: str
    modifiers: list[str]
    annotations: list[Annotation]
    summary: str
    line: int


@dataclass
class ClassInfo:
    name: str
    package: str
    kind: str
    annotations: list[Annotation]
    extends: str
    implements: list[str]
    summary: str
    fields: list[FieldInfo]
    methods: list[MethodInfo]
    line: int
    file: str

    @property
    def qualified(self) -> str:
        return f"{self.package}.{self.name}" if self.package else self.name


# ---------------------------------------------------------------- 저수준 스캐너

def mask(src: str) -> tuple[str, list[tuple[int, int, str]]]:
    """주석·문자열 내용을 공백으로 지운 사본과 javadoc 목록(start, end, text)을 돌려준다."""
    n = len(src)
    out = list(src)
    javadocs: list[tuple[int, int, str]] = []

    def blank(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] not in "\r\n":
                out[k] = " "

    i = 0
    while i < n:
        c = src[i]
        two = src[i:i + 2]
        if two == "//":
            j = src.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif two == "/*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            if src.startswith("/**", i) and j - i > 4:
                javadocs.append((i, j, src[i:j]))
            blank(i, j)
            i = j
        elif src.startswith('"""', i):
            j = src.find('"""', i + 3)
            j = n if j < 0 else j + 3
            blank(i + 3, max(i + 3, j - 3))
            i = j
        elif c in "\"'":
            j = i + 1
            while j < n and src[j] != c and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            blank(i + 1, j)
            i = j + 1
        else:
            i += 1
    return "".join(out), javadocs


def match_close(m: str, i: int, open_: str, close: str) -> int:
    depth = 0
    for k in range(i, len(m)):
        if m[k] == open_:
            depth += 1
        elif m[k] == close:
            depth -= 1
            if depth == 0:
                return k
    return len(m) - 1


def split_members(m: str, start: int, end: int) -> list[tuple[int, int]]:
    """본문을 멤버(필드/메서드/중첩 타입) 단위 구간으로 나눈다."""
    chunks: list[tuple[int, int]] = []
    i, cs, paren, seen_eq = start, start, 0, False
    while i < end:
        c = m[i]
        if c == "(":
            paren += 1
        elif c == ")":
            paren -= 1
        elif paren == 0 and c == "=":
            seen_eq = True
        elif paren == 0 and c == "{":
            k = match_close(m, i, "{", "}")
            if seen_eq:  # 필드 초기화 블록
                i = k + 1
                continue
            chunks.append((cs, k + 1))
            cs, i, seen_eq = k + 1, k + 1, False
            continue
        elif paren == 0 and c == ";":
            chunks.append((cs, i + 1))
            cs, seen_eq = i + 1, False
        i += 1
    return chunks


def parse_annotations(m: str, o: str, i: int = 0) -> tuple[list[Annotation], int]:
    anns: list[Annotation] = []
    n = len(m)
    while True:
        while i < n and m[i].isspace():
            i += 1
        if i < n and m[i] == "@" and not m.startswith("@interface", i):
            mt = re.compile(r"@\s*([\w.]+)").match(m, i)
            if not mt:
                break
            name = mt.group(1).split(".")[-1]
            i = mt.end()
            j = i
            while j < n and m[j].isspace():
                j += 1
            args = ""
            if j < n and m[j] == "(":
                k = match_close(m, j, "(", ")")
                args = o[j + 1:k]
                i = k + 1
            anns.append(Annotation(name, args))
        else:
            break
    return anns, i


def split_top(m: str, o: str, sep: str = ",") -> list[str]:
    """깊이 0에서만 sep로 나눈다. (<>, (), [], {} 추적)"""
    parts, depth, last = [], 0, 0
    for i, c in enumerate(m):
        if c in "<([{":
            depth += 1
        elif c in ">)]}":
            depth -= 1
        elif c == sep and depth == 0:
            parts.append(o[last:i])
            last = i + 1
    parts.append(o[last:])
    return [p.strip() for p in parts if p.strip()]


def javadoc_summary(raw: str) -> str:
    body = raw.strip()
    body = re.sub(r"^/\*\*+", "", body)
    body = re.sub(r"\*+/$", "", body)
    lines = [re.sub(r"^\s*\*+ ?", "", ln).rstrip() for ln in body.splitlines()]
    text = "\n".join(lines)
    text = re.split(r"(?m)^\s*@\w+", text, maxsplit=1)[0]
    text = re.sub(r"\{@(?:code|link|literal)\s+([^}]*)\}", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    first = next((p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()), "")
    return re.sub(r"\s+", " ", first).strip()


# ---------------------------------------------------------------- 파서

class _Ctx:
    def __init__(self, src: str, path: str):
        self.o = src
        self.m, self.javadocs = mask(src)
        self.path = path
        self.nl = [i for i, ch in enumerate(src) if ch == "\n"]
        pm = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", self.m)
        self.package = pm.group(1) if pm else ""
        self.classes: list[ClassInfo] = []

    def line(self, off: int) -> int:
        return bisect.bisect_left(self.nl, off) + 1

    def doc_between(self, a: int, b: int) -> str:
        found = ""
        for js, je, text in self.javadocs:
            if js >= a and je <= b:
                found = javadoc_summary(text)
        return found


def parse_java(src: str, path: str = "") -> list[ClassInfo]:
    ctx = _Ctx(src, path)
    for a, b in split_members(ctx.m, 0, len(ctx.m)):
        _parse_chunk(ctx, a, b, owner=None, prefix="")
    return ctx.classes


def _parse_chunk(ctx: _Ctx, a: int, b: int, owner: ClassInfo | None, prefix: str) -> None:
    m, o = ctx.m[a:b], ctx.o[a:b]
    anns, i = parse_annotations(m, o)
    doc = ctx.doc_between(a, a + i)
    rest_m, rest_o = m[i:], o[i:]
    lead = len(rest_m) - len(rest_m.lstrip())
    rest_m, rest_o = rest_m[lead:], rest_o[lead:]
    base = a + i + lead
    tm = TYPE_RE.match(rest_m)
    if tm:
        _parse_type(ctx, tm, rest_m, rest_o, base, anns, doc, prefix)
        return
    if owner is None:
        return
    if not doc:
        doc = ctx.doc_between(a, base)
    _parse_member(ctx, owner, rest_m, rest_o, base, anns, doc)


def _parse_type(ctx, tm, rest_m, rest_o, base, anns, doc, prefix) -> None:
    kind, name = tm.group(1), tm.group(2)
    brace = rest_m.find("{")
    header_m = rest_m[tm.end():brace if brace >= 0 else len(rest_m)]
    header_o = rest_o[tm.end():brace if brace >= 0 else len(rest_o)]
    ext = re.search(r"\bextends\s+(.+?)(?=\bimplements\b|\bpermits\b|$)", header_o, re.S)
    imp = re.search(r"\bimplements\s+(.+?)(?=\bpermits\b|$)", header_o, re.S)
    extends = re.sub(r"\s+", " ", ext.group(1)).strip() if ext else ""
    implements = split_top(re.sub(r"\s+", " ", imp.group(1)), re.sub(r"\s+", " ", imp.group(1))) if imp else []
    full = f"{prefix}{name}"
    info = ClassInfo(full, ctx.package, kind, anns, extends, implements, doc, [], [],
                     ctx.line(base), ctx.path)
    ctx.classes.append(info)
    if brace < 0:
        return
    end = match_close(rest_m, brace, "{", "}")
    body_start = base + brace + 1
    body_end = base + end
    if kind == "record":  # record 구성요소를 필드로
        pm = re.search(r"\(", header_m)
        if pm:
            q = match_close(header_m, pm.start(), "(", ")")
            for p in split_top(header_m[pm.start() + 1:q], header_o[pm.start() + 1:q]):
                par = _parse_param(p)
                if par:
                    info.fields.append(FieldInfo(par.name, par.type, ["private", "final"], par.annotations, "", info.line))
    if kind == "enum":  # 상수 목록 건너뛰기
        depth, k = 0, body_start
        while k < body_end:
            c = ctx.m[k]
            if c in "({":
                depth += 1
            elif c in ")}":
                depth -= 1
            elif c == ";" and depth == 0:
                break
            k += 1
        body_start = k + 1
    for ca, cb in split_members(ctx.m, body_start, body_end):
        _parse_chunk(ctx, ca, cb, owner=info, prefix=f"{full}.")


def _parse_param(text: str) -> Param | None:
    mm = re.sub(r"\s+", " ", text)
    anns, i = parse_annotations(mm, mm)
    rest = re.sub(r"\bfinal\s+", "", mm[i:]).strip()
    pm = re.match(r"^(.*\S)\s+(\w+)$", rest)
    return Param(pm.group(2), pm.group(1), anns) if pm else None


def _strip_modifiers(text: str) -> tuple[str, list[str]]:
    mods: list[str] = []
    while True:
        mt = re.match(r"^\s*([\w-]+)\s+", text)
        if mt and mt.group(1) in MODIFIERS:
            mods.append(mt.group(1))
            text = text[mt.end():]
        else:
            return text.strip(), mods


def _parse_member(ctx, owner: ClassInfo, rest_m, rest_o, base, anns, doc) -> None:
    stop = len(rest_m)
    paren = 0
    for k, c in enumerate(rest_m):
        if c == "(":
            paren += 1
        elif c == ")":
            paren -= 1
        elif paren == 0 and c in "{;":
            stop = k
            break
    sig_m, sig_o = rest_m[:stop], rest_o[:stop]
    eq = sig_m.find("=")
    p = sig_m.find("(")
    line = ctx.line(base)
    if p >= 0 and (eq < 0 or p < eq):  # 메서드/생성자
        before = sig_o[:p].strip()
        bm = sig_m[:p]
        if before.startswith("<"):
            before = before[match_close(bm, bm.index("<"), "<", ">") + 1:].strip()
        mt = re.match(r"^(.*?)(\w+)\s*$", before, re.S)
        if not mt:
            return
        pre, name = mt.group(1), mt.group(2)
        ret, mods = _strip_modifiers(pre)
        if not ret:
            return  # 생성자
        q = match_close(sig_m, p, "(", ")")
        params = [pr for pr in (_parse_param(t) for t in split_top(sig_m[p + 1:q], sig_o[p + 1:q])) if pr]
        if owner.kind == "interface" and "private" not in mods:
            mods = mods or ["public"]
        owner.methods.append(MethodInfo(name, re.sub(r"\s+", " ", ret), params, mods, anns, doc, line))
        return
    decl = (sig_o.split("=")[0] if eq >= 0 else sig_o).strip()
    decl, mods = _strip_modifiers(decl)
    fm = re.match(r"^(.*\S)\s+(\w+)\s*(?:\[\s*\]\s*)*$", decl, re.S)
    if fm:
        owner.fields.append(FieldInfo(fm.group(2), re.sub(r"\s+", " ", fm.group(1)), mods, anns, doc, line))
