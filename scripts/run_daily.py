#!/usr/bin/env python3
"""Research in isolation, publish through a resumable, hash-checked transaction."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
CONFIG = json.loads((ROOT / "config/site.json").read_text())
TZ = ZoneInfo(CONFIG["timezone"])


def command(args, *, cwd=None, timeout=300, capture=True, **kwargs):
    result = subprocess.run(args, cwd=cwd or ROOT, text=True, timeout=timeout,
                            capture_output=capture, **kwargs)
    if result.returncode:
        detail = (result.stderr or "")[-2000:] if capture else "see runtime log"
        raise RuntimeError(f"{args[0]} exited {result.returncode}: {detail}")
    return (result.stdout or "").strip()


def git(*args):
    return command(["git", "-c", "credential.helper=!gh auth git-credential", *args])


def issue_relative(day):
    return Path(day.strftime("%Y/%m/%Y-%m-%d.md"))


def atomic_bytes(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def atomic_text(path, text):
    atomic_bytes(path, text.encode("utf-8"))


def save_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def digest(path):
    if path.is_symlink():
        raise RuntimeError(f"Refusing symbolic link: {path}")
    if not path.exists():
        return None
    if not path.is_file():
        raise RuntimeError(f"Expected a regular file: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_workspace(work, day):
    work.mkdir(parents=True)
    (work / "AGENTS.md").write_text(
        "本任务仅做联网研究与 Markdown 编辑。遵守输入编辑规则。"
        "只在当前工作目录写入 issue.md 和必要临时材料。"
        "不操作账户、Git、系统设置或对外发送消息。\n", encoding="utf-8")
    history = work / "history"
    history.mkdir()
    earlier = sorted(p for p in (ROOT / "daily").glob("*/*/*.md")
                     if p.stem < day.isoformat())[-7:]
    for old in earlier:
        target = history / old.relative_to(ROOT / "daily")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(old, target)
    now = datetime.now(TZ).isoformat(timespec="seconds")
    prompt = (ROOT / "prompts/daily.md").read_text(encoding="utf-8")
    prompt += f"\n\n本轮任务参数：日报日期 {day.isoformat()}；时区 Asia/Shanghai；启动时间 {now}。"
    prompt += "检索截止时间不得晚于当前实际时间，正文写明实际核查时间。输出到 issue.md。\n"
    return prompt


def verify_research_events(path):
    """Check observed tool completion, not the truth or support of cited claims."""
    counts = {"search": 0, "page_action": 0, "opaque_non_search": 0}
    completed = False
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            event = json.loads(line)
            if event.get("type") in ("error", "turn.failed"):
                raise RuntimeError("Research log contains a failed turn or error")
            if event.get("type") == "turn.completed":
                completed = True
            item = event.get("item", {})
            if event.get("type") != "item.completed" or item.get("type") not in ("web_search", "web_search_call"):
                continue
            if item.get("status") not in (None, "completed"):
                continue
            action = item.get("action") or {}
            kind = action.get("type") if isinstance(action, dict) else action
            if kind == "search":
                counts["search"] += 1
            elif kind in ("open", "open_page", "find", "find_in_page"):
                counts["page_action"] += 1
            elif kind == "other":
                counts["opaque_non_search"] += 1
    if not completed or not counts["search"] or not (counts["page_action"] + counts["opaque_non_search"]):
        raise RuntimeError("Research requires a completed turn, completed search and completed non-search web action")
    counts["limitation"] = "仅证明工具完成调用；other 是不透明的非搜索操作，不能证明已读正文或引用支持论点。"
    return counts


def generate(work, day):
    prompt = prepare_workspace(work, day)
    args = ["codex", "--search", "exec", "--sandbox", "workspace-write",
            "-c", "sandbox_workspace_write.network_access=true",
            "-c", 'approval_policy="never"', "--skip-git-repo-check",
            "--ephemeral", "--color", "never", "--json",
            "--cd", str(work), "-o", str(work / "result.txt"), "-"]
    env = os.environ.copy()
    for key in ("CODEX_THREAD_ID", "CODEX_SESSION_ID"):
        env.pop(key, None)
    with (work / "events.jsonl").open("w") as out, (work / "stderr.log").open("w") as err:
        command(args, cwd=work, timeout=5400, capture=False,
                input=prompt, stdout=out, stderr=err, env=env)
    evidence = verify_research_events(work / "events.jsonl")
    save_json(work / "research-check.json", evidence)
    output = work / "issue.md"
    if not output.is_file() or output.is_symlink():
        raise RuntimeError("Generator did not produce a regular issue.md")
    if output.stat().st_size > 300_000:
        raise RuntimeError("Generated issue exceeds size limit")
    return output


def validate_candidate(output, day):
    from build_site import load_issue
    with tempfile.TemporaryDirectory(prefix="candidate-", dir=RUNTIME) as temp:
        source = Path(temp) / "daily"
        candidate = source / issue_relative(day)
        candidate.parent.mkdir(parents=True)
        shutil.copyfile(output, candidate)
        if load_issue(candidate, source) is None:
            raise RuntimeError("Generated issue is a draft")


def build(source, output):
    command([sys.executable, str(ROOT / "scripts/build_site.py"),
             "--source", str(source), "--output", str(output),
             "--base-url", CONFIG["base_url"]], timeout=300)


def changed_paths():
    tracked = git("diff", "--name-only", "--no-renames", "-z", "HEAD").split("\0")
    untracked = git("ls-files", "--others", "--exclude-standard", "-z").split("\0")
    return set(filter(None, tracked + untracked))


def require_clean(head=None):
    if head is not None and git("rev-parse", "HEAD") != head:
        raise RuntimeError("HEAD changed during research; preserve concurrent work and stop")
    if git("status", "--porcelain"):
        raise RuntimeError("Working tree has uncommitted changes; preserve them and stop")


def file_inventory(directory):
    if directory.is_symlink():
        raise RuntimeError(f"Refusing symbolic link directory: {directory}")
    paths = {}
    if directory.exists():
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise RuntimeError(f"Refusing symbolic link: {path}")
            if path.is_file():
                paths[path.relative_to(directory).as_posix()] = digest(path)
    return paths


def stage_transaction(day, candidate, base_head):
    transaction_id = uuid.uuid4().hex
    stage = RUNTIME / "transactions" / transaction_id
    stage.mkdir(parents=True)
    archive = stage / "daily"
    if (ROOT / "daily").exists():
        file_inventory(ROOT / "daily")
        shutil.copytree(ROOT / "daily", archive)
    target = archive / issue_relative(day)
    if candidate is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(candidate, target)
    build(archive, stage / "docs")
    # All parsing and rendering has succeeded before the real archive is changed.
    if base_head is not None:
        require_clean(base_head)
    old_docs = file_inventory(ROOT / "docs")
    new_docs = file_inventory(stage / "docs")
    paths = {"docs/" + name for name in old_docs.keys() | new_docs.keys()}
    paths.add("daily/" + issue_relative(day).as_posix())
    files = {}
    for relative in sorted(paths):
        before, after = digest(ROOT / relative), digest(stage / relative)
        if before != after:
            files[relative] = {"before": before, "after": after}
    manifest = {"version": 1, "id": transaction_id, "date": day.isoformat(),
                "base_head": base_head, "commit": None, "files": files}
    if base_head is not None:
        require_clean(base_head)
    save_json(stage / "manifest.json", manifest)
    # The durable marker is written before the first real publication file changes.
    if base_head is not None:
        save_json(RUNTIME / "pending-publication.json", {"id": transaction_id})
    return stage, manifest


def load_pending():
    pointer = RUNTIME / "pending-publication.json"
    if not pointer.exists():
        return None
    ident = json.loads(pointer.read_text())["id"]
    if len(ident) != 32 or any(c not in "0123456789abcdef" for c in ident):
        raise RuntimeError("Invalid pending transaction identifier")
    stage = RUNTIME / "transactions" / ident
    manifest = json.loads((stage / "manifest.json").read_text())
    if manifest["id"] != ident or manifest.get("version") != 1:
        raise RuntimeError("Invalid pending transaction manifest")
    expected_daily = "daily/" + issue_relative(date.fromisoformat(manifest["date"])).as_posix()
    for relative, hashes in manifest["files"].items():
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or not (relative.startswith("docs/") or relative == expected_daily):
            raise RuntimeError("Invalid pending publication path")
        if digest(stage / path) != hashes["after"]:
            raise RuntimeError(f"Staged publication changed: {relative}")
    return stage, manifest


def index_digest(relative):
    result = subprocess.run(["git", "show", ":" + relative], cwd=ROOT,
                            capture_output=True, timeout=30)
    if result.returncode:
        return None
    return hashlib.sha256(result.stdout).hexdigest()


def check_transaction(manifest, *, applied=False):
    files = manifest["files"]
    if manifest["base_head"] is not None:
        head = git("rev-parse", "HEAD")
        expected_head = manifest.get("commit") or manifest["base_head"]
        if head != expected_head:
            # Recover a kill immediately after git commit but before saving its ID.
            trailer = f"Decision-Daily-Transaction: {manifest['id']}"
            if (not manifest.get("commit") and git("rev-parse", "HEAD^") == manifest["base_head"]
                    and trailer in git("show", "-s", "--format=%B", "HEAD")
                    and set(filter(None, git("diff", "--name-only", "--no-renames", "-z", manifest["base_head"], "HEAD").split("\0"))) == set(files)):
                manifest["commit"] = head
            else:
                raise RuntimeError("HEAD changed outside this publication; preserve it and stop")
        unknown = changed_paths() - set(files)
        if unknown:
            raise RuntimeError("Concurrent user changes outside publication: " + ", ".join(sorted(unknown)))
        staged = set(filter(None, git("diff", "--cached", "--name-only", "-z").split("\0")))
        if staged - set(files):
            raise RuntimeError("Refusing unrelated staged changes")
        for relative in staged:
            allowed = {files[relative]["after"]} if applied else set(files[relative].values())
            if index_digest(relative) not in allowed:
                raise RuntimeError(f"Concurrent staged edits to {relative}; preserve them and stop")
    for relative, hashes in files.items():
        allowed = {hashes["after"]} if applied or manifest.get("commit") else set(hashes.values())
        if digest(ROOT / relative) not in allowed:
            raise RuntimeError(f"Concurrent edits to {relative}; preserve them and stop")


def apply_transaction(stage, manifest):
    check_transaction(manifest)
    for relative, hashes in manifest["files"].items():
        destination = ROOT / relative
        # Check each file again immediately before touching it.
        current = digest(destination)
        if current == hashes["after"]:
            continue
        if current != hashes["before"] or manifest.get("commit"):
            raise RuntimeError(f"Concurrent edits to {relative}; preserve them and stop")
        if hashes["after"] is None:
            destination.unlink()
        else:
            if digest(stage / relative) != hashes["after"]:
                raise RuntimeError(f"Staged publication changed: {relative}")
            atomic_bytes(destination, (stage / relative).read_bytes())
    check_transaction(manifest, applied=True)


def verify_pages(day, docs=None, attempts=40, delay=15):
    """Compare the actual reading page, front page and downloadable source."""
    import time
    import urllib.request
    docs = docs or ROOT / "docs"
    markdown = Path("daily") / issue_relative(day)
    html = markdown.with_suffix(".html")
    expected = {p.as_posix(): (docs / p).read_bytes() for p in (markdown, html, Path("index.html"))}
    deadline = time.monotonic() + max(60, attempts * delay)
    for attempt in range(attempts):
        matched = True
        for relative, content in expected.items():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("Pages verification deadline exceeded")
            try:
                request = urllib.request.Request(f"{CONFIG['base_url']}/{relative}",
                    headers={"Cache-Control": "no-cache", "User-Agent": "DecisionDaily/1.0"})
                with urllib.request.urlopen(request, timeout=min(20, remaining)) as response:
                    if response.read() != content:
                        matched = False
            except (OSError, ValueError):
                matched = False
        if matched:
            return f"{CONFIG['base_url']}/{html.as_posix()}"
        if attempt + 1 < attempts:
            time.sleep(min(delay, max(0, deadline - time.monotonic())))
    raise RuntimeError("Push completed, but published Markdown, reading page or homepage did not match")


def publish(stage, manifest):
    check_transaction(manifest, applied=True)
    if not manifest.get("commit"):
        files = sorted(manifest["files"])
        if files:
            git("add", "--", *files)
            check_transaction(manifest, applied=True)
            for relative, hashes in manifest["files"].items():
                if index_digest(relative) != hashes["after"]:
                    raise RuntimeError(f"Index does not match generated content: {relative}")
            git("commit", "-m", f"发布决策情报日报 {manifest['date']}",
                "-m", f"Decision-Daily-Transaction: {manifest['id']}")
        manifest["commit"] = git("rev-parse", "HEAD")
        save_json(stage / "manifest.json", manifest)
    require_clean(manifest["commit"])
    git("push", "origin", "main")
    url = verify_pages(date.fromisoformat(manifest["date"]), stage / "docs")
    (RUNTIME / "pending-publication.json").unlink()
    manifest["verified_url"] = url
    manifest["verified_at"] = datetime.now(TZ).isoformat()
    save_json(stage / "manifest.json", manifest)
    return url


def cleanup():
    cutoff = datetime.now(TZ) - timedelta(days=CONFIG["runtime_retention_days"])
    pending = load_pending()
    keep = pending[0] if pending else None
    for parent in (RUNTIME / "runs", RUNTIME / "transactions"):
        if parent.exists():
            for folder in parent.iterdir():
                if folder != keep and folder.is_dir() and not folder.is_symlink() and folder.stat().st_mtime < cutoff.timestamp():
                    shutil.rmtree(folder)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat)
    parser.add_argument("--publish-only", action="store_true", help="Publish an existing issue without generating")
    parser.add_argument("--no-push", action="store_true", help="Validate and build locally only")
    args = parser.parse_args()
    day = args.date or datetime.now(TZ).date()
    if day > datetime.now(TZ).date():
        parser.error("Cannot publish a future issue")
    RUNTIME.mkdir(exist_ok=True)
    with (RUNTIME / "daily.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another daily publication is already running; skipped.")
            return 0
        status = {"date": day.isoformat(), "started_at": datetime.now(TZ).isoformat(), "state": "running"}
        status_path = RUNTIME / "last-run.json"
        save_json(status_path, status)
        try:
            pending = load_pending()
            if args.no_push and pending:
                raise RuntimeError("Pending publication exists; resume normal publishing before a local preview")
            if not args.no_push:
                if git("branch", "--show-current") != "main":
                    raise RuntimeError("Scheduled publishing requires main branch")
                remote = git("remote", "get-url", "origin")
                if remote not in (f"https://github.com/{CONFIG['repository']}.git", f"git@github.com:{CONFIG['repository']}.git"):
                    raise RuntimeError("Unexpected publication repository")
                if pending:
                    stage, manifest = pending
                    apply_transaction(stage, manifest)
                    status["recovered_publication"] = manifest["date"]
                    status["verified_url"] = publish(stage, manifest)
                    if manifest["date"] == day.isoformat():
                        status.update(state="published", commit=manifest["commit"], reused_existing_issue=True)
                require_clean()
                git("pull", "--ff-only", "origin", "main")
            if status["state"] != "published":
                base_head = None if args.no_push else git("rev-parse", "HEAD")
                target = ROOT / "daily" / issue_relative(day)
                status["reused_existing_issue"] = target.exists()
                output = None
                if not target.exists():
                    if args.publish_only:
                        raise RuntimeError("No existing issue for --publish-only")
                    work = RUNTIME / "runs" / (datetime.now(TZ).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
                    output = generate(work, day)
                    validate_candidate(output, day)
                    status["research_check"] = json.loads((work / "research-check.json").read_text())
                stage, manifest = stage_transaction(day, output, base_head)
                apply_transaction(stage, manifest)
                if args.no_push:
                    status["state"] = "built"
                else:
                    status["verified_url"] = publish(stage, manifest)
                    status.update(state="published", commit=manifest["commit"])
            status["finished_at"] = datetime.now(TZ).isoformat()
            save_json(status_path, status)
            cleanup()
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return 0
        except Exception as error:
            status.update(state="failed", error=str(error), finished_at=datetime.now(TZ).isoformat())
            save_json(status_path, status)
            print(json.dumps(status, ensure_ascii=False, indent=2), file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
