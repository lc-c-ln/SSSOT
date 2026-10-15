"""CLI: python -m ssot analyze|sync"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

from .docs import open_document
from .facts import collect_facts
from .llm import make_llm
from .sync import Change, sync_narrative, sync_tables
from .tablemap import load_aliases

ACTION_LABEL = {
    "update": "수정", "fill": "채움", "add": "추가", "missing": "소스에 없음",
    "review": "확인 필요", "narrative": "본문 수정",
}


def render_report(results: list[tuple[str, list[Change]]]) -> str:
    out = ["# 문서 동기화 리포트", ""]
    for name, changes in results:
        out.append(f"## {name}")
        if not changes:
            out += ["", "변경 사항 없음 (소스와 일치)", ""]
            continue
        counts: dict[str, int] = {}
        for c in changes:
            counts[c.action] = counts.get(c.action, 0) + 1
        out += ["", " · ".join(f"{ACTION_LABEL[k]} {v}" for k, v in counts.items()), "",
                "| 구분 | 위치 | 대상 | 항목 | 이전 | 이후 | 근거/비고 |", "|---|---|---|---|---|---|---|"]
        for c in changes:
            cell = lambda s: (s or "").replace("|", "\\|").replace("\n", " ")
            note = " ".join(x for x in (c.source, c.note) if x)
            out.append(f"| {ACTION_LABEL[c.action]} | {cell(c.where)} | {cell(c.key)} | {cell(c.field)} | "
                       f"{cell(c.old)} | {cell(c.new)} | {cell(note)} |")
        out.append("")
    return "\n".join(out)


def cmd_analyze(a) -> int:
    facts = collect_facts(Path(a.src))
    if a.json:
        print(json.dumps([asdict(f) for f in facts], ensure_ascii=False, indent=2))
        return 0
    for f in facts:
        print(f"[{f.kind}] {f.label}  ({f.source})")
    print(f"\n총 {len(facts)}건")
    return 0


def cmd_sync(a) -> int:
    src = Path(a.src)
    if not src.exists():
        print(f"소스 경로가 없습니다: {src}", file=sys.stderr)
        return 2
    facts = collect_facts(src)
    cfg = Path(a.config) if a.config else (src / "ssot.toml")
    aliases = load_aliases(cfg if cfg.exists() else None)
    llm = make_llm(a.provider, a.model, a.base_url) if a.llm else None
    out_dir = Path(a.out_dir) if a.out_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    results: list[tuple[str, list[Change]]] = []
    for d in a.doc:
        path = Path(d)
        doc = open_document(path)
        changes = sync_tables(doc, facts, aliases, llm, strict=a.strict)
        if llm:
            changes += sync_narrative(doc, facts, llm)
        results.append((path.name, changes))
        edits = [c for c in changes if c.action in ("update", "fill", "add", "narrative")]
        if a.apply and edits:
            if a.in_place:
                shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
                target = path
            else:
                target = (out_dir or path.parent) / f"{path.stem}.updated{path.suffix}"
            doc.save(target)
            print(f"저장: {target}")
    report = render_report(results)
    if a.report:
        Path(a.report).write_text(report, encoding="utf-8")
        print(f"리포트: {a.report}")
    else:
        print(report)
    if not a.apply:
        print("(미리보기 모드: 문서는 수정되지 않았습니다. --apply 로 저장)")
    return 1 if a.check and any(c.action in ("update", "fill", "add", "missing", "narrative") for _, cs in results for c in cs) else 0


def cmd_web(a) -> int:
    from .web import serve
    serve(a.host, a.port)
    return 0


def cmd_sr(a) -> int:
    from .sr_web import serve
    serve(a.db, a.host, a.port, a.ollama_host, a.model, mask=not a.no_mask, allow_self_approve=a.allow_self_approve, insecure_no_auth=a.insecure_no_auth)
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="ssot", description="Spring 소스 기준 문서 자동 갱신")
    sub = ap.add_subparsers(dest="cmd", required=True)
    an = sub.add_parser("analyze", help="소스에서 추출한 사실 목록 출력")
    an.add_argument("src")
    an.add_argument("--json", action="store_true")
    an.set_defaults(fn=cmd_analyze)
    sy = sub.add_parser("sync", help="문서를 소스와 대조해 갱신")
    sy.add_argument("--src", required=True, help="Spring 프로젝트 루트")
    sy.add_argument("--doc", required=True, nargs="+", help="docx/md/xlsx 문서")
    sy.add_argument("--apply", action="store_true", help="실제로 저장 (기본은 미리보기)")
    sy.add_argument("--in-place", action="store_true", help="원본 덮어쓰기 (.bak 백업 생성)")
    sy.add_argument("--out-dir", help="결과 저장 폴더 (기본: 원본과 같은 폴더, *.updated.*)")
    sy.add_argument("--report", help="리포트를 파일(md)로 저장")
    sy.add_argument("--llm", action="store_true", help="LLM 으로 신규 설명 생성·서술 본문 점검 (기본: 로컬 Ollama)")
    sy.add_argument("--provider", choices=["ollama", "openai", "anthropic"], default="ollama",
                    help="ollama/openai = OpenAI 호환 API (Ollama·LM Studio·Groq 등), anthropic = Claude")
    sy.add_argument("--model", help="모델명 (기본 SSOT_MODEL 또는 qwen2.5:7b)")
    sy.add_argument("--base-url", help="OpenAI 호환 주소 (기본 http://localhost:11434/v1)")
    sy.add_argument("--strict", action="store_true", help="목록형 칸(메서드/필드/파라미터)을 집합 일치로 엄격 비교")
    sy.add_argument("--check", action="store_true", help="불일치가 있으면 종료코드 1 (CI 용)")
    sy.add_argument("--config", help="ssot.toml 경로 (헤더 별칭 확장)")
    sy.set_defaults(fn=cmd_sync)
    wb = sub.add_parser("web", help="웹 UI 실행 (로컬)")
    wb.add_argument("--host", default="127.0.0.1")
    wb.add_argument("--port", type=int, default=8765)
    wb.set_defaults(fn=cmd_web)
    sr = sub.add_parser("sr", help="SR 접수 서버 실행 (로컬 Ollama 분류 + 감사 로그)")
    sr.add_argument("--db", default="ssot_sr.db", help="SQLite 파일 (기본 ssot_sr.db)")
    sr.add_argument("--host", default="127.0.0.1")
    sr.add_argument("--port", type=int, default=8766)
    sr.add_argument("--ollama-host", help="기본 OLLAMA_HOST 또는 http://localhost:11434")
    sr.add_argument("--model", help="분류에 쓸 모델 (기본 SSOT_MODEL 또는 qwen2.5-coder:7b)")
    sr.add_argument("--no-mask", action="store_true", help="감사 로그의 비밀값 마스킹 끄기")
    sr.add_argument("--allow-self-approve", action="store_true", help="요청자 본인의 승인을 허용 (기본: 금지)")
    sr.add_argument("--insecure-no-auth", action="store_true", help="외부 주소로 열면서 로그인 없이 실행(비권장, 사내 테스트용)")
    sr.set_defaults(fn=cmd_sr)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
