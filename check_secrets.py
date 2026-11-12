"""Scan tracked project text files for likely credentials without printing them."""

import math
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent
EXCLUDED_DIRS = {".git", ".venv", "__pycache__", "test_invoices"}
SECRET_PATTERNS = (
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_-]{20,}")),
    ("Anthropic API key", re.compile(r"sk-ant-[0-9A-Za-z_-]{20,}")),
    ("Groq API key", re.compile(r"gsk_[0-9A-Za-z_-]{20,}")),
    (
        "SMTP password",
        re.compile(
            r"""SMTP_PASSWORD[ \t]*[:=][ \t]*["']?([^\s"'#]{8,})""",
            re.IGNORECASE,
        ),
    ),
)
LONG_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9])")
PLACEHOLDERS = {
    "change-me", "changeme", "placeholder", "your-token-here",
    "your-smtp-password", "wrong-password",
}


def _entropy(value):
    counts = {character: value.count(character) for character in set(value)}
    return -sum(
        (count / len(value)) * math.log2(count / len(value))
        for count in counts.values()
    )


def find_secrets(text):
    findings = []
    for label, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            if label == "SMTP password":
                value = match.group(1).strip()
                if not value or value.casefold() in PLACEHOLDERS:
                    continue
            findings.append((label, match.start()))
    for match in LONG_TOKEN.finditer(text):
        value = match.group(0)
        if (
            len(value) >= 32
            and not re.fullmatch(r"[a-z]+(?:_[a-z]+)+", value)
            and len(set(value)) > 8
            and not re.fullmatch(r"[0-9a-fA-F]{32,}", value)
            and _entropy(value) >= 3.5
        ):
            findings.append(("high-entropy token", match.start()))
    return findings


def _tracked_paths():
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return [
            path.relative_to(ROOT)
            for path in ROOT.rglob("*")
            if path.is_file()
            and not any(part in EXCLUDED_DIRS for part in path.relative_to(ROOT).parts)
        ]
    return [
        Path(path.decode("utf-8"))
        for path in result.stdout.split(b"\0")
        if path
    ]


def scan():
    results = []
    for relative_path in _tracked_paths():
        if relative_path.name == ".env" or (
            relative_path.name.startswith(".env.")
            and relative_path.name != ".env.example"
        ):
            continue
        if relative_path.name == "app.db":
            continue
        path = ROOT / relative_path
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
            if b"\0" in data or len(data) > 2_000_000:
                continue
            text = data.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        line_starts = [0]
        line_starts.extend(
            index + 1 for index, character in enumerate(text) if character == "\n"
        )
        for label, offset in find_secrets(text):
            line = sum(start <= offset for start in line_starts)
            results.append((relative_path.as_posix(), line, label))
    return results


if __name__ == "__main__":
    findings = scan()
    if findings:
        for path, line, label in findings:
            print(f"{path}:{line}: possible {label}")
        raise SystemExit(1)
    print("No likely credentials found.")
