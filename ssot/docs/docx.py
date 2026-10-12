"""DOCX 어댑터: document.xml 의 표/문단만 최소 수정하고 나머지 파트는 바이트 그대로 보존한다."""
from __future__ import annotations

import copy
import re
from pathlib import Path

from .base import Document, Para, Table
from .ooxml import XmlPart, read_zip, write_zip

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
HEADING = re.compile(r"^(heading|제목)\s*(\d+)?$", re.I)


def para_text(p) -> str:
    out = []
    for el in p.iter():
        if el.tag == W + "t":
            out.append(el.text or "")
        elif el.tag == W + "tab":
            out.append("\t")
        elif el.tag == W + "br":
            out.append("\n")
    return "".join(out)


def set_para_text(p, text: str) -> None:
    """첫 번째 텍스트 run 의 서식을 유지한 채 문단 내용을 교체한다."""
    runs = [r for r in p.iter(W + "r") if r.find(W + "t") is not None]
    keep = runs[0] if runs else p.makeelement(W + "r", {})
    for child in list(p):
        if child.tag in (W + "r", W + "hyperlink", W + "ins", W + "del"):
            p.remove(child)
    p.append(keep)
    for ch in list(keep):
        if ch.tag != W + "rPr":
            keep.remove(ch)
    for i, line in enumerate(text.replace(chr(13), "").split(chr(10))):
        if i:
            keep.append(keep.makeelement(W + "br", {}))
        t = keep.makeelement(W + "t", {XML_SPACE: "preserve"})
        t.text = line
        keep.append(t)


class DocxTable(Table):
    def __init__(self, tbl, label: str):
        self.tbl, self.label = tbl, label

    def _rows(self):
        return self.tbl.findall(W + "tr")

    @staticmethod
    def _cells(tr):
        return tr.findall(W + "tc")

    @staticmethod
    def _cell_text(tc) -> str:
        return "\n".join(para_text(p) for p in tc.findall(W + "p")).strip()

    @property
    def header(self) -> list[str]:
        return [self._cell_text(tc) for tc in self._cells(self._rows()[0])]

    def rows(self) -> list[list[str]]:
        n = len(self.header)
        out = []
        for tr in self._rows()[1:]:
            cells = [self._cell_text(tc) for tc in self._cells(tr)]
            out.append((cells + [""] * n)[:n])
        return out

    @staticmethod
    def _set_tc(tc, text: str) -> None:
        paras = tc.findall(W + "p")
        if not paras:
            paras = [tc.makeelement(W + "p", {})]
            tc.append(paras[0])
        for extra in paras[1:]:
            tc.remove(extra)
        set_para_text(paras[0], text)

    def set_cell(self, row: int, col: int, text: str) -> None:
        cells = self._cells(self._rows()[row + 1])
        self._set_tc(cells[col], text)

    def add_row(self, values: list[str]) -> None:
        rows = self._rows()
        new = copy.deepcopy(rows[-1])
        pr = new.find(W + "trPr")
        if pr is not None:
            for h in pr.findall(W + "tblHeader"):
                pr.remove(h)
        cells = self._cells(new)
        for tc, v in zip(cells, values):
            self._set_tc(tc, v)
        idx = list(self.tbl).index(rows[-1])
        self.tbl.insert(idx + 1, new)


class DocxDocument(Document):
    PART = "word/document.xml"

    def __init__(self, path: Path):
        self.path = Path(path)
        self._infos, self._files = read_zip(self.path)
        self._part = XmlPart(self._files[self.PART])
        self._body = self._part.root.find(W + "body")

    def _heading_of(self, p) -> tuple[bool, int]:
        st = p.find(f"{W}pPr/{W}pStyle")
        if st is not None:
            m = HEADING.match(st.get(W + "val", ""))
            if m:
                return True, int(m.group(2) or 1)
        return False, 0

    def tables(self) -> list[Table]:
        out, heading = [], ""
        for el in self._body:
            if el.tag == W + "p":
                is_h, _ = self._heading_of(el)
                if is_h:
                    heading = para_text(el).strip()
            elif el.tag == W + "tbl" and el.find(W + "tr") is not None:
                out.append(DocxTable(el, f"{heading or '(제목 없음)'} 표"))
        return out

    def _body_paras(self):
        return [el for el in self._body if el.tag == W + "p"]

    def paragraphs(self) -> list[Para]:
        out = []
        for i, p in enumerate(self._body_paras()):
            text = para_text(p).strip()
            if not text:
                continue
            is_h, lvl = self._heading_of(p)
            out.append(Para(i, text, is_h, lvl))
        return out

    def set_paragraph(self, index: int, text: str) -> None:
        set_para_text(self._body_paras()[index], text)

    def save(self, out: Path) -> None:
        files = dict(self._files)
        files[self.PART] = self._part.dump()
        write_zip(Path(out), self._infos, files)
