"""Guard rails for what may live in this repository.

A ledger is operational data about real systems, and the fastest way to leak it
is to commit one "just for a test". These checks run in CI so the mistake is
caught by a red build rather than by a stranger reading the repository.

They are deliberately generic: patterns for credentials, machine-specific
absolute paths, contact addresses and database files. Nothing here encodes a
list of things some particular organisation considers secret, because such a
list is itself a disclosure.
"""
from __future__ import annotations

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRECTORIES = {
    ".git", ".venv", "venv", "env", "__pycache__", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", "build", "dist", ".tox", ".nox", "htmlcov",
}

TEXT_SUFFIXES = {
    ".py", ".md", ".toml", ".cfg", ".txt", ".yml", ".yaml", ".json",
    ".ini", ".gitignore", ".gitattributes", "",
}

FORBIDDEN_SUFFIXES = {".sqlite", ".db", ".sqlite3", ".pem", ".key", ".pfx", ".p12"}

FORBIDDEN_NAMES = {".env", "credentials.json", "secrets.json", "id_rsa", "id_ed25519"}

# Built so that the patterns cannot match their own source text.
SECRET_PATTERNS = [
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key id", re.compile(r"AKIA" + r"[0-9A-Z]{16}")),
    ("GitHub token", re.compile(r"gh[pousr]_" + r"[A-Za-z0-9]{36}")),
    ("Slack token", re.compile(r"xox[baprs]-" + r"[0-9A-Za-z-]{10,}")),
    ("OpenAI-style key", re.compile(r"\bsk-" + r"[A-Za-z0-9]{32,}")),
    ("Google API key", re.compile(r"AIza" + r"[0-9A-Za-z_-]{35}")),
    ("bearer token literal", re.compile(r"(?i)authorization\s*[:=]\s*['\"]?bearer\s+\S+")),
    (
        "assigned secret literal",
        re.compile(
            r"(?i)\b(api[_-]?key|secret|passwd|password|access[_-]?token)\b\s*[:=]\s*"
            r"['\"][^'\"\s]{8,}['\"]"
        ),
    ),
    ("Windows home path", re.compile(r"[A-Za-z]:\\Users\\[A-Za-z0-9._-]+")),
    ("POSIX home path", re.compile(r"/(?:home|Users)/[a-z][A-Za-z0-9._-]*")),
    (
        "contact address",
        re.compile(
            r"[A-Za-z0-9._%+-]+@(?!example\.(?:com|org)\b|noreply\b)"
            r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
        ),
    ),
]


def repository_files():
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if any(part in SKIP_DIRECTORIES for part in relative.parts):
            continue
        if path.is_file():
            yield path


class RepositoryHygieneTests(unittest.TestCase):
    def test_no_data_or_credential_files_are_present(self):
        offenders = [
            str(path.relative_to(ROOT))
            for path in repository_files()
            if path.suffix.lower() in FORBIDDEN_SUFFIXES
            or path.name in FORBIDDEN_NAMES
            or path.name.startswith(".env.")
        ]
        self.assertEqual([], offenders, "data or credential files must not be committed")

    def test_no_secret_or_machine_specific_patterns(self):
        offenders = []
        for path in repository_files():
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                for label, pattern in SECRET_PATTERNS:
                    if pattern.search(line):
                        offenders.append(
                            f"{path.relative_to(ROOT)}:{line_number}: {label}"
                        )
        self.assertEqual([], offenders)

    def test_text_files_use_lf_line_endings(self):
        offenders = []
        for path in repository_files():
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            try:
                raw = path.read_bytes()
            except OSError:  # pragma: no cover - unreadable file
                continue
            if b"\r\n" in raw:
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual([], offenders, "commit text files with LF endings")

    def test_the_scan_can_actually_fail(self):
        """A check that cannot produce a positive proves nothing when clean."""
        samples = {
            "AWS access key id": "AKIA" + "ABCDEFGHIJKLMNOP",
            "GitHub token": "ghp" + "_" + "a" * 36,
            "Windows home path": "C:" + chr(92) + "Users" + chr(92) + "someone",
            "POSIX home path": "/home" + "/someone/ledger",
            "contact address": "person@" + "some-company.tld",
            "assigned secret literal": 'password = "' + "hunter2hunter2" + '"',
        }
        for label, sample in samples.items():
            pattern = dict(SECRET_PATTERNS)[label]
            self.assertTrue(pattern.search(sample), f"{label} pattern never matches")

    def test_the_expected_public_files_exist(self):
        for name in (
            "README.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md",
            "pyproject.toml", ".gitignore", ".gitattributes",
        ):
            self.assertTrue((ROOT / name).exists(), f"{name} is missing")
        self.assertTrue((ROOT / "src" / "agent_action_ledger").is_dir())
        self.assertTrue((ROOT / "examples" / "support_desk.py").exists())
        self.assertTrue((ROOT / ".github" / "workflows").is_dir())


if __name__ == "__main__":
    unittest.main()
