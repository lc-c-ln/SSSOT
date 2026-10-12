"""OOXML(zip+XML) 공용 도구. 원본 루트 태그(네임스페이스 선언)를 보존해 Office 호환성을 유지한다."""
from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

NS_DECL = re.compile(r'xmlns(?::(\w+))?="([^"]+)"')


class XmlPart:
    def __init__(self, data: bytes):
        text = data.decode("utf-8")
        m = re.match(r"^(<\?xml[^>]*\?>\s*)?", text)
        self.decl = m.group(0)
        root_m = re.search(r"<[A-Za-z_][\w:.\-]*\b[^>]*>", text[len(self.decl):])
        self.start_tag = root_m.group(0)
        self.nsmap = [(p or "", u) for p, u in NS_DECL.findall(self.start_tag)]
        self._register()
        self.root = ET.fromstring(data)

    def _register(self) -> None:
        for prefix, uri in self.nsmap:
            ET.register_namespace(prefix, uri)

    def dump(self) -> bytes:
        self._register()
        body = ET.tostring(self.root, encoding="unicode")
        body = re.sub(r"^<[A-Za-z_][\w:.\-]*\b[^>]*>", lambda _m: self.start_tag, body, count=1)
        return (self.decl + body).encode("utf-8")


def read_zip(path: Path) -> tuple[list[zipfile.ZipInfo], dict[str, bytes]]:
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        return infos, {i.filename: z.read(i.filename) for i in infos}


def write_zip(out: Path, infos: list[zipfile.ZipInfo], files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for info in infos:
            z.writestr(info.filename, files[info.filename], compress_type=zipfile.ZIP_DEFLATED)
