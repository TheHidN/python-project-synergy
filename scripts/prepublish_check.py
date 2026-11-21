#!/usr/bin/env python3
"""Pre-publication safety check: scan the files (and optionally the full git history) for secrets, private
identifiers and files that must never be published. It prints rule names and locations, never the matched text.

Usage: prepublish_check.py [--repo PATH] [--history]
A line containing "prepublish: ignore" is skipped (for deliberate fake data in test fixtures).
Environment: PREPUBLISH_IDENTIFIERS = comma-separated extra words that must not appear (the current user name and
host name are always checked).
"""

from __future__ import annotations

import argparse
import fnmatch
import getpass
import os
import re
import socket
import subprocess
import sys

PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
LITERAL_SECRET = re.compile(
    r"""(?i)\b(?:pass(?:word)?|passwd|secret|api[_-]?key|token|auth\w*)\w*\s*[:=]\s*["'][^"'\s$<{]{12,}["']""")
TOKEN_SHAPES = re.compile(r"\b(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}"
                          r"|sk-[A-Za-z0-9]{20,})\b")
IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
ALLOWED_IP = re.compile(r"^(?:127\.|0\.0\.0\.0$|255\.|192\.0\.2\.|198\.51\.100\.|203\.0\.113\.)")
ALLOWED_EMAIL_DOMAIN = re.compile(r"(?:^|\.)(?:example\.(?:com|org|net)|noreply\.github\.com)$", re.I)
FORBIDDEN_FILES = ["*.db", "*.db-wal", "*.db-shm", "*.sqlite*", "*.token", "*.pem", "*.key", ".env", ".env.*",
                   "id_rsa*", "id_ed25519*", "settings.local.json"]
FORBIDDEN_DIRS = {"tokens", "packets", "launchers"}
IGNORE_MARK = "prepublish: ignore"  # a test fixture may carry this marker on a line that holds deliberate fake data


def identifiers() -> list[str]:
    words = {getpass.getuser(), socket.gethostname().split(".")[0]}
    words.update(w.strip() for w in os.environ.get("PREPUBLISH_IDENTIFIERS", "").split(",") if w.strip())
    return sorted(w for w in words if len(w) >= 4)


def scan_line(line: str, words: list[str]) -> list[str]:
    if IGNORE_MARK in line:
        return []
    rules = []
    if PRIVATE_KEY.search(line):
        rules.append("private-key")
    if LITERAL_SECRET.search(line):
        rules.append("literal-secret")
    if TOKEN_SHAPES.search(line):
        rules.append("token-shape")
    if any(not ALLOWED_IP.match(m.group(0)) for m in IPV4.finditer(line)):
        rules.append("ip-address")
    if any(not ALLOWED_EMAIL_DOMAIN.search(m.group(0).split("@", 1)[1]) for m in EMAIL.finditer(line)):
        rules.append("email")
    lowered = line.lower()
    if any(w.lower() in lowered for w in words):
        rules.append("identifier")
    return rules


def forbidden_file(path: str) -> bool:
    parts = path.split("/")
    if any(p in FORBIDDEN_DIRS for p in parts[:-1]):
        return True
    return any(fnmatch.fnmatch(parts[-1], pattern) for pattern in FORBIDDEN_FILES)


def git(repo: str, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, check=False)


def scan_tree(repo: str, words: list[str]) -> list[str]:
    found = []
    listing = git(repo, "ls-files", "-co", "--exclude-standard", "-z")
    for rel in filter(None, listing.stdout.decode(errors="replace").split("\0")):
        if forbidden_file(rel):
            found.append(f"forbidden-file  {rel}")
            continue
        try:
            data = open(os.path.join(repo, rel), "rb").read()
        except OSError:
            continue
        if b"\0" in data:
            continue
        for number, line in enumerate(data.decode(errors="replace").splitlines(), 1):
            for rule in scan_line(line, words):
                found.append(f"{rule}  {rel}:{number}")
    return found


def scan_history(repo: str, words: list[str]) -> list[str]:
    found: set[str] = set()
    names = git(repo, "log", "--all", "--name-only", "--pretty=format:commit %H", "--no-color")
    commit = ""
    for line in names.stdout.decode(errors="replace").splitlines():
        if line.startswith("commit "):
            commit = line.split()[1][:8]
        elif line and forbidden_file(line):
            found.add(f"history  {commit}  forbidden-file  {line}")
    patches = git(repo, "log", "--all", "-p", "--no-color", "--pretty=format:commit %H")
    commit = ""
    for line in patches.stdout.decode(errors="replace").splitlines():
        if line.startswith("commit "):
            commit = line.split()[1][:8]
        elif line.startswith("+") and not line.startswith("+++"):
            for rule in scan_line(line[1:], words):
                found.add(f"history  {commit}  {rule}")
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a repository before it is published.")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--history", action="store_true", help="also scan every commit in the history")
    args = parser.parse_args(argv)
    words = identifiers()
    findings = scan_tree(args.repo, words)
    if args.history:
        findings += scan_history(args.repo, words)
    for finding in findings:
        print(finding)
    print(f"{len(findings)} findings" if findings else "no findings")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
