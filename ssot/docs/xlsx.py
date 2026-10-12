"""XLSX 어댑터: 시트 하나를 표 하나로 본다(첫 번째 2칸 이상 채워진 행이 헤더)."""
from __future__ import annotations

import posixpath
import re
from pathlib import Path

from .base import Document, Table
from .ooxml import XmlPart, read_zip, write_zip

M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def col_letters(i: int) -> str:
    s, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


class XlsxTable(Table):
    def __init__(self, doc: "XlsxDocument", part: XmlPart, name: str):
        self.doc, self.part, self.label = doc, part, f"시트 '{name}'"
        self.data = part.root.find(M + "sheetData")
        self._header_row = self._find_header()

    def _text(self, c) -> str:
        t = c.get("t")
        if t == "s":
            v = c.find(M + "v")
            return self.doc.shared[int(v.text)] if v is not None and v.text else ""
        if t == "inlineStr":
            return "".join(x.text or "" for x in c.iter(M + "t"))
        v = c.find(M + "v")
        return v.text or "" if v is not None else ""

    def _row_cells(self, row) -> dict[int, str]:
        return {col_index(c.get("r")): self._text(c) for c in row.findall(M + "c")}

    def _find_header(self):
        for row in self.data.findall(M + "row"):
            if sum(1 for v in self._row_cells(row).values() if v.strip()) >= 2:
                return row
        return None

    def _data_rows(self):
        rows = self.data.findall(M + "row")
        return rows[rows.index(self._header_row) + 1:] if self._header_row is not None else []

    @property
    def header(self) -> list[str]:
        if self._header_row is None:
            return []
        cells = self._row_cells(self._header_row)
        n = max(cells) + 1 if cells else 0
        return [cells.get(i, "").strip() for i in range(n)]

    def rows(self) -> list[list[str]]:
        n = len(self.header)
        out = []
        for row in self._data_rows():
            cells = self._row_cells(row)
            out.append([cells.get(i, "") for i in range(n)])
        return out

    @staticmethod
    def _put(row, col: int, text: str, style: str | None = None):
        ref = f"{col_letters(col)}{row.get('r')}"
        cell = next((c for c in row.findall(M + "c") if c.get("r") == ref), None)
        if cell is None:
            cell = row.makeelement(M + "c", {"r": ref})
            pos = len([c for c in row.findall(M + "c") if col_index(c.get("r")) < col])
            row.insert(pos, cell)
        if style and "s" not in cell.attrib:
            cell.set("s", style)
        for ch in list(cell):
            cell.remove(ch)
        cell.set("t", "inlineStr")
        is_ = cell.makeelement(M + "is", {})
        t = is_.makeelement(M + "t", {"{http://www.w3.org/XML/1998/namespace}space": "preserve"})
        t.text = text
        is_.append(t)
        cell.append(is_)

    def set_cell(self, row: int, col: int, text: str) -> None:
        self._put(self._data_rows()[row], col, text)

    def add_row(self, values: list[str]) -> None:
        rows = self.data.findall(M + "row")
        last = rows[-1]
        num = int(last.get("r")) + 1
        new = self.data.makeelement(M + "row", {k: v for k, v in last.attrib.items() if k not in ("r", "spans")})
        new.set("r", str(num))
        styles = {col_index(c.get("r")): c.get("s") for c in last.findall(M + "c")}
        for i, v in enumerate(values):
            self._put(new, i, v, styles.get(i))
        self.data.append(new)
        dim = self.part.root.find(M + "dimension")
        if dim is not None and ":" in dim.get("ref", ""):
            a, b = dim.get("ref").split(":")
            dim.set("ref", f"{a}:{col_letters(max(col_index(b), len(values) - 1))}{num}")


class XlsxDocument(Document):
    def __init__(self, path: Path):
        self.path = Path(path)
        self._infos, self._files = read_zip(self.path)
        self.shared = self._read_shared()
        self._sheets = self._read_sheets()  # [(name, partname, XmlPart)]

    def _read_shared(self) -> list[str]:
        data = self._files.get("xl/sharedStrings.xml")
        if not data:
            return []
        root = XmlPart(data).root
        return ["".join(t.text or "" for t in si.iter(M + "t")) for si in root.findall(M + "si")]

    def _read_sheets(self):
        wb = XmlPart(self._files["xl/workbook.xml"]).root
        rels = XmlPart(self._files["xl/_rels/workbook.xml.rels"]).root
        target = {r.get("Id"): r.get("Target") for r in rels.findall(PKG_REL + "Relationship")}
        out = []
        for s in wb.find(M + "sheets"):
            t = target[s.get(R + "id")]
            name = t.lstrip("/") if t.startswith("/") else posixpath.normpath(posixpath.join("xl", t))
            out.append((s.get("name"), name, XmlPart(self._files[name])))
        return out

    def tables(self) -> list[Table]:
        tabs = [XlsxTable(self, part, name) for name, _, part in self._sheets]
        return [t for t in tabs if t.header]

    def save(self, out: Path) -> None:
        files = dict(self._files)
        for _, part_name, part in self._sheets:
            files[part_name] = part.dump()
        write_zip(Path(out), self._infos, files)
