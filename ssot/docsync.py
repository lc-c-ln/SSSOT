"""커밋 -> 문서 갱신 제안. GitLab Webhook(Push) 또는 수동 스캔으로 시작한다.

흐름: 커밋 시점의 소스(임시 worktree)에서 사실을 추출 -> 프로젝트에 등록된 문서와 대조(기존 SSSOT 엔진, 필요 시 Ollama)
      -> 변경 제안을 DB에 저장 -> 담당자가 항목별로 승인하면 `<이름>.updated.<확장자>` 로 저장. 원본은 수정하지 않는다.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
from dataclasses import asdict
from pathlib import Path

from . import gitops
from .docs import open_document
from .facts import collect_facts
from .llm import LLM
from .store import Store
from .sync import apply_ops, sync_narrative, sync_tables
from .tablemap import load_aliases

EDITABLE = ("update", "fill", "add", "narrative")
SR_REF = re.compile(r"\bSR-(\d{1,7})\b")


def verify_secret(header_value: str | None) -> bool:
    secret = os.environ.get("SSOT_WEBHOOK_SECRET", "")
    return bool(secret) and bool(header_value) and hmac.compare_digest(header_value.encode(), secret.encode())


def _norm_url(u: str) -> str:
    u = (u or "").strip().lower().rstrip("/")
    u = re.sub(r"\.git$", "", u)
    return re.sub(r"^https?://([^@/]+@)?", "", u)


def match_project(store: Store, payload: dict) -> dict | None:
    p = payload.get("project") or {}
    cands = {_norm_url(p.get(k, "")) for k in ("web_url", "git_http_url", "homepage") if p.get(k)}
    for proj in store.list_projects():
        if proj["gitlab_url"] and _norm_url(proj["gitlab_url"]) in cands:
            return proj
    return None


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def doc_files(proj: dict) -> list[Path]:
    root = Path(proj["local_path"])
    out = []
    for d in proj["doc_paths"]:
        p = Path(d)
        out.append(p if p.is_absolute() else root / p)
    return out


def scan_commit(store: Store, llm: LLM | None, proj: dict, sha: str, messages: list[str] | None = None,
                use_llm: bool = True) -> dict:
    """한 커밋 시점의 소스 기준으로 프로젝트 문서의 변경 제안을 만든다. 반환: 요약 dict."""
    root = Path(proj["local_path"])
    messages = messages or []
    sr_ids = sorted({int(m) for msg in messages for m in SR_REF.findall(msg) if store.get_sr(int(m))})
    audit_sr = sr_ids[0] if sr_ids else None
    if not proj["doc_paths"]:
        return {"created": 0, "note": "프로젝트에 갱신할 문서 경로가 설정되어 있지 않습니다."}
    if gitops.run(root, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode != 0:
        token = os.environ.get(proj["token_env"], "") if proj["token_env"] else ""
        gitops.run(root, "fetch", "-q", "origin", proj["default_branch"], env=gitops.auth_env(token),
                   secrets=[token], check=False, timeout=300)
        if gitops.run(root, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode != 0:
            msg = f"커밋 {sha[:8]} 이(가) 로컬 저장소에 없습니다. 로컬 clone 에서 git pull 후 다시 시도하세요."
            store.audit(audit_sr, "system", "doc.scan_failed", {"project": proj["name"], "commit": sha, "error": msg})
            raise gitops.GitError(msg)
    created, details = 0, []
    with gitops.worktree(root, sha) as wt:
        facts = collect_facts(wt)
        cfg = wt / "ssot.toml"
        aliases = load_aliases(cfg if cfg.exists() else None)
        for path in doc_files(proj):
            if not path.is_file():
                details.append(f"{path}: 문서를 찾을 수 없습니다.")
                continue
            try:
                doc = open_document(path)
                changes = sync_tables(doc, facts, aliases, llm if use_llm else None)
                if use_llm and llm:
                    changes += sync_narrative(doc, facts, llm)
            except Exception as e:  # noqa: BLE001 - 문서 하나가 깨져도 나머지는 계속
                details.append(f"{path.name}: 분석 실패 ({type(e).__name__}: {e})"[:300])
                continue
            if not changes:
                continue
            ser = [{"id": i, **asdict(c)} for i, c in enumerate(changes)]
            editable = sum(1 for c in changes if c.action in EDITABLE and c.op)
            summary = f"수정 가능 {editable}건 · 확인 필요 {len(changes) - editable}건"
            pid = store.create_doc_patch(proj["name"], sha, str(path), _sha(path), ser, sr_ids, summary)
            if pid is None:
                details.append(f"{path.name}: 이미 처리한 커밋입니다.")
                continue
            created += 1
            for sid in (sr_ids or [None]):
                store.audit(sid, "system", "doc.patch_proposed",
                            {"doc_patch_id": pid, "project": proj["name"], "commit": sha, "doc": str(path), "summary": summary})
    return {"created": created, "note": "; ".join(details)}


def handle_push(store: Store, llm: LLM | None, payload: dict) -> dict:
    """GitLab Push Event 처리. 기본 브랜치로 들어온 커밋만 대상으로 한다."""
    proj = match_project(store, payload)
    if not proj:
        return {"ignored": "등록되지 않은 프로젝트"}
    if payload.get("object_kind") not in (None, "push"):
        return {"ignored": f"push 이벤트가 아님: {payload.get('object_kind')}"}
    after = str(payload.get("checkout_sha") or payload.get("after") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", after) or set(after) == {"0"}:
        return {"ignored": "삭제된 브랜치이거나 커밋 정보가 없음"}
    if payload.get("ref") != f"refs/heads/{proj['default_branch']}":
        return {"ignored": f"기본 브랜치({proj['default_branch']})가 아님"}
    msgs = [str(c.get("message", "")) for c in payload.get("commits", []) if isinstance(c, dict)]
    return {"project": proj["name"], **scan_commit(store, llm, proj, after, msgs)}


def apply_doc_patch(store: Store, pid: int, actor: str, accepted: list[int]) -> Path:
    actor = actor.strip()
    if not actor:
        raise ValueError("승인자 이름이 필요합니다.")
    dp = store.get_doc_patch(pid)
    if not dp:
        raise LookupError("문서 변경 제안을 찾을 수 없습니다.")
    if dp["status"] != "pending":
        raise ValueError("이미 처리된 제안입니다.")
    path = Path(dp["doc_path"])
    if not path.is_file():
        raise ValueError(f"문서를 찾을 수 없습니다: {path}")
    if _sha(path) != dp["doc_sha"]:
        raise ValueError("제안을 만든 뒤 문서가 바뀌었습니다. 같은 커밋으로 다시 스캔해 새 제안을 받으세요.")
    chosen = [c for c in dp["changes"] if c["id"] in set(accepted) and c["action"] in EDITABLE and c.get("op")]
    if not chosen:
        raise ValueError("적용할 항목을 하나 이상 선택하세요.")
    doc = open_document(path)
    apply_ops(doc, [c["op"] for c in chosen])
    out = path.with_name(f"{path.stem}.updated{path.suffix}")
    n = 1
    while out.exists():
        n += 1
        out = path.with_name(f"{path.stem}.updated-{n}{path.suffix}")
    doc.save(out)
    store.update_doc_patch(pid, status="applied", output=str(out), decided_by=actor)
    for sid in (dp["sr_ids"] or [None]):
        store.audit(sid, actor, "doc.patch_applied",
                    {"doc_patch_id": pid, "doc": str(path), "output": str(out), "commit": dp["commit_sha"],
                     "changes": [{k: c[k] for k in ("action", "where", "key", "field", "old", "new")} for c in chosen]})
    return out


def dismiss_doc_patch(store: Store, pid: int, actor: str, reason: str) -> None:
    dp = store.get_doc_patch(pid)
    if not dp:
        raise LookupError("문서 변경 제안을 찾을 수 없습니다.")
    if dp["status"] != "pending":
        raise ValueError("이미 처리된 제안입니다.")
    if not actor.strip() or not reason.strip():
        raise ValueError("담당자 이름과 사유가 필요합니다.")
    store.update_doc_patch(pid, status="dismissed", decided_by=actor.strip())
    for sid in (dp["sr_ids"] or [None]):
        store.audit(sid, actor.strip(), "doc.patch_dismissed", {"doc_patch_id": pid, "reason": reason.strip()})
