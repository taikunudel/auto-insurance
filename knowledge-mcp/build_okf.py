#!/usr/bin/env python3
"""OKF helper functions shared by the knowledge-mcp server.

Pure, deterministic helpers for reading and writing OKF markdown: frontmatter
parsing, type/title/timestamp derivation, and folder `index.md` (catalog)
generation. No machine-specific paths, no network, no randomness. `server.py`
imports these for its write tools (kb_add, kb_reindex, kb_validate, ...).
"""
import re
from pathlib import Path

RESERVED = {"index.md", "log.md"}            # OKF reserved filenames (carry no frontmatter)


def split_frontmatter(text):
    """Return (fm_lines|None, body). fm_lines are the lines between the first
    two `---` fences (newlines stripped); body is everything after."""
    m = re.match(r"^---[ \t]*\n(.*?\n)?---[ \t]*\n?", text, re.DOTALL)
    if m:
        return (m.group(1) or "").splitlines(), text[m.end():]
    return None, text


def fm_keys(fm_lines):
    keys = {}
    for ln in fm_lines or []:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):(.*)$", ln)
        if m:
            keys[m.group(1)] = m.group(2).strip()
    return keys


def guess_type(path):
    if path.stem == "overview":
        return "overview"
    if path.stem == "gaps":
        return "gaps"
    if path.name in ("CLAUDE.md", "AGENTS.md", "GEMINI.md"):
        return "agent-instructions"
    return {"concepts": "concept", "entities": "entity",
            "sources": "source", "examples": "example"}.get(path.parent.name, "document")


def title_from(keys, path):
    t = (keys.get("title", "") or "").strip().strip('"').strip("'")
    return t if t else path.stem.replace("-", " ").replace("_", " ").title()


def iso_ts(keys):
    d = (keys.get("last_updated") or keys.get("date") or "").strip().strip('"').strip("'")
    if re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        return f"{d}T00:00:00Z"
    if re.match(r"^\d{4}-\d{2}-\d{2}T[\d:]+Z?$", d):
        return d
    return ""


def derive_description(body):
    for raw in body.splitlines():
        ln = raw.strip()
        if not ln or ln.startswith(("#", ">", "---", "- ", "* ", "|", "```")):
            continue
        ln = re.sub(r"\s+", " ", ln)
        first = re.split(r"(?<=[.!?]) ", ln)[0]
        return first[:160].rstrip()
    return ""


def first_comment(path):
    for raw in path.read_text().splitlines():
        ln = raw.strip()
        if ln.startswith("#"):
            return ln.lstrip("# ").strip()[:160]
        if ln:
            break
    return ""


def _parse_index(folder):
    """Read an existing index.md into {link: description} so curated descriptions survive a rebuild."""
    idx = folder / "index.md"
    out = {}
    if idx.is_file():
        for ln in idx.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\*\s*\[[^\]]*\]\(([^)]+)\)\s*(?:-\s*(.*))?$", ln.strip())
            if m:
                out[m.group(1)] = (m.group(2) or "").strip()
    return out


def _subdir_desc(d):
    """A one-line description for a sub-folder: its index.md's first prose line, else ''."""
    idx = d / "index.md"
    if idx.is_file():
        for ln in idx.read_text(encoding="utf-8").splitlines():
            s = ln.strip()
            if s and not s.startswith(("#", "*", "-", ">", "|", "```")):
                return s[:160]
    return ""


def gen_index(folder):
    """Write an OKF reserved index.md (no frontmatter) cataloguing this folder. It lists the
    sub-folders first (linked as `name/`), then the concept docs (.md) and runnable snippets
    (.R). One format throughout: `* [Title](link) - description`. A human-written description
    already in index.md is preserved when the item itself does not supply one, so a rebuild
    never silently wipes curated text. Returns the entry count, or None if empty/missing."""
    folder = Path(folder)
    if not folder.is_dir():
        return None
    prev = _parse_index(folder)
    subdirs, files = [], []
    for child in sorted(folder.iterdir()):
        if child.name in RESERVED:
            continue
        if child.is_dir():
            link = f"{child.name}/"
            title = child.name.replace("-", " ").replace("_", " ").title()
            subdirs.append((title, link, prev.get(link) or _subdir_desc(child)))
        elif child.suffix == ".md":
            fm, body = split_frontmatter(child.read_text())
            keys = fm_keys(fm or [])
            desc = keys.get("description") or prev.get(child.name) or derive_description(body)
            files.append((title_from(keys, child), child.name, desc))
        elif child.suffix == ".R":
            files.append((child.stem, child.name, prev.get(child.name) or first_comment(child)))
    entries = subdirs + files
    if not entries:
        return None
    title = folder.name.replace("-", " ").replace("_", " ").title()
    lines = [f"# {title}\n\n"]
    for ttl, link, desc in entries:
        lines.append(f"* [{ttl}]({link})" + (f" - {desc}" if desc else "") + "\n")
    (folder / "index.md").write_text("".join(lines))
    return len(entries)
