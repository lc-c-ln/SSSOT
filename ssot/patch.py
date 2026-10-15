"""수정안 생성·검증·승인 처리.

작은 로컬 모델이 unified diff 의 줄 번호를 틀리는 문제를 피하려고, 모델에게는 "원본에서 복사한 old -> new" 편집만 받고
실제 diff 는 코드(difflib)가 만든다. 모든 편집은 원본에서 위치를 확인하고, 최종 diff 는 git apply --check 로 검증한다.
"""
from __future__ import annotations

import difflib
import json
import os
import re
from pathlib import Path

from . import gitops
from .java_analyzer import mask
from .llm import LLM, extract_json
from .store import Store

PROMPT_VERSION = "patch-v1"
WHOLE_FILE_CHARS = 14000
MAX_EDITS_PER_FILE = 8

SYSTEM = """당신은 Java/Spring 개발자입니다. 요청을 만족하도록 주어진 파일을 최소한으로 수정하세요.
반드시 JSON 한 개만 출력하세요:
{"explanation":"수정 이유 한두 문장(한국어)","edits":[{"old":"수정 전 코드","new":"수정 후 코드"}]}
규칙:
- old 는 파일에 있는 코드를 한 글자도 바꾸지 말고 그대로 복사한 연속된 줄이어야 하며, 파일 안에서 한 번만 나타날 만큼 충분히 길게 쓰세요.
- new 는 old 를 대체할 전체 코드입니다. 들여쓰기를 유지하세요.
- 요청과 무관한 리팩터링·포맷 변경은 하지 마세요.
- 이 파일을 수정할 필요가 없으면 edits 를 빈 배열로 두세요."""


class PatchError(ValueError):
    pass


def _window(text: str, lines_hint: list[int]) -> str:
    """큰 파일은 후보 위치 주변만 보여준다. 작은 파일은 전체."""
    if len(text) <= WHOLE_FILE_CHARS or not lines_hint:
        return text[:WHOLE_FILE_CHARS * 3]
    lines = text.splitlines()
    keep: set[int] = set(range(0, min(len(lines), 30)))          # 파일 머리(패키지/import)
    for ln in lines_hint:
        keep |= set(range(max(0, ln - 16), min(len(lines), ln + 45)))
    out, prev = [], -1
    for i in sorted(keep):
        if prev >= 0 and i != prev + 1:
            out.append("// ... (중략) ...")
        out.append(lines[i])
        prev = i
    return "\n".join(out)


def _locate(lines: list[str], old: list[str]) -> list[int]:
    """old 줄들이 연속으로 나타나는 시작 위치(들). 먼저 정확히, 없으면 공백 차이를 무시하고 찾는다."""
    n = len(old)
    for norm in (lambda s: s, lambda s: s.strip()):
        o = [norm(x) for x in old]
        hits = [i for i in range(len(lines) - n + 1) if [norm(x) for x in lines[i:i + n]] == o]
        if hits:
            return hits
    return []


def apply_edits(original: str, edits: list[dict]) -> tuple[str, list[str]]:
    """편집을 순서대로 적용한다. 반환: (새 텍스트, 문제 목록). CRLF 파일도 지원."""
    eol = "\r\n" if "\r\n" in original else "\n"
    text = original.replace("\r\n", "\n")
    problems: list[str] = []
    for n, e in enumerate(edits[:MAX_EDITS_PER_FILE], 1):
        old, new = str(e.get("old", "")).replace("\r\n", "\n").strip("\n"), str(e.get("new", "")).replace("\r\n", "\n").strip("\n")
        if not old.strip():
            problems.append(f"편집 {n}: old 가 비어 있어 무시했습니다.")
            continue
        if old == new:
            continue
        lines, old_l = text.split("\n"), old.split("\n")
        hits = _locate(lines, old_l)
        if not hits:
            problems.append(f"편집 {n}: 원본에서 해당 코드를 찾지 못해 무시했습니다.")
            continue
        if len(hits) > 1:
            problems.append(f"편집 {n}: 같은 코드가 {len(hits)}곳에 있어 위치를 정할 수 없어 무시했습니다.")
            continue
        i = hits[0]
        new_l = new.split("\n") if new else []
        # 공백 무시로 찾았을 때 들여쓰기 차이를 보정
        want, have = re.match(r"\s*", old_l[0]).group(), re.match(r"\s*", lines[i]).group()
        if want != have and all(l.startswith(want) or not l.strip() for l in new_l):
            new_l = [have + l[len(want):] if l.strip() else l for l in new_l]
        text = "\n".join(lines[:i] + new_l + lines[i + len(old_l):])
    return text.replace("\n", eol) if eol == "\r\n" else text, problems


def make_diff(path: str, old: str, new: str) -> str:
    out: list[str] = []
    for line in difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                     f"a/{path}", f"b/{path}", n=3):
        out.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(out)


def _brace_balance(src: str) -> tuple[int, int, int]:
    m = mask(src)[0]
    return m.count("{") - m.count("}"), m.count("(") - m.count(")"), m.count("[") - m.count("]")


def _stats(diff: str) -> dict:
    files = re.findall(r"^\+\+\+ b/(.+)$", diff, re.M)
    add = len(re.findall(r"^\+(?!\+\+)", diff, re.M))
    dele = len(re.findall(r"^-(?!--)", diff, re.M))
    return {"files": files, "additions": add, "deletions": dele}


def generate_patch(store: Store, llm: LLM, sr_id: int, feedback: str = "") -> int:
    """선택된 파일마다 수정안을 만들어 sr_patch 에 저장한다. 반환: patch id."""
    sr = store.get_sr(sr_id)
    imp = store.get_impact(sr_id)
    proj = store.get_project(sr["project"]) if sr else None
    if not (sr and imp and proj and imp["selected"]):
        raise PatchError("영향 분석에서 수정 대상 파일을 확정한 뒤에 수정안을 만들 수 있습니다.")
    root = Path(proj["local_path"])
    pid = store.create_patch(sr_id, feedback)
    store.update_sr(sr_id, status="patch_generating")
    hints = {c["file"]: [k["line"] for k in c["chunks"]] for c in imp["candidates"]}
    prev = next((p for p in reversed(store.list_patches(sr_id)) if p["id"] != pid and p["diff"]), None)
    diffs, explanations, warnings, llm_log = [], [], [], []
    try:
        with gitops.worktree(root, proj["default_branch"]) as wt:
            for rel in imp["selected"]:
                f = wt / rel
                if not f.is_file():
                    warnings.append(f"{rel}: 기본 브랜치({proj['default_branch']})에 없는 파일이라 건너뜁니다.")
                    continue
                try:
                    original = f.read_bytes().decode("utf-8")
                except UnicodeDecodeError:
                    warnings.append(f"{rel}: UTF-8 이 아닌 파일이라 건너뜁니다.")
                    continue
                user = f"[요청 제목]\n{sr['title']}\n\n[요청 내용]\n{sr['body']}\n"
                if feedback:
                    user += f"\n[담당자 피드백 - 반드시 반영]\n{feedback}\n"
                    if prev:
                        user += f"\n[이전 수정안]\n{prev['diff'][:4000]}\n"
                user += f"\n[파일: {rel}]\n```java\n{_window(original.replace(chr(13) + chr(10), chr(10)), hints.get(rel, []))}\n```"
                try:
                    raw = llm.complete(SYSTEM, user, max_tokens=3000, json_mode=True)
                    data = extract_json(raw)
                except Exception as e:  # noqa: BLE001 - 파일 하나 실패가 전체를 막지 않게
                    llm_log.append({"file": rel, "error": f"{type(e).__name__}: {e}"})
                    warnings.append(f"{rel}: 모델 응답 오류로 건너뜁니다 ({e})"[:300])
                    continue
                llm_log.append({"file": rel, "user": user, "response": raw})
                edits = data.get("edits") if isinstance(data.get("edits"), list) else []
                new, problems = apply_edits(original, [e for e in edits if isinstance(e, dict)])
                warnings += [f"{rel}: {p}" for p in problems]
                if new == original:
                    continue
                if rel.endswith(".java") and _brace_balance(original) != _brace_balance(new):
                    warnings.append(f"{rel}: 수정 후 괄호 짝이 맞지 않을 수 있습니다. 꼼꼼히 확인하세요.")
                diffs.append(make_diff(rel, original, new))
                if data.get("explanation"):
                    explanations.append(str(data["explanation"])[:400])
            diff = "".join(diffs)
            if diff:
                r = gitops.run(wt, "apply", "--check", "-", input=diff.encode("utf-8"), check=False)
                if r.returncode != 0:
                    err = (r.stderr or b"").decode("utf-8", "replace")[:300]
                    store.audit(sr_id, "system", "patch.invalid", {"patch_id": pid, "error": err, "diff": diff[:6000]})
                    store.update_patch(pid, status="failed", error=f"git apply 검증 실패: {err}", warnings=warnings)
                    store.update_sr(sr_id, status="patch_failed", reason="생성된 수정안이 저장소에 적용되지 않습니다.")
                    return pid
    except gitops.GitError as e:
        store.audit(sr_id, "system", "patch.error", {"patch_id": pid, "error": str(e)})
        store.update_patch(pid, status="failed", error=str(e))
        store.update_sr(sr_id, status="patch_failed", reason=str(e)[:300])
        return pid
    store.audit(sr_id, "system", "patch.generated",
                {"patch_id": pid, "prompt_version": PROMPT_VERSION, "model": getattr(llm, "model", "?"),
                 "feedback": feedback, "llm": llm_log, "diff": diff[:20000], "warnings": warnings})
    if not diff:
        store.update_patch(pid, status="failed", error="모델이 적용 가능한 수정안을 만들지 못했습니다.", warnings=warnings)
        store.update_sr(sr_id, status="patch_failed", reason="적용 가능한 수정안이 없습니다. 피드백을 적어 다시 요청하거나 대상 파일을 조정하세요.")
        return pid
    store.update_patch(pid, status="proposed", explanation=" ".join(dict.fromkeys(explanations)), diff=diff,
                       stats=_stats(diff), warnings=warnings)
    store.update_sr(sr_id, status="patch_ready", reason="수정안 승인 대기")
    return pid


def _msg(sr: dict, patch: dict, approver: str) -> str:
    return (f"[SR-{sr['id']:04d}] {sr['title']}\n\n{patch['explanation']}\n\n"
            f"SR: SR-{sr['id']:04d}\nRequested-by: {sr['requester']}\nApproved-by: {approver}\n"
            f"Generated-by: SSSOT local LLM (reviewed and approved by a human)")


def commit_patch(store: Store, sr: dict, proj: dict, patch: dict, approver: str) -> tuple[str, str]:
    """승인된 diff 를 새 브랜치에 커밋한다. 반환: (브랜치, 커밋 sha). 사용자의 작업 폴더는 건드리지 않는다."""
    root = Path(proj["local_path"])
    branch = gitops.unique_branch(root, f"sr/{sr['id']}-{gitops.slugify(sr['title'])}")
    email = os.environ.get("SSOT_GIT_EMAIL", "ssot@localhost")
    with gitops.worktree(root, proj["default_branch"], new_branch=branch) as wt:
        gitops.run(wt, "apply", "-", input=patch["diff"].encode("utf-8"))
        gitops.run(wt, "add", "-A")
        env = {"GIT_AUTHOR_NAME": approver, "GIT_AUTHOR_EMAIL": email,
               "GIT_COMMITTER_NAME": "SSSOT", "GIT_COMMITTER_EMAIL": email}
        gitops.run(wt, "commit", "-q", "--no-verify", "-m", _msg(sr, patch, approver), env=env)
        sha = gitops.run(wt, "rev-parse", "HEAD").stdout.decode().strip()
    return branch, sha


def decide(store: Store, sr_id: int, patch_id: int, actor: str, decision: str, note: str = "",
           allow_self_approve: bool = False) -> str:
    """승인/수정 요구/반려. 반환: 'approved' | 'revise' | 'rejected'. 수정 요구 시 재생성은 호출자가 이어서 수행."""
    actor, note = actor.strip(), note.strip()
    if not actor:
        raise ValueError("승인자 이름이 필요합니다.")
    sr = store.get_sr(sr_id)
    patch = store.get_patch(patch_id)
    if not sr or not patch or patch["sr_id"] != sr_id:
        raise LookupError("수정안을 찾을 수 없습니다.")
    latest = store.list_patches(sr_id)[-1]
    if patch["status"] != "proposed" or latest["id"] != patch_id:
        raise ValueError("현재 승인 대기 중인 최신 수정안만 처리할 수 있습니다.")
    if decision == "approve":
        if actor == sr["requester"] and not allow_self_approve:
            raise ValueError("요청자 본인은 승인할 수 없습니다. 다른 담당자가 승인해야 합니다.")
        proj = store.get_project(sr["project"])
        try:
            branch, sha = commit_patch(store, sr, proj, patch, actor)
        except gitops.GitError as e:
            store.audit(sr_id, "system", "patch.commit_failed", {"patch_id": patch_id, "error": str(e)})
            raise ValueError(f"커밋에 실패했습니다: {e}") from e
        store.update_patch(patch_id, status="committed", decided_by=actor, decision_note=note, branch=branch, commit_sha=sha)
        store.update_sr(sr_id, status="committed", reason=f"브랜치 {branch} 에 커밋됨")
        store.audit(sr_id, actor, "patch.approved", {"patch_id": patch_id, "version": patch["version"], "note": note,
                                                    "branch": branch, "commit": sha, "diff_sha256": _sha(patch["diff"])})
        return "approved"
    if decision == "revise":
        if not note:
            raise ValueError("수정 요구 내용을 적어 주세요.")
        store.update_patch(patch_id, status="superseded", decided_by=actor, decision_note=note)
        store.audit(sr_id, actor, "patch.revise_requested", {"patch_id": patch_id, "comment": note})
        return "revise"
    if decision == "reject":
        if not note:
            raise ValueError("반려 사유를 적어 주세요.")
        store.update_patch(patch_id, status="rejected", decided_by=actor, decision_note=note)
        store.update_sr(sr_id, status="rejected", reason=f"반려: {note}"[:300])
        store.audit(sr_id, actor, "patch.rejected", {"patch_id": patch_id, "reason": note})
        return "rejected"
    raise ValueError("decision 은 approve / revise / reject 중 하나여야 합니다.")


def _sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
