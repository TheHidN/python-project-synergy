#!/usr/bin/env python3
"""Generate docs/cli-reference.md from the real argument parser, so the reference can never drift from the code."""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from project_synergy import cli  # noqa: E402


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _walk(parser: argparse.ArgumentParser, path: list[str], out: list[str]) -> None:
    children = _subparsers(parser)
    if path:
        out.append(f"### synergy {' '.join(path)}")
        out.append("")
        helps = {a.dest: a.help for a in getattr(parser, "_actions", [])}
        description = parser.description or ""
        if description:
            out.append(description)
            out.append("")
        out.append("```")
        out.append(" ".join(parser.format_usage().replace("usage: ", "").split()))
        out.append("```")
        out.append("")
        rows = []
        for action in parser._actions:
            if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
                continue
            name = ", ".join(action.option_strings) if action.option_strings else action.dest
            rows.append((name, (action.help or "").replace("|", "/")))
        if rows:
            out.append("| Argument | What it does |")
            out.append("|---|---|")
            for name, text in rows:
                out.append(f"| `{name}` | {text} |")
            out.append("")
        del helps
    for name, child in children.items():
        _walk(child, [*path, name], out)


def render() -> str:
    parser = cli._parser()
    lines = ["# CLI reference", "",
             "Generated from the program's own argument parser by `scripts/gen_cli_reference.py`; do not edit by hand.",
             "Exit codes: 0 done, 2 wrong usage, 3 refused (the message says why), 4 no registry yet (run `synergy init`).",
             "Every command is a local program and uses no Claude model tokens.", "",
             "Commands that act as an operator read that operator's own token automatically; run them in a session whose",
             "launcher set `SYNERGY_OPERATOR`.", ""]
    top = _subparsers(parser)
    lines.append("## Commands")
    lines.append("")
    lines.append("| Command | What it does |")
    lines.append("|---|---|")
    help_by_name = {a.dest: a.help for a in parser._actions if isinstance(a, argparse._SubParsersAction)}
    del help_by_name
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for choice in action._choices_actions:
                lines.append(f"| `synergy {choice.dest}` | {choice.help or ''} |")
    lines.append("")
    lines.append("## Details")
    lines.append("")
    _walk(parser, [], lines)
    del top
    return "\n".join(lines).rstrip() + "\n"


if __name__ == "__main__":
    sys.stdout.write(render())
