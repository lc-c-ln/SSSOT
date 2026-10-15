"""커밋된 SR 브랜치를 GitLab 에 push 하고 Merge Request 를 만든다. 토큰은 환경변수에서만 읽고 로그/오류에서 가린다."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlparse

from . import gitops
from .store import Store


class PublishError(RuntimeError):
    pass


def _api_target(gitlab_url: str) -> tuple[str, str]:
    u = urlparse(gitlab_url)
    path = u.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not u.netloc or not path:
        raise PublishError("GitLab 주소에서 프로젝트 경로를 알 수 없습니다. 예: https://gitlab.company.com/group/project")
    return f"{u.scheme}://{u.netloc}", path


def create_mr(gitlab_url: str, token: str, source: str, target: str, title: str, description: str,
              timeout: int = 30) -> dict:
    base, path = _api_target(gitlab_url)
    body = json.dumps({"source_branch": source, "target_branch": target, "title": title,
                       "description": description, "remove_source_branch": True}).encode("utf-8")
    req = urllib.request.Request(f"{base}/api/v4/projects/{quote(path, safe='')}/merge_requests", data=body, method="POST",
                                 headers={"PRIVATE-TOKEN": token, "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = gitops.scrub(e.read().decode("utf-8", "replace")[:300], [token])
        hint = {401: " (토큰이 올바르지 않습니다)", 403: " (토큰 권한 부족: api, write_repository 필요)",
                404: " (프로젝트 경로 또는 토큰 접근 권한을 확인하세요)"}.get(e.code, "")
        raise PublishError(f"GitLab MR 생성 실패 {e.code}{hint}: {detail}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise PublishError(f"GitLab 에 연결할 수 없습니다: {gitops.scrub(str(e), [token])}") from e


def publish(store: Store, sr_id: int, actor: str) -> str:
    """committed 상태의 최신 수정안을 push + MR 생성. 반환: MR URL. 실패해도 로컬 브랜치/커밋은 그대로 남는다."""
    sr = store.get_sr(sr_id)
    proj = store.get_project(sr["project"]) if sr else None
    patch = next((p for p in reversed(store.list_patches(sr_id)) if p["status"] in ("committed", "published")), None)
    if not (sr and proj and patch):
        raise PublishError("커밋된 수정안이 없습니다.")
    if patch["mr_url"]:
        return patch["mr_url"]
    if not proj["gitlab_url"]:
        raise PublishError("프로젝트에 GitLab 주소가 설정되어 있지 않습니다.")
    token = os.environ.get(proj["token_env"], "") if proj["token_env"] else ""
    if not token:
        raise PublishError(f"토큰 환경변수({proj['token_env'] or '미지정'})가 설정되어 있지 않습니다. 설정 후 다시 시도하세요.")
    root = Path(proj["local_path"])
    try:
        gitops.run(root, "push", "origin", f"{patch['branch']}:{patch['branch']}", env=gitops.auth_env(token),
                   secrets=[token], timeout=300)
    except gitops.GitError as e:
        store.audit(sr_id, "system", "gitlab.push_failed", {"patch_id": patch["id"], "error": str(e)})
        raise PublishError(str(e)) from e
    desc = (f"{patch['explanation']}\n\n---\n- SR: SR-{sr['id']:04d} {sr['title']}\n- 요청자: {sr['requester']}\n"
            f"- 승인자: {patch['decided_by']}\n- 변경: {', '.join(patch['stats'].get('files', []))}\n\n"
            "로컬 LLM이 만든 수정안을 담당자가 검토·승인했습니다. 머지 전 GitLab 리뷰 절차를 따르세요.")
    try:
        mr = create_mr(proj["gitlab_url"], token, patch["branch"], proj["default_branch"],
                       f"[SR-{sr['id']:04d}] {sr['title']}", desc)
    except PublishError as e:
        store.audit(sr_id, "system", "gitlab.mr_failed", {"patch_id": patch["id"], "error": str(e)})
        raise
    url = mr.get("web_url", "")
    store.update_patch(patch["id"], status="published", mr_url=url, error="")
    store.update_sr(sr_id, status="mr_created", reason=f"MR !{mr.get('iid', '?')} 생성됨")
    store.audit(sr_id, actor or "system", "gitlab.mr_created", {"patch_id": patch["id"], "mr_iid": mr.get("iid"),
                                                              "url": url, "branch": patch["branch"]})
    return url
