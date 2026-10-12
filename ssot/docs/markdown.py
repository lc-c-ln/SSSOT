"""Markdown 어댑터: 파이프 표와 일반 줄 단위 문단."""
from __future__ import annotations

import re
from pathlib import Path

from .base import Document, Para, Table

_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _split_row(line: str) -> list[str]:
    s = line.strip()
    s = s[1:] if s.startswith("|") else s
    s = s[:-1] if s.endswith("|") and not s.endswith("\\|") else s
    cells = re.split(r"(?<!\\)\|", s)
    return [c.strip().replace("\\|", "|").replace("<br>", "\n") for c in cells]


def _render_row(cells: list[str], indent: str = "") -> str:
    esc = [c.replace("|", "\\|").replace("\r", "").replace("\n", "<br>") for c in cells]
    return indent + "| " + " | ".join(esc) + " |"


class MarkdownTable(Table):
    def __init__(self, doc: "MarkdownDocument", start: int, end: int, label: str):
        self.doc, self.start, self.end, self.label = doc, start, end, label  # end: 마지막 행 줄 번호(포함)

    @property
    def header(self) -> list[str]:
        return _split_row(self.doc.lines[self.start])

    def rows(self) -> list[list[str]]:
        n = len(self.header)
        out = []
        for i in range(self.start + 2, self.end + 1):
            cells = _split_row(self.doc.lines[i])
            out.append((cells + [""] * n)[:n])
        return out

    def set_cell(self, row: int, col: int, text: str) -> None:
        i = self.start + 2 + row
        line = self.doc.lines[i]
        cells = _split_row(line)
        cells += [""] * (len(self.header) - len(cells))
        cells[col] = text
        self.doc.lines[i] = _render_row(cells, line[: len(line) - len(line.lstrip())])

    def add_row(self, values: list[str]) -> None:
        indent = self.doc.lines[self.start][: len(self.doc.lines[self.start]) - len(self.doc.lines[self.start].lstrip())]
        self.doc.lines.insert(self.end + 1, _render_row(values, indent))
        self.end += 1
        for t in self.doc._tables:
            if t is not self and t.start > self.start:
                t.start += 1
                t.end += 1


class MarkdownDocument(Document):
    def __init__(self, path: Path):
        self.path = Path(path)
        raw = self.path.read_bytes().decode("utf-8-sig")
        self._nl = "\r\n" if "\r\n" in raw else "\n"
        self._bom = self.path.read_bytes().startswith(b"\xef\xbb\xbf")
        self.lines = raw.split("\r\n" if self._nl == "\r\n" else "\n")
        self._tables = self._scan()

    def _scan(self) -> list[MarkdownTable]:
        tables, i, n, fence = [], 0, len(self.lines), False
        heading = ""
        while i < n:
            ln = self.lines[i]
            if ln.lstrip().startswith("```"):
                fence = not fence
            elif not fence:
                if ln.startswith("#"):
                    heading = ln.lstrip("#").strip()
                elif ln.lstrip().startswith("|") and i + 1 < n and _SEP.match(self.lines[i + 1]):
                    j = i + 2
                    while j < n and self.lines[j].lstrip().startswith("|"):
                        j += 1
                    tables.append(MarkdownTable(self, i, j - 1, f"{heading or '(제목 없음)'} 표"))
                    i = j
                    continue
            i += 1
        return tables

    def tables(self) -> list[Table]:
        return list(self._tables)

    def paragraphs(self) -> list[Para]:
        out, fence = [], False
        in_table = set()
        for t in self._tables:
            in_table.update(range(t.start, t.end + 1))
        for i, ln in enumerate(self.lines):
            if ln.lstrip().startswith("```"):
                fence = not fence
                continue
            if fence or i in in_table or not ln.strip():
                continue
            if ln.startswith("#"):
                out.append(Para(i, ln.lstrip("#").strip(), True, len(ln) - len(ln.lstrip("#"))))
            else:
                out.append(Para(i, ln.strip()))
        return out

    def set_paragraph(self, index: int, text: str) -> None:
        old = self.lines[index]
        prefix = re.match(r"^(\s*(?:[-*+]|\d+\.)\s+)?", old).group(0)
        self.lines[index] = prefix + re.sub(r"\s*\n\s*", " ", text.strip())

    def save(self, out: Path) -> None:
        data = self._nl.join(self.lines).encode("utf-8")
        Path(out).write_bytes((b"\xef\xbb\xbf" if self._bom else b"") + data)
