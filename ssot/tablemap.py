"""문서 표의 헤더를 보고 어떤 종류(class/endpoint/config)이며 어느 열이 무슨 필드인지 판별한다."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

from .facts import HTTP_METHODS, norm_class, norm_config, norm_path

ALIASES: dict[str, dict[str, list[str]]] = {
    "endpoint": {
        "http_method": ["http메서드", "http메소드", "httpmethod", "메서드", "메소드", "method", "방식", "요청방식"],
        "path": ["경로", "url", "uri", "path", "api", "엔드포인트", "endpoint", "요청url", "api경로", "apiurl"],
        "controller": ["컨트롤러", "controller", "클래스", "class", "담당클래스"],
        "handler": ["핸들러", "핸들러메서드", "handler", "메서드명", "함수"],
        "params": ["파라미터", "요청파라미터", "params", "parameters", "request", "입력", "요청"],
        "returns": ["응답", "반환", "반환타입", "리턴", "response", "returns", "출력", "응답타입"],
        "summary": ["설명", "기능", "description", "개요", "비고", "summary", "api설명", "기능설명"],
    },
    "config": {
        "key": ["설정키", "설정", "프로퍼티", "property", "key", "항목", "설정항목", "키", "설정명", "프로퍼티명"],
        "value": ["기본값", "값", "value", "default", "현재값", "설정값"],
        "summary": ["설명", "기능", "description", "개요", "비고", "summary"],
    },
    "class": {
        "name": ["클래스", "클래스명", "컴포넌트", "컴포넌트명", "class", "classname", "component", "componentname", "name", "명칭", "모듈", "모듈명"],
        "stereotype": ["유형", "구분", "종류", "type", "stereotype", "layer", "계층", "역할구분", "컴포넌트유형"],
        "package": ["패키지", "package", "패키지명"],
        "summary": ["설명", "기능", "description", "역할", "개요", "비고", "summary", "컴포넌트설명", "기능설명"],
        "methods": ["메서드", "메소드", "주요메서드", "주요메소드", "methods", "주요기능", "제공메서드"],
        "fields": ["필드", "속성", "컬럼", "fields", "attributes", "주요필드"],
    },
}
NUMBER_COLS = {"no", "번호", "순번", "#", "연번", "seq"}


def norm_header(h: str) -> str:
    return re.sub(r"[\s_\-/()·.:*`]+", "", h).lower()


def load_aliases(config_path: Path | None) -> dict[str, dict[str, list[str]]]:
    """ssot.toml 의 [aliases.<kind>] field = ["헤더", ...] 로 별칭을 추가한다."""
    aliases = {k: {f: list(v) for f, v in d.items()} for k, d in ALIASES.items()}
    if config_path and Path(config_path).exists():
        cfg = tomllib.loads(Path(config_path).read_text(encoding="utf-8"))
        for kind, fields in cfg.get("aliases", {}).items():
            for fld, names in fields.items():
                aliases.setdefault(kind, {}).setdefault(fld, []).extend(norm_header(n) for n in names)
    return aliases


def column_map(header: list[str], kind: str, aliases) -> dict[str, int]:
    out: dict[str, int] = {}
    for ci, h in enumerate(header):
        nh = norm_header(h)
        for fld, names in aliases[kind].items():
            if fld not in out and nh in {norm_header(n) for n in names}:
                out[fld] = ci
                break
    return out


def classify(header: list[str], aliases=ALIASES) -> tuple[str, dict[str, int]] | None:
    ep = column_map(header, "endpoint", aliases)
    if "path" in ep:
        return "endpoint", ep
    cf = column_map(header, "config", aliases)
    if "key" in cf and "value" in cf:
        return "config", cf
    cl = column_map(header, "class", aliases)
    if "name" in cl:
        return "class", cl
    return None


_METHOD_PREFIX = re.compile(r"^\s*(" + "|".join(HTTP_METHODS) + r")\b\s*(.*)$", re.I)


def split_method_path(cell: str) -> tuple[str, str]:
    m = _METHOD_PREFIX.match(cell.replace("`", ""))
    return (m.group(1).upper(), m.group(2)) if m else ("", cell)


def row_key(kind: str, colmap: dict[str, int], cells: list[str]) -> str | None:
    def cell(f):
        return cells[colmap[f]] if f in colmap and colmap[f] < len(cells) else ""

    if kind == "class":
        return norm_class(cell("name")) or None
    if kind == "config":
        return norm_config(cell("key")) or None
    method = re.sub(r"[`\s]", "", cell("http_method")).split(",")[0].split("/")[0].upper()
    pm, path = split_method_path(cell("path"))
    method = method or pm
    return f"{method} {norm_path(path)}" if method and path.strip() else None
