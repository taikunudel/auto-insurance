#!/usr/bin/env python3
"""knowledge-mcp server: one server, two modes (read | manage) over a git-backed OKF wiki.

The knowledge of a topic is a git repo at <registry>/<topic>/repo/. The arms are branches
(v1 good, v2 silent-defect, v3 stronger-defect).

read mode    : 5 read tools, pinned to ONE commit, content served from git, read-only.
               At startup the requested ref (--version, default the repo's current branch)
               is resolved to a commit id; the server serves that commit for its whole life,
               so a run stays frozen even while a manager keeps committing.
manage mode  : all 14 tools. Checks out one arm and edits the WORKING TREE; kb_snapshot
               commits (a new immutable point); earlier commits never change.

The rules (AGENT_RULES.md) ship as the FastMCP `instructions` and via kb_rules(). The manage
tools gate on the rules: the first manage call returns the rulebook and asks the agent to retry.

Launch:
  server.py --mode read   --registry ~/knowledge --topic <t> [--version v1|v2|v3|<ref>]
  server.py --mode manage --registry ~/knowledge --topic <t> [--version <arm>]
  server.py --root <path>                      # serve a plain folder directly (read mode)

Multi-topic: point --registry at a folder of topic repos and omit --topic. The server then
holds them all; the first read tool returns a topic catalog and the agent (or user) picks one
with kb_select_topic() before content is served (the "topic gate"). Passing --topic pins one
topic at launch and skips the gate (reproducible). --select auto|manual chooses who picks and
--dynamic on|off controls whether new topics are seen live; see the args below.
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
try:                                  # optional view layer: a graph.html regenerated on each edit
    from graph import write_graph as _write_graph  # noqa: E402
except Exception:                     # a broken/absent view must never stop the server
    _write_graph = None

# --- args ----------------------------------------------------------------------------
ap = argparse.ArgumentParser(description="knowledge-mcp server (stdio).")
ap.add_argument("--mode", choices=["read", "manage"], default="read")
ap.add_argument("--registry", default=None, help="path to the registry (parent of <topic>/).")
ap.add_argument("--topic", default=None)
ap.add_argument("--version", default=None,
                help="read: ref/branch/commit to serve (default the repo's current branch); "
                     "manage: the arm (branch) to edit.")
ap.add_argument("--root", default=None, help="serve exactly this folder (read mode); bypasses git.")
ap.add_argument("--log-dir", default=None)
ap.add_argument("--name", default="knowledge")
ap.add_argument("--select", choices=["auto", "manual"], default="auto",
                help="multi-topic: how the first read picks a topic. auto = the agent chooses "
                     "from the catalog; manual = the agent asks the user. Ignored when --topic "
                     "pins one topic or only one exists.")
ap.add_argument("--dynamic", choices=["on", "off"], default="on",
                help="on = re-scan the registry for topics on each call (new topics appear "
                     "live); off = freeze the topic list at startup (reproducible).")
ARGS = ap.parse_args()


def _die(msg: str):
    sys.exit(f"[knowledge-mcp] {msg}")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- resolve registry; topics are discovered, one is activated at startup or via the gate ---
# Default the registry to the sibling `knowledge/` folder so the server works wherever the
# whole tree lives, with no absolute path needed. The registry may BE a single repo (flat
# layout) or a parent folder holding several topic repos.
_DEFAULT_REGISTRY = Path(__file__).resolve().parents[1] / "knowledge"
REGISTRY = (Path(ARGS.registry).expanduser().resolve() if ARGS.registry
            else (_DEFAULT_REGISTRY if _DEFAULT_REGISTRY.is_dir() else None))

# Active serving context — set by _activate_topic()/_activate_root(), possibly only after the
# first read call hits the topic gate. stdio means one process per session, so these module
# globals ARE this session's state (no cross-session bleed to worry about).
TOPIC = ARGS.topic
REPO = None              # active topic's git repo
SHA = None               # frozen commit served in read mode
ACTIVE_ROOT = None       # the repo (git) or a plain folder (--root); also the "is a topic active?" flag
READ_FROM_GIT = False
VERSION_LABEL = None
_ACTIVE_TOPIC = None     # selected topic name, or None while the gate is still open
_TOPICS_CACHE = None     # topic discovery cache for --dynamic off

# Layout flags. A flat registry (the registry IS one repo) or a pinned --topic is a single,
# fixed topic: no gate, no selection tools, read surface stays at the original 5. Only a
# registry-of-topics with no pin is "multi-capable": it gets kb_topics()/kb_select_topic()
# and the first-read gate. Pinning (--topic) is exactly what keeps a benchmark run reproducible.
_FLAT = bool(REGISTRY and (REGISTRY / ".git").is_dir())
_MULTI_CAPABLE = bool(REGISTRY) and not _FLAT and not ARGS.root and not ARGS.topic


def _git_in(repo, *a) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)


def _git(*a) -> subprocess.CompletedProcess:
    return _git_in(REPO, *a)


def _default_branch_of(repo) -> str:
    r = _git_in(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else "v1"


# --- topic discovery + activation + the selection gate -------------------------------
# A "topic" is one knowledge base = one git repo. The registry holds zero or more of them.
# One topic is made active before any content is served: at startup when there is no choice
# (flat repo, a single topic, or --topic pins one), otherwise lazily on the first read call,
# which returns a catalog and asks for kb_select_topic() instead of leaking every topic.
def _topic_repo(d: Path):
    """The git repo for a topic dir: <topic>/repo/ (legacy layout) or <topic>/ itself, else None."""
    if (d / "repo" / ".git").is_dir():
        return d / "repo"
    if (d / ".git").is_dir():
        return d
    return None


def _discover_topics() -> dict:
    """Map topic-name -> repo Path. A flat registry (the registry IS a repo) is one topic."""
    if not REGISTRY:
        return {}
    if (REGISTRY / ".git").is_dir():
        return {REGISTRY.name: REGISTRY}
    out = {}
    for d in sorted(REGISTRY.iterdir()):
        if d.is_dir():
            r = _topic_repo(d)
            if r:
                out[d.name] = r
    return out


def _topics_now() -> dict:
    """Current topics, honouring --dynamic (on: re-scan each call; off: frozen at first scan)."""
    global _TOPICS_CACHE
    if ARGS.dynamic == "off":
        if _TOPICS_CACHE is None:
            _TOPICS_CACHE = _discover_topics()
        return _TOPICS_CACHE
    return _discover_topics()


def _topic_desc(repo) -> str:
    """First non-heading line of a topic's index.md, used as its one-line catalog description."""
    ref = ARGS.version or _default_branch_of(repo)
    r = _git_in(repo, "show", f"{ref}:index.md")
    if r.returncode:
        return ""
    for ln in r.stdout.splitlines():
        s = ln.strip()
        if s and not s.startswith("#"):
            return s
    return ""


def _topics_catalog() -> str:
    topics = _topics_now()
    lines = [f"{len(topics)} topic(s) available (choose with kb_select_topic(<name>)):"]
    for name, repo in topics.items():
        d = _topic_desc(repo)
        lines.append(f"  - {name}" + (f" — {d}" if d else ""))
    return "\n".join(lines)


def _activate_root() -> str | None:
    """Activate --root: serve a plain folder directly (no git, single topic)."""
    global ACTIVE_ROOT, READ_FROM_GIT, VERSION_LABEL, TOPIC, _ACTIVE_TOPIC
    root = Path(ARGS.root).expanduser().resolve()
    if not root.is_dir():
        return f"--root not a folder: {root}"
    ACTIVE_ROOT, READ_FROM_GIT, VERSION_LABEL = root, False, root.name
    TOPIC = TOPIC or root.name
    _ACTIVE_TOPIC = TOPIC
    return None


def _activate_topic(name: str) -> str | None:
    """Make `name` the active topic: resolve its commit (read) or check out its arm (manage).
    Returns an error string on failure, else None. Re-callable to switch topics mid-session."""
    global REPO, TOPIC, SHA, ACTIVE_ROOT, READ_FROM_GIT, VERSION_LABEL, _ACTIVE_TOPIC
    repo = _topics_now().get(name)
    if repo is None:
        return f"ERROR: no such topic '{name}'. Call kb_topics() for the list."
    ref = ARGS.version or _default_branch_of(repo)
    if ARGS.mode == "read":
        rp = _git_in(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
        if rp.returncode:
            return f"ERROR: no such version/ref '{ref}' in topic '{name}'."
        REPO, TOPIC, SHA = repo, name, rp.stdout.strip()
        READ_FROM_GIT, ACTIVE_ROOT, VERSION_LABEL = True, repo, ref
    else:                            # manage: check out the arm and edit its working tree
        co = _git_in(repo, "checkout", ref)
        if co.returncode:
            return f"ERROR: manage: cannot check out '{ref}' in topic '{name}': {co.stderr.strip()}"
        REPO, TOPIC, SHA = repo, name, None
        READ_FROM_GIT, ACTIVE_ROOT, VERSION_LABEL = False, repo, ref
    _ACTIVE_TOPIC = name
    _log("topic_activate", selected=name, ref=ref)
    return None


_SELECT_HINT = {
    "auto": "Read the catalog below, pick the one topic that matches the user's task, and call "
            "kb_select_topic(<name>). If none clearly fits, ask the user before guessing.",
    "manual": "Ask the user which topic to use, then call kb_select_topic(<name>).",
}


def _topic_gate():
    """Read-tool gate. Returns None when a topic is active (proceed). Otherwise returns the
    catalog plus a hint, so no content is served until kb_select_topic resolves the choice.
    Idempotent: every pre-selection read returns the same catalog, never partial content."""
    if ACTIVE_ROOT is not None:
        return None
    topics = _topics_now()
    if not topics:
        return "ERROR: no topics found. Point --registry at a knowledge repo or a registry of topics."
    if len(topics) == 1:                       # no choice to make: activate silently
        return _activate_topic(next(iter(topics)))
    _log("topic_gate", n_topics=len(topics))
    return ("SELECT A TOPIC before reading. " + _SELECT_HINT.get(ARGS.select, _SELECT_HINT["auto"])
            + "\n\n" + _topics_catalog())

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


# --- decide the active context now, or defer to the topic gate -----------------------
if ARGS.root:
    _e = _activate_root()
    if _e:
        _die(_e)
else:
    if shutil.which("git") is None:            # doctor: fail loudly, do not limp on
        _die("git is required to serve a knowledge repo but was not found on PATH. Install git.")
    _t0 = _topics_now()
    if not _t0:
        _die("need --root, or --registry pointing at a knowledge repo / a registry of topics.")
    if ARGS.topic:                             # pinned at launch -> the gate is a no-op
        _e = _activate_topic(ARGS.topic)
        if _e:
            _die(_e)
    elif len(_t0) == 1:                        # only one topic -> activate it, no gate
        _e = _activate_topic(next(iter(_t0)))
        if _e:
            _die(_e)
    elif ARGS.mode == "manage":                # editing needs an explicit target topic
        _die(f"manage mode needs --topic to choose which topic to edit "
             f"(found {len(_t0)}: {', '.join(_t0)}).")
    # else: read mode, multiple topics, no --topic -> stay inactive; first read hits the gate


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


# When several topics are live (none pinned), tell the agent up front that the first read
# returns a catalog and a topic must be selected. (The rulebook itself stays topic-agnostic,
# so this note only goes into the connect-time instructions, not into kb_rules() output.)
_MULTI = _ACTIVE_TOPIC is None
INSTRUCTIONS = RULES_TEXT + (
    "\n\n[topics] This server hosts several knowledge bases (topics). The first read tool you "
    "call returns a topic catalog instead of content; choose one with kb_select_topic(<name>) "
    "(or call kb_topics() first), then read. Select again to switch topics."
    if _MULTI else "")
mcp = FastMCP(ARGS.name, instructions=INSTRUCTIONS)


# ============================ READ TOOLS (always registered) =========================
@mcp.tool()
def kb_index(folder: str = "") -> str:
    """Return the table of contents (index.md) for a folder. Empty argument means the top level."""
    g = _topic_gate()
    if g is not None:
        return g
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
    g = _topic_gate()
    if g is not None:
        return g
    pages = _pages()
    _log("kb_list", n_pages=len(pages))
    return "\n".join(pages)


@mcp.tool()
def kb_get(page_id: str) -> str:
    """Return the full raw markdown of one page, identified by its path relative to the root."""
    g = _topic_gate()
    if g is not None:
        return g
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
    g = _topic_gate()
    if g is not None:
        return g
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


# Topic navigation tools — registered ONLY for a multi-topic registry with no pin. A flat or
# pinned single-topic server never shows these, so its read surface stays at the original 5.
if _MULTI_CAPABLE:

    @mcp.tool()
    def kb_topics() -> str:
        """List the topics (knowledge bases) this server can serve, one per line with a short
        description. Call kb_select_topic(name) before the read tools will return content."""
        _log("kb_topics")
        return _topics_catalog()

    @mcp.tool()
    def kb_select_topic(name: str) -> str:
        """Choose which topic (knowledge base) the read tools serve for this session. Required
        once when the server hosts multiple topics; call again to switch. Names from kb_topics()."""
        err = _activate_topic(name.strip())
        if err:
            return err
        return (f"active topic: {TOPIC} (version {VERSION_LABEL}"
                + (f", commit {SHA[:12]}" if SHA else "")
                + "). Read tools now serve this topic only; select again to switch.")


# ============================ MANAGE TOOLS (manage mode only) =========================
if ARGS.mode == "manage":

    def _append_log(msg: str):
        with (ACTIVE_ROOT / "log.md").open("a", encoding="utf-8") as fh:
            fh.write(f"- {_now()} {msg}\n")

    def _regen_graph():
        """Regenerate <root>/graph.html so the visual view tracks every edit. Never raises:
        a view bug must not break a knowledge write (mirrors how _log swallows errors)."""
        if _write_graph is None:
            return
        try:
            _write_graph(ACTIVE_ROOT)
        except Exception:
            pass

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
        _regen_graph()
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
        _regen_graph()
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
        _regen_graph()
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
        _regen_graph()
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
        _regen_graph()
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
        _regen_graph()                # ensure the committed snapshot carries a current graph.html
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
        _regen_graph()                # the working tree now points at a different arm; refresh the view
        return (f"current arm: {old} -> {ref} (checked out)\n"
                f"  read servers started now default to {ref}\n"
                f"  already-running read servers keep their pinned commit") + RULES_REMINDER


if __name__ == "__main__":
    _status = (f"topic={TOPIC} version={VERSION_LABEL}" + (f" commit={SHA[:12]}" if SHA else "")
               + (" (git)" if REPO else "")
               if _ACTIVE_TOPIC is not None
               else f"{len(_topics_now())} topics, awaiting kb_select_topic")
    print(f"[knowledge-mcp] mode={ARGS.mode} {_status}", file=sys.stderr)
    mcp.run()
