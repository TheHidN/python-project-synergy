import importlib.util
import os
import pathlib
import re
import shlex
import unittest

from project_synergy import cli

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOC_FILES = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]


def has_emoji(text: str) -> bool:
    return any(0x1F300 <= ord(c) <= 0x1FAFF or 0x2600 <= ord(c) <= 0x27BF or ord(c) in (0x2B50, 0x2B06, 0x2705)
               for c in text)


CHAPTERS = ["Introduction", "Quick start", "Installation", "Claude setup", "Multi-operator setup", "tmux guide",
            "Collaboration guide", "CLI reference", "Configuration guide", "Safety guide", "Usage efficiency",
            "Troubleshooting", "Updating and uninstalling", "Architecture", "FAQ"]


def load_generator():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("gen_cli_reference", ROOT / "scripts" / "gen_cli_reference.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def documented_commands() -> list[tuple[str, list[str]]]:
    found = []
    for path in DOC_FILES:
        if path.name == "cli-reference.md":
            continue
        in_block = False
        for line in path.read_text().splitlines():
            if line.strip().startswith("```"):
                in_block = not in_block
                continue
            if not in_block:
                continue
            text = line.strip().lstrip("$ ").strip()
            if not re.match(r"^([A-Z_]+=\S+\s+)*synergy\s", text):
                continue
            parts = shlex.split(text)
            while parts and re.match(r"^[A-Z_]+=", parts[0]):
                parts.pop(0)
            found.append((f"{path.name}: {text}", parts[1:]))
    return found


class TestDocs(unittest.TestCase):
    def test_the_cli_reference_is_generated_from_the_real_parser_and_is_current(self) -> None:
        expected = load_generator().render()
        self.assertEqual((ROOT / "docs" / "cli-reference.md").read_text(), expected,
                         "run: python3 scripts/gen_cli_reference.py > docs/cli-reference.md")

    def test_every_documented_synergy_command_parses_with_the_real_parser(self) -> None:
        commands = documented_commands()
        self.assertGreater(len(commands), 25)
        parser = cli._parser()
        for label, argv in commands:
            if "--" in argv:
                argv = argv[: argv.index("--")]
            try:
                args = parser.parse_args(argv)
            except SystemExit:
                self.fail(f"does not parse: {label}")
            self.assertTrue(args.group, label)

    def test_relative_links_point_at_files_that_exist(self) -> None:
        for path in DOC_FILES:
            for target in re.findall(r"\]\((?!https?://|#|mailto:)([^)#\s]+)", path.read_text()):
                self.assertTrue((path.parent / target).exists(), f"{path.name}: broken link {target}")

    def test_no_private_paths_names_or_addresses_in_the_docs(self) -> None:
        for path in DOC_FILES:
            text = path.read_text().replace("https://github.com/TheHidN/python-project-synergy.git", "")
            for pattern in (r"/home/\w", re.escape(__import__("getpass").getuser()), r"hidn", r"\b(?:\d{1,3}\.){3}\d{1,3}\b", r"api[_-]?key\s*[:=]",  # prepublish: ignore
                            r"BEGIN [A-Z ]*PRIVATE KEY"):
                self.assertIsNone(re.search(pattern, text, re.I), f"{path.name}: {pattern}")

    def test_the_manual_has_every_required_chapter(self) -> None:
        headings = re.findall(r"^#{2}\s+(?:\d+\.\s+)?(.+)$", (ROOT / "docs" / "user-manual.md").read_text(), re.M)
        for chapter in CHAPTERS:
            self.assertIn(chapter, headings)

    def test_the_quick_start_is_short_and_numbered_from_one(self) -> None:
        text = (ROOT / "docs" / "quick-start.md").read_text()
        steps = re.findall(r"^(\d+)\.\s", text, re.M)
        self.assertEqual(steps, [str(i) for i in range(1, len(steps) + 1)])
        self.assertLessEqual(len(steps), 7)
        self.assertLess(len(text.split()), 900)

    def test_readme_follows_the_house_style(self) -> None:
        lines = (ROOT / "README.md").read_text().splitlines()
        self.assertTrue(lines[0].startswith("# ") and has_emoji(lines[0]))
        self.assertTrue(any(l.startswith("> ") and has_emoji(l) for l in lines[:5]))
        for line in lines:
            if line.startswith("## "):
                self.assertTrue(has_emoji(line), line)
        in_block = False
        for line in lines:
            if line.strip().startswith("```"):
                in_block = not in_block
            elif in_block:
                self.assertFalse(has_emoji(line), line)

    def test_docs_never_claim_what_is_untested_as_working(self) -> None:
        text = " ".join(p.read_text() for p in DOC_FILES).lower()
        for phrase in ("guaranteed safe", "completely secure", "unbreakable", "100% secure"):
            self.assertNotIn(phrase, text)

    def test_docs_state_the_token_limit_and_the_bypass_caution(self) -> None:
        manual = (ROOT / "docs" / "user-manual.md").read_text().lower()
        self.assertIn("same linux user", manual)
        self.assertIn("bypass", manual)
        self.assertIn("not a security boundary", manual)
