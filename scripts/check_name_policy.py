#!/usr/bin/env python3
"""Enforce the repository's single-location legacy-name policy."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


RESERVED = "bili" + "pod"
ACKNOWLEDGEMENT = (
    "- ["
    + RESERVED.capitalize()
    + "](https://github.com/sunrisewestern/"
    + RESERVED
    + ")"
    + "：感谢原作者及贡献者以 GPLv3 提供基础实现；本仓库来源基线为提交 "
    + "`d16ce56604d1fbe3b0504ce2db964b0e29ffd9f0`，其后进行了项目重命名、配置迁移、安全、发布与部署方面的派生修改。"
)


class PolicyViolation(RuntimeError):
    pass


def git(*args: str, text: bool = False) -> bytes | str:
    return subprocess.check_output(["git", *args], text=text)


def scan_name(value: str, location: str) -> None:
    if RESERVED.casefold() in value.casefold():
        raise PolicyViolation(f"reserved name in {location}")


def scan_content(payload: bytes, logical_path: str, location: str) -> None:
    needle = RESERVED.encode("ascii")
    if needle not in payload.lower():
        return
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise PolicyViolation(f"reserved name in binary content: {location}") from exc
    matches = [(number, line) for number, line in enumerate(lines, 1) if RESERVED.casefold() in line.casefold()]
    allowed_readme = PurePosixPath(logical_path).name == "README.md"
    for number, line in matches:
        if not (allowed_readme and line == ACKNOWLEDGEMENT):
            raise PolicyViolation(f"reserved name in {location}:{number}")
    if len(matches) != 1:
        raise PolicyViolation(f"reserved name must occur exactly once in {location}")


def scan_treeish(treeish: str) -> None:
    entries = git("ls-tree", "-r", "-z", "--full-tree", treeish).split(b"\0")
    for entry in entries:
        if not entry:
            continue
        metadata, raw_path = entry.split(b"\t", 1)
        path = raw_path.decode("utf-8", "surrogateescape")
        scan_name(path, f"filename at {treeish}: {path}")
        object_id = metadata.rsplit(b" ", 1)[1].decode("ascii")
        scan_content(git("cat-file", "blob", object_id), path, f"{treeish}:{path}")


def scan_worktree() -> None:
    tracked = git("ls-files", "-z").split(b"\0")
    untracked = git("ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
    paths = tracked + untracked
    for raw_path in paths:
        if not raw_path:
            continue
        path = raw_path.decode("utf-8", "surrogateescape")
        scan_name(path, f"filename: {path}")
        scan_content(Path(path).read_bytes(), path, path)


def scan_commits(base: str, head: str) -> None:
    revision = head if set(base) == {"0"} else f"{base}..{head}"
    commits = git("rev-list", "--reverse", revision, text=True).splitlines()
    if not commits:
        scan_treeish(head)
        return
    for commit in commits:
        message = git("show", "-s", "--format=%B", commit, text=True)
        scan_name(message, f"commit message {commit}")
        scan_treeish(commit)


def scan_refs() -> None:
    rows = git("for-each-ref", "--format=%(refname)%00%(contents)%00", "refs/heads", "refs/tags").split(b"\0")
    for index in range(0, len(rows) - 1, 2):
        refname = rows[index].decode("utf-8", "replace")
        message = rows[index + 1].decode("utf-8", "replace")
        scan_name(refname, f"ref name {refname}")
        if refname.startswith("refs/tags/"):
            scan_name(message, f"tag message {refname}")


def scan_archive(path: Path) -> None:
    if tarfile.is_tarfile(path):
        with tarfile.open(path) as archive:
            for member in archive.getmembers():
                scan_name(member.name, f"archive member in {path.name}: {member.name}")
                if member.isfile():
                    extracted = archive.extractfile(member)
                    if extracted is not None:
                        scan_content(extracted.read(), member.name, f"{path.name}:{member.name}")
        return
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                scan_name(member.filename, f"archive member in {path.name}: {member.filename}")
                if not member.is_dir():
                    scan_content(archive.read(member), member.filename, f"{path.name}:{member.filename}")
        return
    scan_content(path.read_bytes(), path.name, path.name)


def scan_assets(paths: list[str]) -> None:
    for raw_path in paths:
        path = Path(raw_path)
        scan_name(path.name, f"release asset name {path.name}")
        scan_archive(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("worktree")
    commits = subparsers.add_parser("commits")
    commits.add_argument("--base", required=True)
    commits.add_argument("--head", required=True)
    subparsers.add_parser("refs")
    assets = subparsers.add_parser("assets")
    assets.add_argument("paths", nargs="+")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "worktree":
            scan_worktree()
        elif args.command == "commits":
            scan_commits(args.base, args.head)
        elif args.command == "refs":
            scan_refs()
        else:
            scan_assets(args.paths)
    except (OSError, subprocess.CalledProcessError, PolicyViolation) as exc:
        print(f"name policy failed: {exc}", file=sys.stderr)
        return 1
    print("name policy passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
