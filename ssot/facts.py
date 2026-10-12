"""분석 결과를 문서와 대조할 수 있는 'Fact'(사실) 단위로 변환한다."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .java_analyzer import Annotation, ClassInfo, MethodInfo, parse_java

HTTP_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")
MAPPING_ANN = {
    "GetMapping": "GET", "PostMapping": "POST", "PutMapping": "PUT",
    "DeleteMapping": "DELETE", "PatchMapping": "PATCH", "RequestMapping": None,
}
STEREOTYPES = [
    "RestController", "Controller", "RestControllerAdvice", "ControllerAdvice", "Service",
    "Repository", "Entity", "Configuration", "Mapper", "SpringBootApplication", "Component",
]
SERVLET_TYPES = {
    "HttpServletRequest", "HttpServletResponse", "Principal", "Model", "BindingResult",
    "Authentication", "Locale", "HttpSession", "ModelMap", "RedirectAttributes",
}
PARAM_SOURCE = {
    "PathVariable": "path", "RequestParam": "query", "RequestBody": "body",
    "RequestHeader": "header", "ModelAttribute": "form", "RequestPart": "part",
}
SECRET_RE = re.compile(r"(password|passwd|secret|token|credential|private|api[-_]?key)", re.I)


@dataclass
class Fact:
    kind: str                 # class | endpoint | config
    match: str                # 문서 행과 대조할 정규화 키
    fields: dict[str, str]    # 정규 필드명 -> 값
    group: str                # 표가 여러 개일 때 새 행을 어느 표에 넣을지 판단하는 힌트
    source: str               # 파일:라인
    label: str = ""           # 사람이 읽는 이름

    def get(self, name: str) -> str:
        return self.fields.get(name, "")


# ------------------------------------------------------------- 정규화 (문서 쪽에서도 공용)

def norm_class(text: str) -> str:
    t = re.sub(r"`|\*\*|@", "", text).strip()
    t = re.sub(r"\.java$", "", t)
    return t.split(".")[-1].strip() if t else ""


def norm_path(text: str) -> str:
    t = re.sub(r"[`\s]", "", text.split("?")[0])
    t = re.sub(r"\{[^}/]*\}", "{}", t)
    t = "/" + t.strip("/")
    return t if t != "/" else "/"


def norm_config(text: str) -> str:
    return re.sub(r"[`\s\-_]", "", text).lower()


def canon_stereotype(text: str) -> str:
    t = re.sub(r"[@`\s]", "", text).lower()
    ko = {"컨트롤러": "controller", "서비스": "service", "리포지토리": "repository",
          "레포지토리": "repository", "엔티티": "entity", "설정": "configuration", "컴포넌트": "component"}
    for k, v in ko.items():
        t = t.replace(k, v)
    return t.replace("restcontroller", "controller").replace("restcontrolleradvice", "controlleradvice")


# ------------------------------------------------------------- 어노테이션 해석

def _ann(anns: list[Annotation], *names: str) -> Annotation | None:
    for a in anns:
        if a.name in names:
            return a
    return None


def _attr_strings(args: str, *keys: str) -> list[str]:
    for key in keys:
        mt = re.search(rf"\b{key}\s*=\s*(\{{[^}}]*\}}|\"[^\"]*\")", args)
        if mt:
            return re.findall(r'"([^"]*)"', mt.group(1))
    if "=" not in args.split('"')[0]:
        return re.findall(r'"([^"]*)"', args)
    return []


def _join(base: str, sub: str) -> str:
    p = "/" + "/".join(s.strip("/") for s in (base, sub) if s.strip("/"))
    return p if p != "" else "/"


def _mapping(anns: list[Annotation]) -> tuple[list[str], list[str]] | None:
    for a in anns:
        if a.name in MAPPING_ANN:
            paths = _attr_strings(a.args, "value", "path") or [""]
            fixed = MAPPING_ANN[a.name]
            if fixed:
                methods = [fixed]
            else:
                mt = re.search(r"\bmethod\s*=\s*(\{[^}]*\}|[\w.]+)", a.args)
                methods = [m for m in HTTP_METHODS if mt and re.search(rf"\b{m}\b", mt.group(1))] or ["GET"]
            return paths, methods
    return None


def _stereotype(c: ClassInfo) -> str:
    for s in STEREOTYPES:
        if _ann(c.annotations, s):
            return s
    if c.kind == "interface" and re.search(r"Repository|JpaRepository|CrudRepository", c.extends):
        return "Repository"
    return ""


def _fmt_return(t: str) -> str:
    mt = re.match(r"^ResponseEntity<(.+)>$", t.strip())
    if mt:
        t = mt.group(1)
    return "Object" if t.strip() == "?" else t.strip()


def _fmt_params(m: MethodInfo) -> str:
    out = []
    for p in m.params:
        if p.type.split("<")[0] in SERVLET_TYPES:
            continue
        src = next((PARAM_SOURCE[a.name] for a in p.annotations if a.name in PARAM_SOURCE), "")
        named = next((a for a in p.annotations if a.name in PARAM_SOURCE), None)
        nm = (_attr_strings(named.args, "value", "name") if named else []) or [p.name]
        out.append(f"{nm[0]}: {p.type}" + (f" ({src})" if src else ""))
    return ", ".join(out)


# ------------------------------------------------------------- 추출

def class_facts(classes: list[ClassInfo]) -> list[Fact]:
    facts: list[Fact] = []
    for c in classes:
        st = _stereotype(c)
        if not st:
            continue
        public = [m.name for m in c.methods if "public" in m.modifiers or c.kind == "interface"]
        fields = [f"{f.name}: {f.type}" for f in c.fields if "static" not in f.modifiers] if st == "Entity" else []
        facts.append(Fact(
            kind="class", match=norm_class(c.name),
            fields={"name": c.name, "stereotype": st, "package": c.package, "summary": c.summary,
                    "methods": ", ".join(dict.fromkeys(public)), "fields": ", ".join(fields)},
            group=canon_stereotype(st), source=f"{c.file}:{c.line}", label=c.name,
        ))
    return facts


def endpoint_facts(classes: list[ClassInfo]) -> list[Fact]:
    facts: list[Fact] = []
    for c in classes:
        if not _ann(c.annotations, "RestController", "Controller"):
            continue
        base = _mapping(c.annotations)
        base_paths = base[0] if base else [""]
        for m in c.methods:
            mp = _mapping(m.annotations)
            if not mp:
                continue
            swagger = _ann(m.annotations, "Operation")
            summary = m.summary or (next(iter(_attr_strings(swagger.args, "summary")), "") if swagger else "")
            for bp in base_paths:
                for sub in mp[0]:
                    path = _join(bp, sub)
                    for http in mp[1]:
                        facts.append(Fact(
                            kind="endpoint", match=f"{http} {norm_path(path)}",
                            fields={"http_method": http, "path": path, "controller": c.name, "handler": m.name,
                                    "params": _fmt_params(m), "returns": _fmt_return(m.return_type), "summary": summary},
                            group=c.name, source=f"{c.file}:{m.line}", label=f"{http} {path}",
                        ))
    return facts


def _yaml_flat(text: str) -> list[tuple[str, str, str]]:
    """단순 YAML(들여쓰기 맵) -> (key, value, 직전 주석). 리스트/앵커는 최소 지원."""
    out, stack, comment = [], [], ""
    for raw in text.splitlines():
        if not raw.strip():
            comment = ""
            continue
        if raw.lstrip().startswith("#"):
            comment = raw.lstrip().lstrip("#").strip()
            continue
        if raw.strip() == "---":
            stack = []
            continue
        indent = len(raw) - len(raw.lstrip())
        mt = re.match(r"^\s*(?:-\s+)?([\w.\-\[\]\"']+)\s*:\s*(.*)$", raw)
        if not mt:
            continue
        while stack and stack[-1][0] >= indent:
            stack.pop()
        key = mt.group(1).strip("\"'")
        val = re.sub(r"\s+#.*$", "", mt.group(2)).strip().strip("\"'")
        stack.append((indent, key))
        if val:
            out.append((".".join(k for _, k in stack), val, comment))
        comment = ""
    return out


def config_facts(root: Path) -> list[Fact]:
    files = sorted(
        (p for p in root.rglob("application*") if p.suffix in (".properties", ".yml", ".yaml")),
        key=lambda p: (p.stem != "application", str(p)),
    )
    seen: dict[str, Fact] = {}
    for p in files:
        text = p.read_text(encoding="utf-8", errors="replace")
        if p.suffix == ".properties":
            entries, comment = [], ""
            for ln in text.splitlines():
                s = ln.strip()
                if s.startswith("#"):
                    comment = s.lstrip("#").strip()
                elif "=" in s:
                    k, v = s.split("=", 1)
                    entries.append((k.strip(), v.strip(), comment))
                    comment = ""
                elif not s:
                    comment = ""
        else:
            entries = _yaml_flat(text)
        for k, v, cm in entries:
            nk = norm_config(k)
            if nk in seen:
                continue
            val = "****" if SECRET_RE.search(k) else v
            seen[nk] = Fact("config", nk, {"key": k, "value": val, "summary": cm},
                            group=k.split(".")[0], source=f"{p.name}", label=k)
    return list(seen.values())


def value_annotation_facts(classes: list[ClassInfo], known: set[str]) -> list[Fact]:
    """@Value("${a.b:default}") 로만 선언된 설정 키 보충."""
    facts = []
    for c in classes:
        for f in c.fields:
            a = _ann(f.annotations, "Value")
            if not a:
                continue
            mt = re.search(r"\$\{([^}:]+)(?::([^}]*))?\}", a.args)
            if not mt or norm_config(mt.group(1)) in known:
                continue
            key, default = mt.group(1).strip(), (mt.group(2) or "").strip()
            known.add(norm_config(key))
            facts.append(Fact("config", norm_config(key),
                              {"key": key, "value": "****" if SECRET_RE.search(key) else default, "summary": f.summary},
                              group=key.split(".")[0], source=f"{c.file}:{f.line}", label=key))
    return facts


def collect_facts(src: Path, exclude: tuple[str, ...] = ("test", "build", "target", ".git", "node_modules")) -> list[Fact]:
    src = Path(src)
    classes: list[ClassInfo] = []
    for p in sorted(src.rglob("*.java")):
        rel = p.relative_to(src)
        if any(part in exclude for part in rel.parts):
            continue
        classes += parse_java(p.read_text(encoding="utf-8", errors="replace"), str(rel).replace("\\", "/"))
    facts = class_facts(classes) + endpoint_facts(classes)
    cfg = config_facts(src)
    known = {f.match for f in cfg}
    return facts + cfg + value_annotation_facts(classes, known)
