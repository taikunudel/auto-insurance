#!/usr/bin/env python3
"""knowledge-mcp server: one server, two modes (read | manage) over a git-backed OKF wiki.

The knowledge of a topic is a git repo at <registry>/<topic>/repo/. The arms are versioned
branches (`v1`, `v2`, and `v3`).

read mode    : 5 read tools, pinned to ONE commit, content served from git, read-only.
               At startup the requested ref (--version, default the repo's current branch)
               is resolved to a commit id; the server serves that commit for its whole life,
               so a run stays frozen even while a manager keeps committing.
manage mode  : all 15 tools. Checks out one arm and edits the WORKING TREE; kb_snapshot
               commits (a new immutable point); earlier commits never change. kb_new_topic
               creates a NEW topic (a separate repo under --topics-root, default the current
               repo's parent) — but only with a charter agreed with the user first.

The rules (AGENT_RULES.md) ship as the FastMCP `instructions` and via kb_rules(). The manage
tools gate on the rules: the first manage call returns the rulebook and asks the agent to retry.

Launch:
  server.py --mode read   --registry ~/knowledge --topic <t> [--version v1|v2|v3|<ref>]
  server.py --mode manage --registry ~/knowledge --topic <t> [--version <arm>]
  server.py --root <path>                      # serve a plain folder directly (read mode)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# reuse the OKF helpers we already wrote and tested
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_okf import (split_frontmatter, fm_keys, gen_index,
                       title_from, RESERVED)  # noqa: E402

# --- args ----------------------------------------------------------------------------
ap = argparse.ArgumentParser(description="knowledge-mcp server (stdio).")
ap.add_argument("--mode", choices=["read", "manage"], default="read")
ap.add_argument("--registry", default=None, help="path to the registry (parent of <topic>/).")
ap.add_argument("--topic", default=None)
ap.add_argument("--version", default=None,
                help="read: ref/branch/commit to serve (default the repo's current branch); "
                     "manage: the arm (branch) to edit.")
ap.add_argument("--root", default=None, help="serve exactly this folder (read mode); bypasses git.")
ap.add_argument("--topics-root", default=None,
                help="manage: where kb_new_topic creates new topic repos "
                     "(default: the parent folder of the current repo).")
ap.add_argument("--log-dir", default=None)
ap.add_argument("--name", default="knowledge")
ARGS = ap.parse_args()


def _die(msg: str):
    sys.exit(f"[knowledge-mcp] {msg}")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- resolve registry / topic / repo (self-locating; rule 7) -------------------------
# Default the registry to the sibling `knowledge/` folder so the server works wherever the
# whole knowledge-mcp-design/ tree lives, with no absolute path needed.
_DEFAULT_REGISTRY = Path(__file__).resolve().parents[1] / "knowledge"
REGISTRY = (Path(ARGS.registry).expanduser().resolve() if ARGS.registry
            else (_DEFAULT_REGISTRY if _DEFAULT_REGISTRY.is_dir() else None))
TOPIC = ARGS.topic
# The knowledge base is a git repo, found two ways: the registry path may BE the repo
# (flat: the OKF bundle sits at the repo root), or the legacy layout <registry>/<topic>/repo/.
REPO = None
if REGISTRY and (REGISTRY / ".git").is_dir():
    REPO = REGISTRY
    TOPIC = TOPIC or REGISTRY.name
elif REGISTRY and not ARGS.root:
    if not TOPIC:
        _topics = [d.name for d in REGISTRY.iterdir() if d.is_dir()]
        if len(_topics) == 1:          # exactly one topic: use it without being told
            TOPIC = _topics[0]
    _td = (REGISTRY / TOPIC) if TOPIC else None
    if _td and (_td / "repo" / ".git").is_dir():
        REPO = _td / "repo"
if REPO and shutil.which("git") is None:    # doctor: fail loudly, do not limp on
    _die("git is required to serve the knowledge repo but was not found on PATH. Install git.")


def _git(*a) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(REPO), *a], capture_output=True, text=True)


def _default_branch() -> str:
    r = _git("rev-parse", "--abbrev-ref", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else "v1"


# Decide where content comes from: a frozen commit (read+git), or a directory (manage / --root).
SHA = None
if ARGS.root:
    ACTIVE_ROOT = Path(ARGS.root).expanduser().resolve()
    if not ACTIVE_ROOT.is_dir():
        _die(f"--root not a folder: {ACTIVE_ROOT}")
    READ_FROM_GIT = False
    VERSION_LABEL = ACTIVE_ROOT.name
elif REPO:
    REF = ARGS.version or _default_branch()
    if ARGS.mode == "read":
        rp = _git("rev-parse", "--verify", f"{REF}^{{commit}}")
        if rp.returncode:
            _die(f"no such version/ref '{REF}' in the repo")
        SHA = rp.stdout.strip()
        READ_FROM_GIT = True
        ACTIVE_ROOT = REPO          # used only for logging label; content comes from SHA
        VERSION_LABEL = REF
    else:                            # manage: check out the arm and edit its working tree
        co = _git("checkout", REF)
        if co.returncode:
            _die(f"manage: cannot check out arm '{REF}' (uncommitted changes?): {co.stderr.strip()}")
        READ_FROM_GIT = False
        ACTIVE_ROOT = REPO
        VERSION_LABEL = REF
else:
    _die("need --root, or a git repo at <topic>/repo/ (run the migration), "
         "or both --registry and --topic")

# Where kb_new_topic creates NEW topic repos: --topics-root, else beside the current repo.
TOPICS_ROOT = (Path(ARGS.topics_root).expanduser().resolve() if ARGS.topics_root
               else (REPO.parent if REPO else ACTIVE_ROOT.parent))

# --- access log ----------------------------------------------------------------------
LOG_DIR = (Path(ARGS.log_dir).expanduser().resolve() if ARGS.log_dir
           else Path(__file__).resolve().parent / "logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / f"kb_access_{os.getpid()}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl"


def _log(tool: str, **fields):
    rec = {"ts": _now(), "tool": tool, "mode": ARGS.mode, "topic": TOPIC, "version": VERSION_LABEL}
    if SHA:
        rec["commit"] = SHA[:12]
    rec.update(fields)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # logging must never break a tool
        pass


# --- content access: mode-aware (frozen commit for read+git, else the directory) ------
def _norm_pid(page_id: str) -> str:
    """Normalise a bundle-relative page_id and block traversal."""
    rel = page_id.strip().lstrip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError(f"page_id escapes the knowledge root: {page_id!r}")
    return "/".join(parts)


def _pages() -> list[str]:
    if READ_FROM_GIT:
        out = _git("ls-tree", "-r", "--name-only", SHA).stdout
        return sorted(p for p in out.splitlines() if p)
    return sorted(str(p.relative_to(ACTIVE_ROOT)) for p in ACTIVE_ROOT.rglob("*")
                  if p.is_file() and ".git" not in p.relative_to(ACTIVE_ROOT).parts)


def _page_text(pid: str) -> str:
    if READ_FROM_GIT:
        r = _git("show", f"{SHA}:{pid}")
        if r.returncode:
            raise FileNotFoundError(pid)
        return r.stdout
    return (ACTIVE_ROOT / pid).read_text(encoding="utf-8", errors="replace")


def _page_exists(pid: str) -> bool:
    if READ_FROM_GIT:
        return _git("cat-file", "-e", f"{SHA}:{pid}").returncode == 0
    return (ACTIVE_ROOT / pid).is_file()


def _read(p: Path) -> str:                       # manage-internal file read (working tree)
    return p.read_text(encoding="utf-8", errors="replace")


def _serve(tool: str, page_id: str, text: str) -> str:
    blob = text.encode("utf-8")
    _log(tool, page_id=page_id, bytes=len(blob), sha256=hashlib.sha256(blob).hexdigest())
    return text


def _resolve_page(page_id: str) -> Path:         # manage write tools (directory, working tree)
    rel = page_id.strip().lstrip("/")
    target = (ACTIVE_ROOT / rel).resolve()
    if not target.is_relative_to(ACTIVE_ROOT):
        raise ValueError(f"page_id escapes the knowledge root: {page_id!r}")
    return target


# --- rules: shipped as instructions, returned by kb_rules, and gated on for writes ----
RULES_PATH = Path(__file__).resolve().parent / "AGENT_RULES.md"
RULES_TEXT = RULES_PATH.read_text(encoding="utf-8") if RULES_PATH.is_file() else "(AGENT_RULES.md not found)"
RULES_REMINDER = ("\n\n[knowledge rules] every page needs a non-empty `type`; index.md is "
                  "rebuilt for you; edits stay in the working tree until kb_snapshot commits "
                  "them (a new immutable point). Full rulebook: kb_rules().")
_RULES_READ = False


def _gate():
    """Before the first manage action, force the rules into context. Returns the rulebook
    (and asks the agent to retry) the first time; None thereafter."""
    global _RULES_READ
    if _RULES_READ:
        return None
    _RULES_READ = True
    _log("rules_gate")
    return ("STOP: read the knowledge rules before managing the base. They are below. "
            "Then call your tool again.\n\n" + RULES_TEXT)


mcp = FastMCP(ARGS.name, instructions=RULES_TEXT)


# ============================ READ TOOLS (always registered) =========================
@mcp.tool()
def kb_index(folder: str = "") -> str:
    """Return the table of contents (index.md) for a folder. Empty argument means the top level."""
    try:
        rel = _norm_pid(folder) if folder.strip() else ""
    except ValueError as e:
        return f"ERROR: {e}"
    pid = f"{rel}/index.md" if rel else "index.md"
    text = _page_text(pid) if _page_exists(pid) else f"(no index.md in '{rel or '.'}')"
    return _serve("kb_index", pid, text)


@mcp.tool()
def kb_list() -> str:
    """List the path (page_id) of every page in the knowledge base, one per line."""
    pages = _pages()
    _log("kb_list", n_pages=len(pages))
    return "\n".join(pages)


@mcp.tool()
def kb_get(page_id: str) -> str:
    """Return the full raw markdown of one page, identified by its path relative to the root."""
    try:
        pid = _norm_pid(page_id)
    except ValueError as e:
        _log("kb_get", page_id=page_id, error="traversal")
        return f"ERROR: {e}"
    if not _page_exists(pid):
        _log("kb_get", page_id=page_id, error="not_found")
        return f"ERROR: no such page '{page_id}'. Call kb_index() or kb_list() for valid page_ids."
    return _serve("kb_get", pid, _page_text(pid))


@mcp.tool()
def kb_grep(query: str, max_results: int = 50) -> str:
    """Search every page for a term (case-insensitive regex, literal fallback). Returns 'page_id:line: text'."""
    try:
        rx = re.compile(query, re.IGNORECASE)
    except re.error:
        rx = re.compile(re.escape(query), re.IGNORECASE)
    hits: list[str] = []
    pages: set[str] = set()
    for pid in _pages():
        try:
            text = _page_text(pid)
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{pid}:{i}: {line.strip()}")
                pages.add(pid)
                if len(hits) >= max_results:
                    break
        if len(hits) >= max_results:
            break
    _log("kb_grep", query=query, n_matches=len(hits), n_pages=len(pages))
    if not hits:
        return f"(no matches for {query!r})"
    return f"{len(hits)} match(es) across {len(pages)} page(s):\n" + "\n".join(hits)


@mcp.tool()
def kb_rules() -> str:
    """Return the operating rulebook (AGENT_RULES.md): how this base must be used and maintained."""
    global _RULES_READ
    _RULES_READ = True
    _log("kb_rules", bytes=len(RULES_TEXT.encode()))
    return RULES_TEXT


# ============================ MANAGE TOOLS (manage mode only) =========================
if ARGS.mode == "manage":

    def _append_log(msg: str):
        with (ACTIVE_ROOT / "log.md").open("a", encoding="utf-8") as fh:
            fh.write(f"- {_now()} {msg}\n")

    def _bundle_dirs():
        return [ACTIVE_ROOT] + [d for d in ACTIVE_ROOT.rglob("*")
                                if d.is_dir() and ".git" not in d.relative_to(ACTIVE_ROOT).parts]

    def _validate():
        issues = []
        folders = _bundle_dirs()
        for d in folders:
            if not (d / "index.md").is_file():
                issues.append(f"{d.relative_to(ACTIVE_ROOT) or '.'}: missing index.md")
        if not (ACTIVE_ROOT / "log.md").is_file():
            issues.append("log.md: missing at the bundle root")
        ndocs = 0
        for f in ACTIVE_ROOT.rglob("*.md"):
            if ".git" in f.relative_to(ACTIVE_ROOT).parts:
                continue
            fm, _ = split_frontmatter(_read(f))
            if f.name in RESERVED:
                if fm is not None:
                    issues.append(f"{f.relative_to(ACTIVE_ROOT)}: reserved {f.name} must carry no frontmatter")
                continue
            ndocs += 1
            if fm is None:
                issues.append(f"{f.relative_to(ACTIVE_ROOT)}: no frontmatter")
            elif not fm_keys(fm).get("type"):
                issues.append(f"{f.relative_to(ACTIVE_ROOT)}: missing `type`")
        return issues, len(folders), ndocs

    def _validate_str() -> str:
        issues, nf, nd = _validate()
        return f"PASS ({nd} docs, {nf} folders)" if not issues else f"FAIL ({len(issues)} issues)"

    @mcp.tool()
    def kb_add(page_id: str, type: str, title: str = "", description: str = "", body: str = "") -> str:
        """Create a new page in the working tree. Requires `type`. Writes frontmatter, rebuilds the parent index.md, logs, validates. Not committed until kb_snapshot."""
        g = _gate()
        if g is not None:
            return g
        if not type.strip():
            return "ERROR: a non-empty `type` is required (rule 2)."
        try:
            p = _resolve_page(page_id)
        except ValueError as e:
            return f"ERROR: {e}"
        if p.name in RESERVED:
            return f"ERROR: '{p.name}' is a reserved filename."
        if p.exists():
            return f"ERROR: {page_id} already exists; use kb_update."
        ts = _now()
        ttl = title.strip() or title_from({}, p)
        fm = [f"type: {type.strip()}", f'title: "{ttl}"']
        if description.strip():
            fm.append(f'description: "{description.strip()}"')
        fm += ["status: draft", "version: 1", f"created: {ts}", f"updated: {ts}"]
        p.parent.mkdir(parents=True, exist_ok=True)
        bd = body if body.strip() else f"# {ttl}\n"
        p.write_text("---\n" + "\n".join(fm) + "\n---\n\n" + bd, encoding="utf-8")
        gen_index(p.parent)
        _append_log(f"add {page_id}")
        _log("kb_add", page_id=page_id)
        return (f"added {page_id} (working tree, uncommitted)\n"
                f"  frontmatter: type={type.strip()}, title=\"{ttl}\", status=draft, version=1, created={ts}\n"
                f"  parent index rebuilt: {Path(page_id).parent}/index.md\n"
                f"  log appended\n  validate: {_validate_str()}") + RULES_REMINDER

    @mcp.tool()
    def kb_update(page_id: str, body: str = "", note: str = "") -> str:
        """Edit a page's body, bump its version and `updated` date, and log the change. Uncommitted until kb_snapshot."""
        g = _gate()
        if g is not None:
            return g
        try:
            p = _resolve_page(page_id)
        except ValueError as e:
            return f"ERROR: {e}"
        if not p.is_file():
            return f"ERROR: no such page '{page_id}'."
        fm, bd = split_frontmatter(_read(p))
        if fm is None:
            return f"ERROR: '{page_id}' has no frontmatter; refusing to edit blindly."
        try:
            ver = int(fm_keys(fm).get("version", "1")) + 1
        except ValueError:
            ver = 2
        new = []
        seen_v = seen_u = False
        for ln in fm:
            if re.match(r"\s*version:", ln):
                new.append(f"version: {ver}"); seen_v = True
            elif re.match(r"\s*updated:", ln):
                new.append(f"updated: {_now()}"); seen_u = True
            else:
                new.append(ln)
        if not seen_v:
            new.append(f"version: {ver}")
        if not seen_u:
            new.append(f"updated: {_now()}")
        newbody = body if body.strip() else bd
        p.write_text("---\n" + "\n".join(new) + "\n---\n" + ("" if newbody.startswith("\n") else "\n") + newbody,
                     encoding="utf-8")
        _append_log(f"update {page_id}" + (f" ({note})" if note else ""))
        _log("kb_update", page_id=page_id)
        return (f"updated {page_id}\n  version -> {ver}\n  updated: {_now()}\n  log appended") + RULES_REMINDER

    @mcp.tool()
    def kb_remove(page_id: str) -> str:
        """Delete a page from the working tree and rebuild the parent folder's index.md."""
        g = _gate()
        if g is not None:
            return g
        try:
            p = _resolve_page(page_id)
        except ValueError as e:
            return f"ERROR: {e}"
        if p.name in RESERVED:
            return f"ERROR: cannot remove reserved file '{p.name}'."
        if not p.is_file():
            return f"ERROR: no such page '{page_id}'."
        p.unlink()
        gen_index(p.parent)
        _append_log(f"remove {page_id}")
        _log("kb_remove", page_id=page_id)
        return (f"removed {page_id}\n  parent index rebuilt: {Path(page_id).parent}/index.md\n  log appended") + RULES_REMINDER

    @mcp.tool()
    def kb_new_folder(path: str, description: str = "") -> str:
        """Create a new sub-topic folder and give it the required index.md automatically."""
        g = _gate()
        if g is not None:
            return g
        d = (ACTIVE_ROOT / path.strip().strip("/")).resolve()
        if not d.is_relative_to(ACTIVE_ROOT):
            return "ERROR: path escapes the knowledge root."
        d.mkdir(parents=True, exist_ok=True)
        idx = d / "index.md"
        if not idx.exists():
            idx.write_text(f"# {d.name.replace('-', ' ').title()}\n\n{description}\n" if description
                           else f"# {d.name.replace('-', ' ').title()}\n", encoding="utf-8")
        gen_index(d.parent)
        _log("kb_new_folder", path=path)
        return (f"created {path}/\n  wrote {path}/index.md\n  parent index updated") + RULES_REMINDER

    @mcp.tool()
    def kb_reindex(folder: str = "") -> str:
        """Rebuild a folder's index.md from its actual on-disk children. Empty argument means the root."""
        g = _gate()
        if g is not None:
            return g
        d = (ACTIVE_ROOT / folder.strip().strip("/")) if folder else ACTIVE_ROOT
        n = gen_index(d) or 0
        _log("kb_reindex", folder=folder, entries=n)
        return (f"rebuilt {folder or '.'}/index.md ({n} entries)") + RULES_REMINDER

    @mcp.tool()
    def kb_validate() -> str:
        """Check the whole base against the rules and report anything broken."""
        g = _gate()
        if g is not None:
            return g
        issues, nf, nd = _validate()
        _log("kb_validate", issues=len(issues))
        if not issues:
            return (f"OKF v0.1: PASS\n  folders checked: {nf}\n  every folder has index.md: yes\n"
                    f"  knowledge documents: {nd}\n  every document has a non-empty type: yes\n"
                    f"  reserved files carry no frontmatter: yes") + RULES_REMINDER
        return (f"OKF v0.1: FAIL ({len(issues)} issue(s))\n" + "\n".join(f"  - {x}" for x in issues)) + RULES_REMINDER

    @mcp.tool()
    def kb_versions() -> str:
        """List the arms (git branches) and the recent timeline, marking the one checked out."""
        g = _gate()
        if g is not None:
            return g
        head = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        branches = _git("for-each-ref", "--sort=refname",
                        "--format=%(refname:short)\t%(objectname:short)\t%(contents:subject)",
                        "refs/heads").stdout.strip().splitlines()
        log = _git("log", "--oneline", "-6").stdout.strip().splitlines()
        lines = [f"topic: {TOPIC}  (git repo)", "arms (branches):"]
        for b in branches:
            name = b.split("\t", 1)[0]
            lines.append("  " + b.replace("\t", "  ") + ("   <- current" if name == head else ""))
        lines.append(f"recent commits on {head}:")
        lines += [f"  {l}" for l in log]
        _log("kb_versions")
        return "\n".join(lines) + RULES_REMINDER

    @mcp.tool()
    def kb_snapshot(message: str) -> str:
        """Commit the working-tree changes on the current arm as a new immutable point. Earlier commits never change."""
        g = _gate()
        if g is not None:
            return g
        if not message.strip():
            return "ERROR: a snapshot needs a message describing the change."
        _git("add", "-A")
        c = _git("commit", "-m", message.strip())
        if "nothing to commit" in (c.stdout + c.stderr):
            return "nothing to snapshot: the working tree has no changes." + RULES_REMINDER
        if c.returncode:
            return f"ERROR: commit failed: {(c.stdout + c.stderr).strip()}"
        head = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        sha = _git("rev-parse", "--short", "HEAD").stdout.strip()
        _log("kb_snapshot", branch=head, commit=sha)
        return (f"committed on {head}: {sha}  {message.strip()}\n"
                f"  a new immutable point; earlier commits are unchanged\n"
                f"  read servers pin a commit at startup, so running runs are unaffected") + RULES_REMINDER

    @mcp.tool()
    def kb_set_current(ref: str) -> str:
        """Switch the working tree to another arm (git branch). Read servers started afterward default to it."""
        g = _gate()
        if g is not None:
            return g
        if _git("rev-parse", "--verify", f"{ref}^{{commit}}").returncode:
            return f"ERROR: no such arm/ref '{ref}'. Call kb_versions()."
        old = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        co = _git("checkout", ref)
        if co.returncode:
            return (f"ERROR: cannot switch to '{ref}' (uncommitted changes? snapshot or discard first). "
                    f"git said: {co.stderr.strip()}")
        _log("kb_set_current", old=old, new=ref)
        return (f"current arm: {old} -> {ref} (checked out)\n"
                f"  read servers started now default to {ref}\n"
                f"  already-running read servers keep their pinned commit") + RULES_REMINDER

    _SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
    _INTERVIEW = (
        "STOP — nothing was created. A new topic needs a charter agreed with the USER first.\n"
        "Interview the user now; do NOT invent the answers yourself:\n"
        "  1. materials — what does the user have available (papers, repos, datasets, notes,\n"
        "     run logs, URLs)?\n"
        "  2. purpose & consumers — which agents/tasks will read this wiki, and to do what?\n"
        "  3. scope — what is in, what is out?\n"
        "  4. structure — folder plan (default: concepts, entities, sources, examples;\n"
        "     override with `folders`).\n"
        "Distil the user's answers into a short charter, then call\n"
        "  kb_new_topic(name='{name}', charter=<the distilled answers>[, folders='a,b,c'])\n"
        "The charter is written into the new topic's overview.md, so the agreement is durable."
    )

    def _tgit(target: Path, *a) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(target), *a], capture_output=True, text=True)

    def _portable(p: Path) -> str:
        """Render a path with $HOME instead of the literal home dir (rule 7: portable configs)."""
        s, home = str(p), str(Path.home())
        return s.replace(home, "$HOME", 1) if s.startswith(home + os.sep) else s

    @mcp.tool()
    def kb_new_topic(name: str, charter: str = "",
                     folders: str = "concepts,entities,sources,examples") -> str:
        """Create a NEW topic (a separate git repo beside this one) after a charter is agreed with the user. Call it without `charter` first: it returns the interview to run with the user. Never write the charter yourself; it must record the USER's answers."""
        g = _gate()
        if g is not None:
            return g
        slug = name.strip().lower()
        if not _SLUG_RE.match(slug):
            return (f"ERROR: topic name must be a slug (lowercase letters/digits/dashes, "
                    f"starting alphanumeric); got {name!r}.")
        target = (TOPICS_ROOT / slug).resolve()
        if not target.is_relative_to(TOPICS_ROOT):
            return f"ERROR: topic path escapes the topics root: {name!r}"
        if target.exists():
            return f"ERROR: {target} already exists; pick another name."
        if not charter.strip():
            _log("kb_new_topic", name=slug, phase="interview")
            return _INTERVIEW.format(name=slug)
        if len(charter.strip()) < 80:
            return ("ERROR: charter too thin. It must record the user's actual answers "
                    "(materials, purpose & consumers, scope). Interview the user, then retry.")
        subdirs = [s.strip() for s in folders.split(",") if s.strip()]
        bad = [s for s in subdirs if not _SLUG_RE.match(s)]
        if bad:
            return f"ERROR: bad folder name(s): {', '.join(bad)} (slugs only)."
        ts = _now()
        ttl = slug.replace("-", " ").title()
        target.mkdir(parents=True)
        (target / "log.md").write_text(f"- {ts} topic created; charter agreed with the user\n",
                                       encoding="utf-8")
        (target / "overview.md").write_text(
            "---\n"
            "type: overview\n"
            f'title: "Overview — {ttl}"\n'
            "status: draft\nversion: 1\n"
            f"created: {ts}\nupdated: {ts}\n"
            "---\n\n"
            f"# {ttl}\n\n## Charter (agreed with the user)\n\n{charter.strip()}\n",
            encoding="utf-8")
        for s in subdirs:
            d = target / s
            d.mkdir()
            (d / "index.md").write_text(f"# {s.replace('-', ' ').title()}\n", encoding="utf-8")
        gen_index(target)
        for cmd in (("init", "-b", "v1"), ("add", "-A")):
            r = _tgit(target, *cmd)
            if r.returncode:
                return f"ERROR: git {' '.join(cmd)} failed: {(r.stdout + r.stderr).strip()}"
        ident = ([] if _tgit(target, "config", "user.email").stdout.strip()
                 else ["-c", "user.name=knowledge-mcp", "-c", "user.email=knowledge-mcp@localhost"])
        r = subprocess.run(["git", "-C", str(target), *ident, "commit", "-m",
                            f"init {slug} wiki (charter agreed with user)"],
                           capture_output=True, text=True)
        if r.returncode:
            return f"ERROR: git commit failed: {(r.stdout + r.stderr).strip()}"
        sha = _tgit(target, "rev-parse", "--short", "HEAD").stdout.strip()
        _log("kb_new_topic", name=slug, phase="created", path=str(target), commit=sha,
             folders=subdirs)
        start = Path(__file__).resolve().parent / "start.sh"
        cfg = json.dumps({"mcpServers": {f"knowledge-{slug}": {
            "command": "/bin/sh",
            "args": ["-c", f'exec "{_portable(start)}" --mode read '
                           f'--registry "{_portable(target)}" --name knowledge-{slug}']}}},
            indent=2, ensure_ascii=False)
        return (f"created topic '{slug}' at {target}\n"
                f"  git repo, branch v1, commit {sha}\n"
                f"  seeded: overview.md (charter embedded), log.md, index.md"
                + (f", folders: {', '.join(subdirs)}" if subdirs else "") + "\n"
                f"  NOTE: this server stays bound to topic '{TOPIC}'; the new topic needs its "
                f"own server:\n"
                f"    manage: {_portable(start)} --mode manage --registry {_portable(target)}\n"
                f"  read-mode MCP config snippet:\n{cfg}") + RULES_REMINDER


if __name__ == "__main__":
    print(f"[knowledge-mcp] mode={ARGS.mode} topic={TOPIC} version={VERSION_LABEL}"
          + (f" commit={SHA[:12]}" if SHA else "") + (" (git)" if REPO else ""), file=sys.stderr)
    mcp.run()
