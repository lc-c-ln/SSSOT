"""영향도 분석: SR 내용으로 수정이 필요한 Java/Spring 소스 후보를 찾는다.

검색 = (1) LLM 이 한국어 요청을 영문 식별자 검색어로 확장 -> (2) BM25 키워드 검색
       + (3) Ollama 임베딩(bge-m3 등) 코사인 유사도 (사용 불가하면 자동으로 BM25 만 사용).
점수와 근거를 같이 돌려주고, 최종 대상은 담당자가 고른다.
"""
from __future__ import annotations

import array
import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import Path

from .java_analyzer import ClassInfo, parse_java
from .llm import LLM, extract_json
from .store import Store

EXCLUDE = {"test", "build", "target", ".git", "node_modules", "out", ".idea", ".gradle"}
MAX_FILE_BYTES = 400_000
TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*|\d+|[가-힣]+")
STOP = {"the", "and", "for", "get", "set", "public", "private", "return", "new", "this", "void", "string", "int", "api"}
TOP_FILES = 8


@dataclass
class Chunk:
    file: str
    cls: str
    method: str
    line: int
    text: str          # 색인용 텍스트
    snippet: str       # 화면 표시용


def tokens(text: str) -> list[str]:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", text)
    out: list[str] = []
    for m in TOKEN.finditer(text):
        t = m.group().lower()
        if re.match(r"[가-힣]", t):
            out.append(t)
            out += [t[i:i + 2] for i in range(len(t) - 1)] if len(t) > 2 else []
        elif len(t) > 1 and t not in STOP:
            out.append(t)
    return out


def _ann(anns) -> str:
    return " ".join(f"{a.name} {a.args}" for a in anns)


def _java_chunks(c: ClassInfo, lines: list[str]) -> list[Chunk]:
    out: list[Chunk] = []
    head = (f"{c.kind} {c.name} {c.package} {_ann(c.annotations)} {c.summary} extends {c.extends} implements {' '.join(c.implements)} "
            + " ".join(f"{f.name} {f.type} {_ann(f.annotations)} {f.summary}" for f in c.fields)
            + " " + " ".join(m.name for m in c.methods))
    out.append(Chunk(c.file, c.name, "", c.line, head, f"{c.kind} {c.name}"))
    starts = sorted({m.line for m in c.methods})
    for m in c.methods:
        nxt = next((s for s in starts if s > m.line), m.line + 40)
        body = lines[m.line - 1:min(nxt - 1, m.line + 39)]
        sig = f"{m.return_type} {m.name}({', '.join(f'{p.type} {p.name}' for p in m.params)})"
        text = f"{c.name} {m.name} {sig} {_ann(m.annotations)} {_ann([a for p in m.params for a in p.annotations])} {m.summary} " + "\n".join(body)
        out.append(Chunk(c.file, c.name, m.name, m.line, text, "\n".join(body[:12])))
    return out


SUFFIXES = (".java", ".xml", ".yml", ".yaml", ".properties")


def source_files(root: Path):
    """색인 대상 파일 (상대경로, 절대경로)."""
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in SUFFIXES or p.name == "pom.xml":
            continue
        rel = p.relative_to(root)
        if any(part in EXCLUDE for part in rel.parts):
            continue
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        yield str(rel).replace("\\", "/"), p


def file_chunks(relp: str, text: str) -> list[Chunk]:
    if relp.lower().endswith(".java"):
        out: list[Chunk] = []
        lines = text.splitlines()
        for c in parse_java(text, relp):
            out += _java_chunks(c, lines)
        return out
    lines = text.splitlines()
    name = relp.rsplit("/", 1)[-1]
    return [Chunk(relp, "", "", i + 1, f"{name} " + "\n".join(lines[i:i + 60]), "\n".join(lines[i:i + 8]))
            for i in range(0, len(lines), 60)]


def build_chunks(root: Path) -> list[Chunk]:
    """저장소 없이 한 번에 청크를 만든다(테스트/도구용). 서비스는 refresh_index 로 DB 에 미리 만들어 둔다."""
    out: list[Chunk] = []
    for relp, p in source_files(root):
        out += file_chunks(relp, p.read_text(encoding="utf-8", errors="replace"))
    return out


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf = [self._count(d) for d in docs]
        self.len = [len(d) for d in docs]
        self.avg = (sum(self.len) / len(docs)) if docs else 1
        df: dict[str, int] = {}
        for tf in self.tf:
            for t in tf:
                df[t] = df.get(t, 0) + 1
        n = len(docs)
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    @staticmethod
    def _count(d: list[str]) -> dict[str, int]:
        tf: dict[str, int] = {}
        for t in d:
            tf[t] = tf.get(t, 0) + 1
        return tf

    def score(self, i: int, query: dict[str, float]) -> tuple[float, list[tuple[str, float]]]:
        tf, total, parts = self.tf[i], 0.0, []
        for t, w in query.items():
            f = tf.get(t)
            if not f:
                continue
            s = w * self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[i] / self.avg))
            total += s
            parts.append((t, s))
        return total, sorted(parts, key=lambda x: -x[1])


def _pack(v: list[float]) -> bytes:
    return array.array("f", v).tobytes()


def _unpack(b: bytes) -> list[float]:
    a = array.array("f")
    a.frombytes(b)
    return list(a)


_sumprod = getattr(math, "sumprod", None)   # Python 3.12+ : C 구현으로 훨씬 빠르다


def _dot(a: list[float], b: list[float]) -> float:
    return _sumprod(a, b) if _sumprod else sum(x * y for x, y in zip(a, b))


def _cos(a: list[float], b: list[float]) -> float:
    na, nb = math.sqrt(_dot(a, a)), math.sqrt(_dot(b, b))
    return _dot(a, b) / (na * nb) if na and nb else 0.0


def embed_chunks(store: Store, llm, chunks: list[Chunk], progress=None) -> list[list[float]]:
    """캐시를 쓰며 청크를 임베딩한다 (변경된 청크만 Ollama 호출)."""
    model = getattr(llm, "embed_model", "?")
    texts = [c.text[:1500] for c in chunks]
    shas = [hashlib.sha256(t.encode("utf-8")).hexdigest() for t in texts]
    have = store.cache_get(list(set(shas)), model)
    todo = [i for i, s in enumerate(shas) if s not in have]
    new: dict[str, bytes] = {}
    for k in range(0, len(todo), 32):
        idx = todo[k:k + 32]
        vecs = llm.embed([texts[i] for i in idx])
        if len(vecs) != len(idx):
            raise RuntimeError("임베딩 응답 개수가 요청과 다릅니다.")
        if progress:
            progress(min(k + 32, len(todo)), len(todo))
        for i, v in zip(idx, vecs):
            new[shas[i]] = _pack(v)
    if new:
        store.cache_put(new, model)
        have.update(new)
    return [_unpack(have[s]) for s in shas]


def expand_keywords(llm: LLM, title: str, body: str) -> tuple[list[str], dict]:
    system = ("당신은 Java/Spring 코드베이스 검색을 돕는 도우미입니다. 요청을 구현하려면 수정해야 할 소스를 찾기 위한 "
              "검색어를 제안하세요. 영문 클래스/메서드/필드명 후보(camelCase 단어 단위), URL 경로 조각, 핵심 한국어 용어를 섞어 "
              '최대 15개. JSON 한 개만 출력: {"keywords":["..."]}')
    detail = {"system": system, "user": f"[제목]\n{title}\n\n[내용]\n{body}"}
    try:
        raw = llm.complete(system, detail["user"], max_tokens=300, json_mode=True)
        kws = [str(k) for k in extract_json(raw).get("keywords", []) if isinstance(k, (str, int))][:15]
        return kws, {**detail, "response": raw}
    except Exception as e:  # noqa: BLE001 - 확장 실패해도 원문 검색은 가능
        return [], {**detail, "error": f"{type(e).__name__}: {e}"}


_MEM: dict[str, tuple[int, list[Chunk], BM25]] = {}
STEREO = ("Controller", "RestController", "Service", "Repository", "Component", "Mapper", "Configuration", "Entity", "FeignClient")
SUMMARY_SYSTEM = ("당신은 Java/Spring 코드를 읽고 클래스의 역할을 요약하는 도우미입니다. 이 클래스가 업무적으로 무엇을 담당하는지 "
                  "한국어 1~2문장으로 쓰세요. 코드에 나오는 업무 용어(회원, 주문, 쿠폰 등)와 다루는 API/기능을 포함하세요. "
                  'JSON 한 개만 출력: {"summary":"..."}')


def _sha(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


def refresh_index(store: Store, root: Path, project: str, llm=None, summarize: bool = False, embed: bool = False,
                  progress=None) -> dict:
    """소스를 읽어 DB 색인을 갱신한다. 바뀐 파일만 다시 파싱하고, summarize/embed 를 켜면 AI 요약·임베딩도 미리 만든다.
    progress(dict) 콜백으로 진행 상황을 알린다."""
    report = progress or (lambda d: None)
    known = store.code_files(project)
    seen: set[str] = set()
    changed = 0
    for relp, p in source_files(root):
        seen.add(relp)
        st = p.stat()
        old = known.get(relp)
        if old and old[0] == st.st_size and old[1] == st.st_mtime_ns:
            continue
        data = p.read_bytes()
        sha = _sha(data)
        if old and old[2] == sha:
            store.code_touch_file(project, relp, st.st_size, st.st_mtime_ns)
            continue
        chunks = file_chunks(relp, data.decode("utf-8", errors="replace"))
        store.code_replace_file(project, relp, st.st_size, st.st_mtime_ns, sha,
                                [(c.cls, c.method, c.line, c.text, c.snippet) for c in chunks])
        changed += 1
    gone = [k for k in known if k not in seen]
    if gone:
        store.code_delete_files(project, gone)
    meta = store.index_meta(project)
    if changed or gone or not meta["built_at"]:
        store.set_index_meta(project, bump=True, built_at=datetime_now(), files=len(seen), last_error="")
    rows = store.code_chunks(project)
    report({"phase": "parsed", "files": len(seen), "changed": changed, "chunks": len(rows)})

    summarized = 0
    if summarize and llm is not None:
        summarized = summarize_classes(store, llm, project, report)
    embedded = 0
    if embed and hasattr(llm, "embed"):
        chunks, _ = load_index(store, project, force=True)
        try:
            embed_chunks(store, llm, chunks, progress=lambda i, n: report({"phase": "embedding", "done": i, "total": n}))
            embedded = len(chunks)
        except Exception as e:  # noqa: BLE001 - 임베딩 실패해도 키워드 색인은 유효
            store.set_index_meta(project, last_error=f"임베딩 생략: {e}"[:300])
    meta_f = {"files": len(seen), "chunks": len(rows), "summaries": len(store.summaries(project))}
    if embedded:
        meta_f["embedded"] = embedded
    store.set_index_meta(project, **meta_f)
    report({"phase": "done", **meta_f})
    return {"files": len(seen), "changed": changed, "removed": len(gone), "chunks": len(rows), "summarized": summarized,
            "embedded": embedded}


def datetime_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def summarize_classes(store: Store, llm, project: str, report=lambda d: None) -> int:
    """역할이 분명한 클래스(Spring 스테레오타입 또는 메서드 3개 이상)를 AI로 한 번만 요약한다. 파일이 바뀐 것만 다시 요약."""
    files = store.code_files(project)
    have = store.summaries(project)
    todo: list[tuple[str, str, str, str]] = []     # path, cls, sha, material
    by_file: dict[str, list] = {}
    for r in store.code_chunks(project):
        if r["path"].endswith(".java"):
            by_file.setdefault(r["path"], []).append(r)
    for path, rows in by_file.items():
        sha = files[path][2]
        for head in (r for r in rows if r["cls"] and not r["method"]):
            cls = head["cls"]
            if (path, cls) in have and have[(path, cls)][0] == sha:
                continue
            methods = [r for r in rows if r["cls"] == cls and r["method"]]
            important = any(k in head["text"] for k in STEREO) or len(methods) >= 3
            if not important:
                continue
            sigs = "\n".join(m["text"].split("\n")[0][:200] for m in methods[:25])
            todo.append((path, cls, sha, f"{head['text'][:1500]}\n[메서드]\n{sigs}"))
    done = 0
    for i, (path, cls, sha, material) in enumerate(todo, 1):
        report({"phase": "summarizing", "done": i - 1, "total": len(todo), "current": f"{cls}"})
        try:
            raw = llm.complete(SUMMARY_SYSTEM, f"[클래스: {cls}]\n{material}", max_tokens=200, json_mode=True)
            summ = str(extract_json(raw).get("summary", "")).strip()[:400]
        except Exception:  # noqa: BLE001 - 요약 실패한 클래스는 건너뛴다(검색은 코드 본문으로 가능)
            continue
        if summ:
            store.put_summary(project, path, cls, sha, summ)
            done += 1
    report({"phase": "summarizing", "done": len(todo), "total": len(todo)})
    if done:
        store.set_index_meta(project, bump=True)      # 요약이 색인 텍스트에 합쳐지므로 메모리 캐시 무효화
    return done


def load_index(store: Store, project: str, force: bool = False) -> tuple[list[Chunk], BM25]:
    """DB 에 저장된 색인을 메모리로 올린다(색인 버전이 같으면 재사용). 클래스 요약은 클래스 청크 텍스트에 합친다."""
    ver = store.index_meta(project)["version"]
    hit = _MEM.get(project)
    if hit and hit[0] == ver and not force:
        return hit[1], hit[2]
    summ = store.summaries(project)
    chunks = []
    for r in store.code_chunks(project):
        text = r["text"]
        if r["cls"] and not r["method"] and (r["path"], r["cls"]) in summ:
            text = f"{text} {summ[(r['path'], r['cls'])][1]}"
        chunks.append(Chunk(r["path"], r["cls"], r["method"], r["line"], text, r["snippet"]))
    bm = BM25([tokens(c.text) for c in chunks])
    _MEM[project] = (ver, chunks, bm)
    while len(_MEM) > 8:
        _MEM.pop(next(iter(_MEM)))
    return chunks, bm


def get_index(store: Store, root: Path, project: str) -> tuple[list[Chunk], BM25]:
    """요청 시점: 이미 분석된 색인을 쓰되, 파일이 바뀌었으면 바뀐 파일만 빠르게 다시 파싱한다(요약/임베딩 없이)."""
    refresh_index(store, root, project)
    return load_index(store, project)


def search(store: Store, llm, root: Path, title: str, body: str, keywords: list[str],
           project: str = "") -> tuple[list[dict], bool, str]:
    chunks, bm = get_index(store, root, project or str(root.resolve()))
    if not chunks:
        return [], False, "색인할 Java/XML/설정 파일이 없습니다."
    q: dict[str, float] = {}
    for t in tokens(f"{title} {body}"):
        q[t] = max(q.get(t, 0), 0.5)
    for k in keywords:
        for t in tokens(k):
            q[t] = 1.0
    bm_scores = [bm.score(i, q) for i in range(len(chunks))]
    top_bm = max((s for s, _ in bm_scores), default=0) or 1.0

    vec_scores = [0.0] * len(chunks)
    vector_used, note = False, ""
    if hasattr(llm, "embed"):
        try:
            cv = embed_chunks(store, llm, chunks)
            qv = llm.embed([f"{title}\n{body}\n{' '.join(keywords)}"[:1500]])[0]
            vec_scores = [max(0.0, _cos(qv, v)) for v in cv]
            vector_used = True
        except Exception as e:  # noqa: BLE001 - 임베딩 모델 미설치 등: 키워드 검색만 사용
            note = f"벡터 검색 생략({e}) · 키워드 검색만 사용"[:200]
    top_vec = max(vec_scores) or 1.0

    scored = []
    for i, c in enumerate(chunks):
        b = bm_scores[i][0] / top_bm
        v = vec_scores[i] / top_vec if vector_used else 0.0
        scored.append(((0.6 * b + 0.4 * v) if vector_used else b, i))
    scored.sort(reverse=True)

    files: dict[str, dict] = {}
    for s, i in scored[:60]:
        if s <= 0:
            break
        c = chunks[i]
        f = files.setdefault(c.file, {"file": c.file, "score": 0.0, "chunks": []})
        why = [t for t, _ in bm_scores[i][1][:4]]
        f["chunks"].append({"cls": c.cls, "method": c.method, "line": c.line, "score": round(s, 3),
                            "terms": why, "snippet": c.snippet})
        f["score"] = max(f["score"], s)
    ranked = sorted(files.values(), key=lambda f: -f["score"])[:TOP_FILES]
    for n, f in enumerate(ranked):
        f["score"] = round(f["score"], 3)
        f["chunks"] = f["chunks"][:3]
        f["preselected"] = n < 3 and f["score"] >= 0.35 * ranked[0]["score"]
        best = f["chunks"][0]
        f["reason"] = ", ".join(best["terms"]) or "유사도"
    return ranked, vector_used, note


def analyze_sr(store: Store, llm, sr_id: int) -> str:
    sr = store.get_sr(sr_id)
    if not sr:
        raise ValueError(f"SR {sr_id} 없음")
    proj = store.get_project(sr["project"]) if sr["project"] else None
    if not proj:
        msg = "프로젝트 설정에서 대상 프로젝트를 등록하고 SR의 프로젝트를 지정해 주세요." if not sr["project"] else \
            f"등록되지 않은 프로젝트입니다: {sr['project']}"
        store.audit(sr_id, "system", "impact.error", {"error": msg})
        store.update_sr(sr_id, status="impact_error", reason=msg)
        return "impact_error"
    root = Path(proj["local_path"])
    if not root.is_dir():
        msg = f"로컬 소스 경로를 찾을 수 없습니다: {root}"
        store.audit(sr_id, "system", "impact.error", {"error": msg})
        store.update_sr(sr_id, status="impact_error", reason=msg)
        return "impact_error"
    store.update_sr(sr_id, status="analyzing")
    kws, kdetail = expand_keywords(llm, sr["title"], sr["body"])
    store.audit(sr_id, "system", "impact.keywords", {**kdetail, "keywords": kws})
    try:
        cands, vec, note = search(store, llm, root, sr["title"], sr["body"], kws, proj["name"])
    except Exception as e:  # noqa: BLE001
        store.audit(sr_id, "system", "impact.error", {"error": f"{type(e).__name__}: {e}"})
        store.update_sr(sr_id, status="impact_error", reason=f"영향 분석 실패: {e}"[:300])
        return "impact_error"
    store.save_impact(sr_id, kws, cands, vec)
    store.audit(sr_id, "system", "impact.analyzed",
                {"project": proj["name"], "files": [(c["file"], c["score"]) for c in cands], "vector": vec, "note": note})
    store.update_sr(sr_id, status="impact_ready", reason=note or ("후보 %d개 파일" % len(cands)))
    return "impact_ready"


def confirm_selection(store: Store, sr_id: int, actor: str, files: list[str]) -> None:
    sr = store.get_sr(sr_id)
    imp = store.get_impact(sr_id)
    if not sr or not imp:
        raise ValueError("영향 분석 결과가 없습니다.")
    if not actor.strip():
        raise ValueError("담당자 이름이 필요합니다.")
    proj = store.get_project(sr["project"])
    root = Path(proj["local_path"]).resolve()
    clean: list[str] = []
    for f in files:
        p = (root / f).resolve()
        if root != p and root not in p.parents or not p.is_file():   # 경로 탈출 방지 + 존재 확인
            raise ValueError(f"프로젝트 안에 없는 파일입니다: {f}")
        clean.append(str(p.relative_to(root)).replace("\\", "/"))
    if not clean:
        raise ValueError("수정 대상 파일을 하나 이상 선택하세요.")
    store.set_selected(sr_id, clean)
    store.audit(sr_id, actor.strip(), "impact.confirmed", {"files": clean, "suggested": [c["file"] for c in imp["candidates"]]})
    store.update_sr(sr_id, status="impact_confirmed")
