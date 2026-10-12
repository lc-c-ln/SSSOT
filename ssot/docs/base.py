"""문서 어댑터 공통 인터페이스. 형식(md/docx/xlsx)과 무관하게 표·문단 단위로 다룬다."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Para:
    index: int
    text: str
    heading: bool = False
    level: int = 0


class Table:
    label: str = ""

    @property
    def header(self) -> list[str]:
        raise NotImplementedError

    def rows(self) -> list[list[str]]:  # 헤더 제외
        raise NotImplementedError

    def set_cell(self, row: int, col: int, text: str) -> None:
        raise NotImplementedError

    def add_row(self, values: list[str]) -> None:
        raise NotImplementedError


class Document:
    path: Path

    def tables(self) -> list[Table]:
        raise NotImplementedError

    def paragraphs(self) -> list[Para]:
        return []

    def set_paragraph(self, index: int, text: str) -> None:
        raise NotImplementedError

    def save(self, out: Path) -> None:
        raise NotImplementedError


def open_document(path: str | Path) -> Document:
    p = Path(path)
    ext = p.suffix.lower()
    if ext in (".md", ".markdown"):
        from .markdown import MarkdownDocument
        return MarkdownDocument(p)
    if ext == ".docx":
        from .docx import DocxDocument
        return DocxDocument(p)
    if ext == ".xlsx":
        from .xlsx import XlsxDocument
        return XlsxDocument(p)
    raise ValueError(f"지원하지 않는 문서 형식: {p.suffix} ({p.name})")
