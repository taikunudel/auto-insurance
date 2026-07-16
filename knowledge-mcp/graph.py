#!/usr/bin/env python3
"""Generate a self-contained force-directed graph.html view of an OKF knowledge base.

A third root-level *view* over the bundle, alongside the catalog (index.md) and the
chronicle (log.md): index.md shows parent->child containment; this shows how pages
RELATE across folders -- the bridges between topic areas that the text catalog hides.

Server-side: the manage tools call write_graph(root) after every change, so
<root>/graph.html always reflects the current working tree. READ-ONLY w.r.t. knowledge
pages -- it only reads *.md and writes the single graph.html at the root. graph.html is
not a knowledge document (not *.md, not a folder), so OKF validation ignores it.

Stdlib only; no build step, no dependencies, no network. Generation is just parse +
string-format (milliseconds for a few hundred pages); the force layout and all motion run
in the browser, so regenerating on every edit stays cheap.

Edge policy (rarity-weighted, IDF): a tag on every page is uninformative, exactly like
search ranking. weight(A,B) = sum over shared tags of log(N/(1+freq(tag))). Edges whose
score clears EDGE_MIN_TAG are drawn; weak "we're both in design" coincidences (~0) draw no
line. Explicit [[wikilinks]] are always drawn, distinctly.

Standalone:  python3 graph.py <kb-root>      (defaults to the sibling knowledge/ bundle)
"""
from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# Reuse the server's OKF frontmatter splitter so parsing matches the write tools exactly.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_okf import split_frontmatter, RESERVED  # noqa: E402

EDGE_MIN_TAG = 1.5          # rarity score needed to draw a tag edge; folder coincidences fall below

# type -> (dot color, legend label). Warm/muted palette tuned for the cream serif theme.
# Any type not listed falls back to FALLBACK (cycled), so this generalises to any OKF base.
TYPE_STYLE = {
    "rule":      ("#c0894e", "Rule"),
    "decision":  ("#5f86b0", "Decision"),
    "guide":     ("#5fa39a", "Guide"),
    "reference": ("#9b6f8e", "Reference"),
    "concept":   ("#d6a64f", "Concept"),
    "source":    ("#5f86b0", "Source"),
    "entity":    ("#9aa7b5", "Entity"),
    "overview":  ("#8a8992", "Overview"),
    "procedure": ("#6fae8f", "Procedure"),
    "example":   ("#c98a5b", "Example"),
}
FALLBACK = ["#c77b8b", "#7e76b8", "#6fae8f", "#c98a5b", "#7b9cc4", "#b59a52"]
UNKNOWN = ("#5b6470", "Other")


# ---- frontmatter list reader (build_okf.fm_keys is scalar-only; we also need tag lists) ----
def parse_kv(fm_lines):
    """Tiny YAML reader for the flat frontmatter this format uses: scalars, inline [a, b]
    lists, and block '- item' lists. Returns {key: str | [str]}."""
    out: dict[str, object] = {}
    i = 0
    fm_lines = fm_lines or []
    while i < len(fm_lines):
        ln = fm_lines[i].rstrip()
        if not ln or ln.startswith("#"):
            i += 1
            continue
        m = re.match(r"^([A-Za-z0-9_]+)\s*:\s*(.*)$", ln)
        if not m:
            i += 1
            continue
        k, v = m.group(1), m.group(2).strip()
        if v == "":                                   # block list on the following '-' lines
            items = []
            i += 1
            while i < len(fm_lines) and re.match(r"^\s*-\s+", fm_lines[i]):
                items.append(re.sub(r"^\s*-\s+", "", fm_lines[i]).strip().strip('"').strip("'"))
                i += 1
            out[k] = items
            continue
        if v.startswith("[") and v.endswith("]"):     # inline list
            out[k] = [x.strip().strip('"').strip("'") for x in v[1:-1].split(",") if x.strip()]
        else:
            out[k] = v.strip('"').strip("'")
        i += 1
    return out


def _skip(rel: str) -> bool:
    parts = rel.split("/")
    return ".git" in parts or "do_not_read" in parts or rel in RESERVED


def short_label(title: str, slug: str) -> str:
    """A compact graph label: the title up to its first em-dash/colon subtitle, capped."""
    s = re.split(r"\s+[—–:]\s+", (title or "").strip())[0].strip()
    if not s:
        s = slug.replace("-", " ").replace("_", " ").title()
    return s if len(s) <= 32 else s[:30].rstrip() + "…"


# ---- collect pages -------------------------------------------------------------------
def load_pages(root: Path):
    pages = []
    for p in sorted(root.rglob("*.md")):
        rel = p.relative_to(root).as_posix()
        if _skip(rel):
            continue
        fm_lines, body = split_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        if fm_lines is None:                          # no frontmatter -> not a knowledge doc
            continue
        fm = parse_kv(fm_lines)
        tags = fm.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        parts = rel.split("/")
        folder = parts[0] if len(parts) > 1 else "(root)"
        title = str(fm.get("title", "") or "").strip() or rel[:-3]
        clean_tags = [t for t in tags if t]
        meta = {k: fm[k] for k in ("description", "status", "version", "created", "updated")
                if fm.get(k) not in (None, "", [])}
        if clean_tags:
            meta["tags"] = clean_tags
        pages.append({
            "id": rel[:-3],
            "file": rel,
            "title": title,
            "type": (str(fm.get("type", "")).strip() or "document"),
            "tags": clean_tags,
            "folder": folder,
            "body": body,
            "meta": meta,                       # parsed fields, for the "Details" view
            "fmraw": "\n".join(fm_lines),        # literal YAML, for the "Raw" view
        })
    return pages


WIKI_RE = re.compile(r"\[\[([^\]]+)\]\]")


def build_edges(pages):
    """IDF-weighted shared-tag edges + explicit wikilinks. Returns (tag_scores, shared_map,
    link_set) where keys are (a_id, b_id) ordered pairs."""
    by_id = {p["id"]: p for p in pages}
    slug = {p["id"].split("/")[-1].lower(): p for p in pages}
    n = len(pages)
    tag_freq = Counter()
    for p in pages:
        for tg in set(p["tags"]):
            tag_freq[tg] += 1

    def idf(tag):
        return max(0.0, math.log(n / (1 + tag_freq[tag]))) if n else 0.0

    scores, shared_map = {}, {}
    for i, a in enumerate(pages):
        aset = set(a["tags"])
        for b in pages[i + 1:]:
            shared = aset & set(b["tags"])
            if not shared:
                continue
            score = sum(idf(tg) for tg in shared)
            if score >= EDGE_MIN_TAG:
                scores[(a["id"], b["id"])] = score
                shared_map[(a["id"], b["id"])] = sorted(shared)

    links = set()
    for a in pages:
        for target in WIKI_RE.findall(a["body"]):
            tgt = target.split("|")[0].split("#")[0].strip()
            b = by_id.get(tgt) or slug.get(tgt.lower())
            if b and b["id"] != a["id"]:
                links.add((a["id"], b["id"]))
    return scores, shared_map, links


def _type_palette(pages):
    """Assign a (color,label) to every type present, drawing on TYPE_STYLE then FALLBACK,
    plus the typeOrder (by frequency, desc) used for the legend."""
    counts = Counter(p["type"] for p in pages)
    order = [t for t, _ in counts.most_common()]
    types, fb = {}, 0
    for t in order:
        if t in TYPE_STYLE:
            color, label = TYPE_STYLE[t]
        else:
            color, label = FALLBACK[fb % len(FALLBACK)], t[:1].upper() + t[1:]
            fb += 1
        types[t] = {"color": color, "label": label}
    types["unknown"] = {"color": UNKNOWN[0], "label": UNKNOWN[1]}
    return types, order


def build_payload(root: Path):
    pages = load_pages(root)
    pages.sort(key=lambda p: p["id"])
    scores, shared_map, links = build_edges(pages)

    deg = Counter()
    for (a, b) in scores:
        deg[a] += 1; deg[b] += 1
    for (a, b) in links:
        deg[a] += 1; deg[b] += 1

    nodes = [{
        "id": p["id"],
        "type": p["type"],
        "label": p["title"],
        "short": short_label(p["title"], p["id"].split("/")[-1]),
        "path": p["file"],
        "markdown": p["body"],
        "meta": p["meta"],
        "fmraw": p["fmraw"],
    } for p in pages]

    edges = [{"from": a, "to": b, "kind": "tag", "via": shared_map[(a, b)]}
             for (a, b) in scores]
    edges += [{"from": a, "to": b, "kind": "link", "via": ["[[wikilink]]"]}
              for (a, b) in sorted(links)]

    types, type_order = _type_palette(pages)
    hero = max(deg, key=lambda k: deg[k]) if deg else (nodes[0]["id"] if nodes else "")
    return {
        "hero": hero,
        "types": types,
        "typeOrder": type_order,
        "nodes": nodes,
        "edges": edges,
    }, pages, edges


def _wiki_title(root: Path, fallback: str) -> str:
    idx = root / "index.md"
    if idx.is_file():
        for ln in idx.read_text(encoding="utf-8", errors="replace").splitlines():
            s = ln.strip()
            if s.startswith("# "):
                return s[2:].strip()
    return fallback.replace("-", " ").replace("_", " ").title()


def render(root: Path) -> tuple[str, str]:
    payload, pages, edges = build_payload(root)
    title = _wiki_title(root, root.resolve().name)
    nfolders = len({p["folder"] for p in pages})
    n_cross = sum(1 for e in edges
                  if next(p for p in pages if p["id"] == e["from"])["folder"]
                  != next(p for p in pages if p["id"] == e["to"])["folder"])
    subtitle = (f"A relationship map of {len(pages)} pages across {nfolders} folders — "
                f"rare shared-tag bridges and the wiki's explicit [[links]], with "
                f"{n_cross} crossing folder boundaries.")
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    built = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html = _TEMPLATE
    for token, val in (("@@PAYLOAD@@", blob), ("@@TITLE@@", _esc(title)),
                       ("@@SUBTITLE@@", _esc(subtitle)), ("@@NNODES@@", str(len(payload["nodes"]))),
                       ("@@NEDGES@@", str(len(edges))), ("@@BUILT@@", built)):
        html = html.replace(token, val)
    summary = (f"graph.html: {len(payload['nodes'])} nodes, {len(edges)} edges "
               f"({n_cross} cross-folder), hero={payload['hero']!r}")
    return html, summary


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def write_graph(root) -> str:
    """Render and write <root>/graph.html. Returns a one-line summary. Raises on real I/O
    errors; the server wraps this so a view bug can never break a knowledge write."""
    root = Path(root)
    html, summary = render(root)
    (root / "graph.html").write_text(html, encoding="utf-8")
    return summary


# ---- self-contained HTML (organic force-directed; adapted from the reference design) ----
_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>@@TITLE@@ — Knowledge Graph</title>
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; }
  body {
    background: var(--kg-bg, #f6f9fc); color: #23232b; overflow: hidden;
    font-family: ui-rounded, "SF Pro Rounded", -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  :root { --serif: "Iowan Old Style", "Palatino Linotype", Palatino, "Book Antiqua", Georgia, ui-serif, serif; }

  /* --- graph --- */
  .kg-edge { stroke: var(--kg-edge, #dbe3ec); stroke-width: 1; fill: none; transition: stroke .22s, opacity .22s; }
  .kg-edge.dim { opacity: 0; }
  .kg-edge.hi  { stroke: var(--kg-hi, #4f7aa8); stroke-width: 1.7; stroke-dasharray: 5 4; animation: kg-march .7s linear infinite; }
  .kg-edge-link { stroke: #c98a4e; stroke-width: 1.3; opacity: .6; }
  .kg-node { position: absolute; left: 0; top: 0; cursor: pointer; transition: opacity .5s ease; }
  .kg-node.dim { opacity: 0; pointer-events: none; }
  .kg-dot { border-radius: 50%; transition: transform .15s ease; box-shadow: 0 1px 2.5px rgba(40,30,15,.16); }
  .kg-node:hover .kg-dot { transform: scale(1.13); }
  .kg-pulse { position: absolute; left: 0; top: 0; border-radius: 50%; border: 2px solid var(--kg-hi, #4f7aa8); opacity: .5; pointer-events: none; animation: kg-pulse 2.8s cubic-bezier(.4,0,.2,1) infinite; }
  .kg-label { position: absolute; left: 0; top: 0; white-space: nowrap; pointer-events: none; opacity: 0; transition: opacity .22s;
    font-family: var(--serif); font-size: 13px; font-weight: 600; color: #2c2c34;
    text-shadow: 0 0 3px var(--kg-bg), 0 0 3px var(--kg-bg), 0 0 6px var(--kg-bg); }
  .kg-label.vis, .kg-label.show { opacity: 1; }
  .kg-label.dim { opacity: 0; }
  .kg-label.hero { font-size: 16px; font-weight: 700; color: #1c1c22; }
  @keyframes kg-march { to { stroke-dashoffset: -18; } }
  @keyframes kg-pulse { 0% { transform: scale(1); opacity: .5; } 70% { opacity: 0; } 100% { transform: scale(2.7); opacity: 0; } }

  /* --- panel helpers --- */
  .kg-hr { height: 1px; background: #ece7dc; margin: 14px 0; }
  .kg-cap { font-size: 11px; font-weight: 700; letter-spacing: .4px; color: #8a6a3f; text-transform: uppercase; }
  .kg-row { display: flex; align-items: center; justify-content: space-between; }
  .kg-switch { display: inline-block; width: 37px; height: 21px; border-radius: 999px; background: var(--kg-hi, #4f7aa8); position: relative; transition: background .2s; }
  .kg-knob { position: absolute; top: 2px; left: 2px; width: 17px; height: 17px; border-radius: 50%; background: #fff; box-shadow: 0 1px 2px rgba(0,0,0,.22); transition: transform .2s; }
  .kg-num { font-family: var(--serif); font-size: 22px; font-weight: 700; color: #1c1c22; }
  .kg-unit { font-size: 11.5px; color: #8a8992; margin-left: 5px; }

  /* --- drawer markdown --- */
  #kg-d-body h1, #kg-d-body h2, #kg-d-body h3 { font-family: var(--serif); color: #1c1c22; line-height: 1.28; margin: 1.25em 0 .45em; font-weight: 700; }
  #kg-d-body h1 { font-size: 18px; } #kg-d-body h2 { font-size: 15.5px; } #kg-d-body h3 { font-size: 13.5px; }
  #kg-d-body h2:first-child, #kg-d-body h1:first-child { margin-top: 0; }
  #kg-d-body p { margin: 0 0 .8em; }
  #kg-d-body ul { margin: 0 0 .9em 1.15em; padding: 0; }
  #kg-d-body li { margin: .3em 0; }
  #kg-d-body hr { border: 0; border-top: 1px solid #e7e2d7; margin: 1em 0; }
  #kg-d-body blockquote { margin: 0 0 .9em; padding: .65em .9em; border-left: 3px solid #c98a4e; background: #f5f1e8; border-radius: 0 8px 8px 0; color: #4a4a52; font-style: italic; }
  #kg-d-body code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .86em; background: #efeae0; padding: .1em .38em; border-radius: 5px; color: #8a4d18; }
  #kg-d-body a { color: #a85f23; font-weight: 600; text-decoration: none; border-bottom: 1px solid rgba(168,95,35,.32); }
  #kg-d-body .wikilink { color: #a85f23; font-weight: 600; cursor: pointer; border-bottom: 1px solid rgba(168,95,35,.32); }

  /* --- panel page directory --- */
  #kg-panel-body { display: flex; flex-direction: column; min-height: 0; flex: 1 1 auto; overflow-y: auto; }
  .kg-gb { border: 0; background: transparent; cursor: pointer; font-family: inherit; font-size: 11px; font-weight: 600; color: #8a8992; padding: 3px 8px; border-radius: 6px; transition: background .15s, color .15s; }
  .kg-gb.on { background: #fff; color: #5a4a2f; box-shadow: 0 1px 2px rgba(60,48,28,.12); }
  .kg-grp { font-size: 10.5px; font-weight: 700; letter-spacing: .4px; color: #8a6a3f; text-transform: uppercase; margin: 12px 0 4px; display: flex; align-items: center; gap: 7px; }
  .kg-grp .gc { width: 9px; height: 9px; border-radius: 50%; flex: none; }
  .kg-grp .gn { margin-left: auto; color: #b3ab98; font-weight: 600; letter-spacing: 0; }
  .kg-item { display: flex; align-items: center; gap: 8px; padding: 3px 6px; border-radius: 7px; cursor: pointer; font-size: 12px; color: #45454d; transition: background .12s; }
  .kg-item:hover { background: rgba(243,239,230,0.85); }
  .kg-item.sel { background: #efe7d6; color: #1c1c22; font-weight: 600; }
  .kg-item .ic { width: 8px; height: 8px; border-radius: 50%; flex: none; }
  .kg-item .nm { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  /* --- view sliders (zoom / name density) with +/- steppers --- */
  .kg-slider { display: flex; align-items: center; gap: 8px; }
  .kg-slider input[type="range"] { flex: 1; min-width: 0; accent-color: var(--kg-hi, #4f7aa8); height: 4px; cursor: pointer; }
  .kg-step { width: 22px; height: 22px; flex: none; border-radius: 6px; border: 1px solid rgba(231,226,214,0.9); background: rgba(243,239,230,0.65); color: #8a6a3f; font-size: 15px; line-height: 1; cursor: pointer; display: flex; align-items: center; justify-content: center; }
  .kg-step:active { transform: scale(0.92); }

  /* --- drawer frontmatter (Details / Raw) --- */
  .kg-meta-tabs { display: inline-flex; gap: 2px; background: rgba(243,239,230,0.7); border: 1px solid rgba(231,226,214,0.9); border-radius: 8px; padding: 2px; }
  .kg-desc { font-size: 12.5px; line-height: 1.5; color: #5c5b63; font-style: italic; margin: 0 0 8px; }
  .kg-mrow { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: 0 0 8px; }
  .kg-mrow:last-child { margin-bottom: 0; }
  .kg-pill { font-size: 11px; padding: 2px 9px; border-radius: 999px; background: #f5f1e8; border: 1px solid #e7e2d6; color: #6c6b73; }
  .kg-pill.status { font-weight: 600; color: #5a6a4a; }
  .kg-tag { font-size: 11px; padding: 1px 8px; border-radius: 999px; background: rgba(201,138,78,0.12); border: 1px solid rgba(201,138,78,0.3); color: #8a4d18; }
  .kg-d-raw { margin: 0; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; line-height: 1.55; color: #4a4a52; background: #f5f1e8; border: 1px solid #e7e2d6; border-radius: 8px; padding: 10px 12px; overflow-x: auto; white-space: pre; }

  /* --- dark mode (data-theme="dark" on <html>); !important overrides element inline styles --- */
  :root[data-theme="dark"] body { color: #d7d5dd; }
  :root[data-theme="dark"] .kg-label { color: #d2d0d8; }
  :root[data-theme="dark"] .kg-label.hero { color: #f3f1f6; }
  :root[data-theme="dark"] .kg-hr { background: #2a2f3a; }
  :root[data-theme="dark"] .kg-cap { color: #c9a36a; }
  :root[data-theme="dark"] .kg-num { color: #ecebf0; }
  :root[data-theme="dark"] .kg-unit { color: #8e8c96; }
  :root[data-theme="dark"] #kg-panel { background: rgba(26,29,37,0.72) !important; border-color: rgba(86,92,107,0.5) !important; box-shadow: 0 14px 44px rgba(0,0,0,0.5) !important; }
  :root[data-theme="dark"] #kg-panel h1 { color: #f1eff4 !important; }
  :root[data-theme="dark"] #kg-panel-body p { color: #9a98a4 !important; }
  :root[data-theme="dark"] #kg-theme, :root[data-theme="dark"] #kg-collapse { background: rgba(44,49,60,0.7) !important; border-color: rgba(86,92,107,0.55) !important; color: #c9a36a !important; }
  :root[data-theme="dark"] #kg-groupby, :root[data-theme="dark"] .kg-meta-tabs { background: rgba(44,49,60,0.7) !important; border-color: rgba(86,92,107,0.55) !important; }
  :root[data-theme="dark"] .kg-gb { color: #9a98a4; }
  :root[data-theme="dark"] .kg-step { background: rgba(44,49,60,0.7); border-color: rgba(86,92,107,0.55); color: #c9a36a; }
  :root[data-theme="dark"] .kg-gb.on { background: #3a4150; color: #f0eef4; box-shadow: 0 1px 2px rgba(0,0,0,0.4); }
  :root[data-theme="dark"] .kg-grp { color: #c9a36a; }
  :root[data-theme="dark"] .kg-grp .gn { color: #76747e; }
  :root[data-theme="dark"] .kg-item { color: #c4c2cc; }
  :root[data-theme="dark"] .kg-item:hover { background: rgba(255,255,255,0.06); }
  :root[data-theme="dark"] .kg-item.sel { background: #2d333f; color: #f1eff4; }
  :root[data-theme="dark"] #kg-drawer { background: #1a1d25 !important; border-color: #2c313c !important; box-shadow: -20px 0 50px rgba(0,0,0,0.5) !important; }
  :root[data-theme="dark"] #kg-d-type { color: #8e8c96 !important; }
  :root[data-theme="dark"] #kg-d-close { background: #2a2f3a !important; color: #b8b6c0 !important; border-color: #3a4150 !important; }
  :root[data-theme="dark"] #kg-d-title { color: #f1eff4 !important; }
  :root[data-theme="dark"] #kg-d-path { color: #87858f !important; }
  :root[data-theme="dark"] #kg-d-body { color: #c4c2cc !important; }
  :root[data-theme="dark"] #kg-d-body h1, :root[data-theme="dark"] #kg-d-body h2, :root[data-theme="dark"] #kg-d-body h3 { color: #f1eff4; }
  :root[data-theme="dark"] #kg-d-body hr { border-top-color: #2c313c; }
  :root[data-theme="dark"] #kg-d-body blockquote { background: #232732; color: #b6b4be; border-left-color: #c98a4e; }
  :root[data-theme="dark"] #kg-d-body code { background: #2a2f3a; color: #e0a868; }
  :root[data-theme="dark"] #kg-d-body a, :root[data-theme="dark"] #kg-d-body .wikilink { color: #e0a868; border-bottom-color: rgba(224,168,104,0.35); }
  :root[data-theme="dark"] .kg-chip { background: #232732 !important; border-color: #333a47 !important; color: #c4c2cc !important; }
  :root[data-theme="dark"] .kg-desc { color: #9d9ba6; }
  :root[data-theme="dark"] .kg-pill { background: #232732; border-color: #333a47; color: #b0aeba; }
  :root[data-theme="dark"] .kg-pill.status { color: #9ec48a; }
  :root[data-theme="dark"] .kg-tag { background: rgba(216,165,102,0.16); border-color: rgba(216,165,102,0.4); color: #e0a868; }
  :root[data-theme="dark"] .kg-d-raw { background: #20242d; border-color: #2f3440; color: #c0bec8; }
</style>
</head>
<body>
<div id="kg-stage" style="position: fixed; inset: 0; z-index: 1;">
  <svg id="kg-edges" style="position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible; pointer-events: none;"></svg>
  <div id="kg-nodes" style="position: absolute; inset: 0;"></div>
  <div id="kg-labels" style="position: absolute; inset: 0; pointer-events: none;"></div>
</div>

<div id="kg-panel" style="position: fixed; top: 26px; left: 26px; z-index: 12; display: flex; flex-direction: column; max-height: calc(100vh - 52px); width: 286px; padding: 20px 20px 18px; border-radius: 16px; background: rgba(255,255,255,0.62); backdrop-filter: blur(20px) saturate(165%); -webkit-backdrop-filter: blur(20px) saturate(165%); border: 1px solid rgba(231,226,214,0.85); box-shadow: 0 14px 44px rgba(60,48,28,0.13);">
  <div class="kg-row" style="align-items: flex-start; gap: 10px;">
    <div>
      <div class="kg-cap">Knowledge Graph</div>
      <h1 style="margin: 7px 0 0; font-family: var(--serif); font-size: 21px; font-weight: 700; line-height: 1.22; color: #1c1c22;">@@TITLE@@</h1>
    </div>
    <div style="display: flex; gap: 6px; flex: none; margin-top: 2px;">
      <button id="kg-theme" aria-label="Toggle dark mode" style="width: 27px; height: 27px; border-radius: 8px; background: rgba(243,239,230,0.65); border: 1px solid rgba(231,226,214,0.9); cursor: pointer; font-size: 13px; line-height: 1; display: flex; align-items: center; justify-content: center;">🌙</button>
      <button id="kg-collapse" aria-label="Collapse panel" style="width: 27px; height: 27px; border-radius: 8px; background: rgba(243,239,230,0.65); border: 1px solid rgba(231,226,214,0.9); color: #8a6a3f; cursor: pointer; font-size: 13px; line-height: 1; display: flex; align-items: center; justify-content: center; transition: transform .25s;">▾</button>
    </div>
  </div>

  <div id="kg-panel-body">
    <p style="margin: 11px 0 0; font-size: 12.5px; line-height: 1.55; color: #6c6b73;">@@SUBTITLE@@</p>

    <div class="kg-hr"></div>
    <div class="kg-cap" style="margin-bottom: 4px;">Page type</div>
    <div id="kg-cats" style="display: flex; flex-direction: column; gap: 1px;"></div>

    <div class="kg-hr"></div>
    <div class="kg-row">
      <span style="font-size: 12.5px; color: #45454d;">Show all nodes</span>
      <button id="kg-allnodes" role="switch" aria-checked="true" style="border: 0; padding: 0; background: transparent; cursor: pointer; display: inline-flex;">
        <span id="kg-allnodes-track" class="kg-switch"><span id="kg-allnodes-knob" class="kg-knob" style="transform: translateX(16px);"></span></span>
      </button>
    </div>

    <div class="kg-hr"></div>
    <div class="kg-cap" style="margin-bottom: 6px;">Zoom</div>
    <div id="kg-zoomctl" class="kg-slider">
      <button class="kg-step" data-z="-" aria-label="Zoom out">−</button>
      <input id="kg-zoom" type="range" min="0.4" max="2.6" step="0.02" aria-label="Zoom">
      <button class="kg-step" data-z="+" aria-label="Zoom in">+</button>
    </div>
    <div class="kg-hr"></div>
    <div style="display: flex; gap: 18px; font-variant-numeric: tabular-nums;">
      <div><span class="kg-num">@@NNODES@@</span><span class="kg-unit">pages</span></div>
      <div><span class="kg-num">@@NEDGES@@</span><span class="kg-unit">links</span></div>
    </div>

    <p style="margin: 13px 0 0; font-size: 11px; line-height: 1.5; color: #9b9aa2;">Node size reflects its connections. Drag to pan, scroll to zoom — more names appear as you zoom in. Click any node to read its page.</p>
    <p style="margin: 8px 0 0; font-size: 10px; line-height: 1.5; color: #b7b6bd;">Regenerated automatically on every edit · @@BUILT@@</p>

    <div class="kg-hr"></div>
    <div class="kg-row" style="margin-bottom: 8px;">
      <div class="kg-cap">View</div>
      <div id="kg-view" style="display: inline-flex; gap: 2px; background: rgba(243,239,230,0.7); border: 1px solid rgba(231,226,214,0.9); border-radius: 8px; padding: 2px;">
        <button class="kg-gb" data-v="net">Net</button>
        <button class="kg-gb" data-v="grid">Grid</button>
      </div>
    </div>
    <div class="kg-row" style="margin-bottom: 8px;">
      <div class="kg-cap">Pages</div>
      <div id="kg-groupby" style="display: inline-flex; gap: 2px; background: rgba(243,239,230,0.7); border: 1px solid rgba(231,226,214,0.9); border-radius: 8px; padding: 2px;">
        <button class="kg-gb" data-g="type">Type</button>
        <button class="kg-gb" data-g="folder">Folder</button>
        <button class="kg-gb" data-g="az">A–Z</button>
      </div>
    </div>
    <div id="kg-nodelist"></div>
  </div>
</div>

<aside id="kg-drawer" style="position: fixed; inset: 26px 26px 26px 338px; z-index: 30; transform: translateX(112%); transition: transform .4s cubic-bezier(.4,0,.2,1); display: flex; flex-direction: column; background: #fff; border: 1px solid #e7e2d6; border-radius: 18px; box-shadow: -20px 0 50px rgba(60,48,28,0.14);">
  <button id="kg-d-close" aria-label="Close" style="position: absolute; top: 18px; right: 18px; z-index: 2; background: #f3efe6; color: #6c6b73; border: 1px solid #e7e2d6; width: 30px; height: 30px; border-radius: 50%; font-size: 18px; line-height: 1; cursor: pointer;">×</button>
  <div id="kg-d-scroll" style="flex: 1; min-height: 0; overflow: auto;">
  <div style="padding: 24px 24px 0; padding-left: max(24px, calc((100% - 760px) / 2)); padding-right: max(24px, calc((100% - 760px) / 2));">
    <div id="kg-d-type" style="display: inline-flex; align-items: center; gap: 7px; font-size: 10.5px; font-weight: 700; letter-spacing: .8px; color: #8a8992;"></div>
    <h2 id="kg-d-title" style="margin: 11px 0 0; font-family: var(--serif); font-size: 21px; font-weight: 700; line-height: 1.26; color: #1c1c22; text-wrap: pretty;"></h2>
    <div id="kg-d-path" style="margin-top: 8px; font-size: 11px; color: #a09e9a; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; word-break: break-all;"></div>
    <div id="kg-d-meta" style="margin-top: 16px;">
      <div class="kg-row" style="margin-bottom: 9px;">
        <div class="kg-cap">Frontmatter</div>
        <div id="kg-meta-tabs" class="kg-meta-tabs">
          <button class="kg-gb" data-m="clean">Details</button>
          <button class="kg-gb" data-m="raw">Raw</button>
        </div>
      </div>
      <div id="kg-d-meta-clean"></div>
      <pre id="kg-d-meta-raw" class="kg-d-raw" style="display: none;"></pre>
    </div>
    <div id="kg-d-related" style="display: flex; flex-wrap: wrap; gap: 7px; margin-top: 16px;"></div>
  </div>
  <div id="kg-d-body" style="padding: 18px 24px 30px; padding-left: max(24px, calc((100% - 760px) / 2)); padding-right: max(24px, calc((100% - 760px) / 2)); font-size: 13px; line-height: 1.72; color: #3c3c44;"></div>
  </div>
</aside>

<script>window.GRAPH_DATA = @@PAYLOAD@@;</script>
<script>
// Knowledge graph: physics layout solved once, then nodes drift gently while the
// camera (pan/zoom) and overlays (hover, drawer, category toggles) stay live.
// run() lists the build order; each step below is one small method.
class KG {
  // ---- small utilities ----
  $(id) { return document.getElementById(id); }
  rgb(h) { const n = parseInt(h.slice(1), 16); return [n >> 16 & 255, n >> 8 & 255, n & 255]; }
  shade(h, p) { const f = v => Math.max(0, Math.min(255, Math.round(v + v * p / 100))); const [r, g, b] = this.rgb(h); return `rgb(${f(r)},${f(g)},${f(b)})`; }
  alpha(h, a) { const [r, g, b] = this.rgb(h); return `rgba(${r},${g},${b},${a})`; }
  norm(s) { return (s || '').toLowerCase().replace(/[^a-z0-9]/g, ''); }
  esc(s) { return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

  async run() {
    const data = window.GRAPH_DATA;
    if (!data || !data.nodes.length) return;
    this.C = {
      bg: '#f6f9fc', edge: '#dbe3ec', hi: '#4f7aa8',
      bgDark: '#14161c', edgeDark: '#2c3340', hiDark: '#6ea0d0',
      hero: data.hero,
      types: data.types,
      order: data.typeOrder,
      fallback: ['#c77b8b', '#7e76b8', '#6fae8f', '#c98a5b', '#7b9cc4'],
      sim: { repel: 13000, link: 180, stiff: 0.03, gravity: 0.0065, collide: 30, steps: 760 }
    };
    this.stage = this.$('kg-stage'); this.svg = this.$('kg-edges'); this.layer = this.$('kg-nodes'); this.labelLayer = this.$('kg-labels');
    this.bindTheme();

    this.buildModel(data);
    this.buildDom();
    this.solve();
    this.measure(); this.fit(); this.lod(); this.render();
    this.enter();
    this.animate();
    this.bindCamera();
    this.bindHover();
    this.bindDrawer();
    this.bindCategories();
    this.bindNodeList();
    this.bindPanel();
    this.bindControls();
    this.bindView();
  }

  // ---- model: nodes, edges, adjacency, display props ----
  buildModel(data) {
    const C = this.C;
    this.byId = {}; this.idx = {};
    this.nodes = data.nodes.map(d => ({ ...d, x: 0, y: 0, vx: 0, vy: 0, deg: 0, fixed: d.id === C.hero }));
    this.nodes.forEach(n => { this.byId[n.id] = n; });
    this.edges = data.edges.map(e => ({ a: this.byId[e.from], b: this.byId[e.to], kind: e.kind })).filter(e => e.a && e.b);
    this.adj = {}; this.nodes.forEach(n => { this.adj[n.id] = new Set(); });
    this.edges.forEach(e => { e.a.deg++; e.b.deg++; this.adj[e.a.id].add(e.b.id); this.adj[e.b.id].add(e.a.id); });

    this.nodes.forEach(n => {
      const t = C.types[n.type] || C.types.unknown;
      n.color = t.color; n.typeLabel = t.label; n.hero = n.id === C.hero;
      n.name = n.short || n.label || n.id.split('/').pop();
      const major = n.hero || n.deg >= 4;
      n.tier = n.hero ? 0 : major ? 1 : 2;
      n.r = n.hero ? 24 : n.tier === 1 ? 10 + Math.min(6, n.deg) : 6 + Math.min(5, n.deg * 1.2);
      n.labelAt = n.hero ? 0 : n.tier === 1 ? 0.55 : Math.max(0.92, 1.5 - n.deg * 0.1);   // min zoom to show label
      this.idx[this.norm(n.label)] = n.id; this.idx[this.norm(n.id.split('/').pop())] = n.id; this.idx[this.norm(n.id)] = n.id;
    });
  }

  paintDot(n) {
    const s = n.dot.style;
    if (n.type === 'entity') {
      s.background = '#fff'; s.border = '1.5px solid ' + n.color; s.boxShadow = '0 1px 2.5px rgba(40,30,15,.14)';
    } else {
      s.background = `radial-gradient(circle at 36% 30%, ${this.shade(n.color, 22)}, ${n.color} 64%)`;
      s.border = (n.hero ? 2 : 1.1) + 'px solid ' + this.shade(n.color, -30);
      s.boxShadow = n.hero ? `0 0 0 5px ${this.alpha(n.color, .16)}, 0 2px 6px rgba(40,30,15,.22)` : '0 1px 2.5px rgba(40,30,15,.16)';
    }
  }

  // ---- DOM: one <line> per edge, a dot + label per node ----
  buildDom() {
    const NS = 'http://www.w3.org/2000/svg';
    this.svg.innerHTML = this.layer.innerHTML = this.labelLayer.innerHTML = '';
    this.edges.forEach(e => { e.el = document.createElementNS(NS, 'line'); e.el.setAttribute('class', 'kg-edge' + (e.kind === 'link' ? ' kg-edge-link' : '')); this.svg.appendChild(e.el); });
    this.nodes.forEach(n => {
      const el = document.createElement('div'); el.className = 'kg-node'; el.dataset.id = n.id;
      n.dot = document.createElement('div'); n.dot.className = 'kg-dot';
      const d = n.r * 2; n.dot.style.width = n.dot.style.height = d + 'px';
      if (n.hero) { const p = document.createElement('div'); p.className = 'kg-pulse'; p.style.width = p.style.height = d + 'px'; el.appendChild(p); }
      this.paintDot(n);
      el.appendChild(n.dot);
      n.lab = document.createElement('div'); n.lab.className = 'kg-label' + (n.hero ? ' hero' : ''); n.lab.textContent = n.name;
      this.layer.appendChild(el); this.labelLayer.appendChild(n.lab);
      n.el = el;
      // seed positions (hero centred) + per-node drift phase
      if (n.hero) { n.x = n.y = 0; }
      else { const a = Math.random() * 6.283, r = (n.tier === 1 ? 150 : 290) + Math.random() * 90; n.x = Math.cos(a) * r; n.y = Math.sin(a) * r; }
      n.phx = Math.random() * 6.283; n.phy = Math.random() * 6.283;
      n.amp = n.hero ? 0 : n.tier === 1 ? 3.6 : 6.8; n.spd = 0.32 + Math.random() * 0.42;
    });
  }

  // ---- force-directed layout, run to rest then frozen as each node's base (bx,by) ----
  solve() {
    const { repel, link, stiff, gravity, collide, steps } = this.C.sim;
    const N = this.nodes, E = this.edges;
    let alpha = 1;
    for (let s = 0; s < steps; s++) {
      N.forEach(n => { n.ax = 0; n.ay = 0; });
      for (let i = 0; i < N.length; i++) for (let j = i + 1; j < N.length; j++) {       // repulsion
        const p = N[i], q = N[j]; let dx = p.x - q.x, dy = p.y - q.y, d2 = dx * dx + dy * dy || 1;
        const d = Math.sqrt(d2), f = repel * (p.r * q.r / 200) / d2, fx = dx / d * f, fy = dy / d * f;
        p.ax += fx; p.ay += fy; q.ax -= fx; q.ay -= fy;
      }
      E.forEach(e => {                                                                   // links pull together
        const p = e.a, q = e.b; let dx = q.x - p.x, dy = q.y - p.y, d = Math.hypot(dx, dy) || 1;
        const f = stiff * (d - link), fx = dx / d * f, fy = dy / d * f;
        p.ax += fx; p.ay += fy; q.ax -= fx; q.ay -= fy;
      });
      N.forEach(n => {                                                                   // integrate (+ gravity to centre)
        if (n.fixed) { n.x = n.y = n.vx = n.vy = 0; return; }
        n.ax -= n.x * gravity; n.ay -= n.y * gravity;
        n.vx = (n.vx + n.ax * alpha) * 0.85; n.vy = (n.vy + n.ay * alpha) * 0.85;
        const sp = Math.hypot(n.vx, n.vy); if (sp > 40) { n.vx *= 40 / sp; n.vy *= 40 / sp; }
        n.x += n.vx; n.y += n.vy;
      });
      for (let k = 0; k < 2; k++) for (let i = 0; i < N.length; i++) for (let j = i + 1; j < N.length; j++) {  // de-overlap
        const p = N[i], q = N[j]; let dx = q.x - p.x, dy = q.y - p.y, d = Math.hypot(dx, dy) || 0.01, min = p.r + q.r + collide;
        if (d < min) { const push = (min - d) / 2, ux = dx / d, uy = dy / d; if (!p.fixed) { p.x -= ux * push; p.y -= uy * push; } if (!q.fixed) { q.x += ux * push; q.y += uy * push; } }
      }
      alpha = Math.max(alpha * 0.99, 0.02);
    }
    // freeze base position + fan each label outward from centre
    N.forEach(n => {
      n.bx = n.x; n.by = n.y;
      const dist = Math.hypot(n.x, n.y);
      if (dist < 55) { n.lox = 0; n.loy = n.r + 9; n.lanchor = 'c'; }
      else { const cx = n.x / dist, cy = n.y / dist, off = n.r + 7; n.lox = cx * off; n.loy = cy * off; n.lanchor = cx > 0.4 ? 'l' : cx < -0.4 ? 'r' : 'c'; }
    });
  }

  // ---- camera + per-frame painting ----
  measure() { const r = this.stage.getBoundingClientRect(); this.w = r.width; this.h = r.height; }
  fit() {
    let a = Infinity, b = -Infinity, c = Infinity, d = -Infinity;
    this.nodes.forEach(n => { a = Math.min(a, n.x - n.r); b = Math.max(b, n.x + n.r); c = Math.min(c, n.y - n.r); d = Math.max(d, n.y + n.r); });
    const padL = this.w < 760 ? 60 : 340, padR = 70, padY = 96, gw = (b - a) + 120, gh = (d - c) + 64;
    this.scale = Math.max(0.3, Math.min(1.4, Math.min((this.w - padL - padR) / gw, (this.h - padY * 2) / gh)));
    this.tx = padL + (this.w - padL - padR) / 2 - (a + b) / 2 * this.scale;
    this.ty = this.h / 2 - (c + d) / 2 * this.scale;
    this._syncZoom();
  }
  lod() { const boost = (this.density || 0) * 2; this.nodes.forEach(n => n.lab.classList.toggle('vis', (this.scale + boost) >= n.labelAt)); }   // zoom + density label reveal
  render() {
    const { tx, ty, scale: s } = this;
    this.nodes.forEach(n => {
      const x = tx + n.x * s, y = ty + n.y * s;
      n.el.style.transform = `translate(${x}px,${y}px) translate(-50%,-50%)`;
      const inner = n.lanchor === 'l' ? 'translate(0,-50%)' : n.lanchor === 'r' ? 'translate(-100%,-50%)' : `translate(-50%,${n.loy < 0 ? '-100%' : '0'})`;
      n.lab.style.transform = `translate(${x + n.lox}px,${y + n.loy}px) ${inner}`;
    });
    this.edges.forEach(e => { e.el.setAttribute('x1', tx + e.a.x * s); e.el.setAttribute('y1', ty + e.a.y * s); e.el.setAttribute('x2', tx + e.b.x * s); e.el.setAttribute('y2', ty + e.b.y * s); });
  }

  // ---- motion: staggered fade-in, then gentle perpetual drift ----
  enter() {
    this.edges.forEach(e => { e.el.style.opacity = '0'; e.el.style.transition = 'opacity .6s ease'; });
    this.nodes.forEach(n => { n.el.style.opacity = '0'; });
    setTimeout(() => this.edges.forEach(e => { e.el.style.opacity = ''; }), 260);
    [...this.nodes].sort((p, q) => Math.hypot(p.x, p.y) - Math.hypot(q.x, q.y))
      .forEach((n, k) => setTimeout(() => { n.el.style.opacity = ''; }, 120 + k * 26));
  }
  animate() {
    const t0 = performance.now();
    const frame = now => {
      const t = (now - t0) / 1000;
      this.nodes.forEach(n => { if (n.amp) { n.x = n.bx + Math.sin(t * n.spd + n.phx) * n.amp; n.y = n.by + Math.cos(t * n.spd * 0.92 + n.phy) * n.amp; } });
      this.render();
      this._raf = requestAnimationFrame(frame);
    };
    this._raf = requestAnimationFrame(frame);
  }

  // ---- highlight a node and its neighbours (null = clear) ----
  highlight(id) {
    const on = new Set(id ? [id, ...this.adj[id]] : []);
    this.nodes.forEach(n => { const k = !id || on.has(n.id); n.el.classList.toggle('dim', !k); n.lab.classList.toggle('dim', !k); n.lab.classList.toggle('show', !!id && k && n.tier === 2); });
    this.edges.forEach(e => { const k = id && (e.a.id === id || e.b.id === id); e.el.classList.toggle('hi', !!k); e.el.classList.toggle('dim', !!id && !k); });
  }

  bindCamera() {
    const st = this.stage; st.style.cursor = 'grab';
    let pressId = null, panning = false, down = false, lx = 0, ly = 0, moved = 0;
    st.addEventListener('pointerdown', e => { const el = e.target.closest('.kg-node'); lx = e.clientX; ly = e.clientY; moved = 0; down = true; pressId = el && el.dataset.id; panning = !el; if (panning) st.style.cursor = 'grabbing'; });
    this._onMove = e => {
      if (!down || (pressId == null && !panning)) return;
      const dx = e.clientX - lx, dy = e.clientY - ly; lx = e.clientX; ly = e.clientY; moved += Math.abs(dx) + Math.abs(dy);
      if (panning) { this.tx += dx; this.ty += dy; this.moved = true; }
    };
    this._onUp = () => {
      if (!down) return;                       // press began outside the stage (e.g. in the reading card): not ours
      if (moved < 5) { if (pressId != null) this.select(pressId); else if (this.selected) this.deselect(); }
      if (panning) st.style.cursor = 'grab'; pressId = null; panning = false; down = false;
    };
    window.addEventListener('pointermove', this._onMove);
    window.addEventListener('pointerup', this._onUp);
    st.addEventListener('wheel', e => {
      e.preventDefault();
      const r = st.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
      const wx = (mx - this.tx) / this.scale, wy = (my - this.ty) / this.scale;
      this.scale = Math.max(0.4, Math.min(2.6, this.scale * (e.deltaY < 0 ? 1.12 : 0.893)));
      this.tx = mx - wx * this.scale; this.ty = my - wy * this.scale; this.moved = true; this.lod(); this._syncZoom();
    }, { passive: false });
    this._onResize = () => { this.measure(); if (!this.moved) { if (this.gridView) this.gridLayout(); else this.fit(); this.lod(); this.render(); } };
    window.addEventListener('resize', this._onResize);
  }

  bindHover() {
    this.layer.addEventListener('pointerover', e => { const el = e.target.closest('.kg-node'); if (el && !this.selected) this.highlight(el.dataset.id); });
    this.layer.addEventListener('pointerout', e => { const el = e.target.closest('.kg-node'); if (el && !this.selected) this.highlight(null); });
  }

  // ---- side drawer + tiny Markdown renderer for the wiki page ----
  md(src) {
    const inline = s => this.esc(s)
      .replace(/\[\[([^\]]+)\]\]/g, (_, p) => { const [t, d] = p.split('|'); const id = this.idx[this.norm(t)]; const txt = this.esc(d || t); return id ? `<span class="wikilink" data-link="${this.norm(t)}">${txt}</span>` : `<span style="color:#a85f23;font-weight:600">${txt}</span>`; })
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, t, u) => /^https?:\/\//.test(u) ? `<a href="${u}" target="_blank" rel="noopener">${t}</a>` : t)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    let html = '', list = false, para = [];
    const flushP = () => { if (para.length) { html += `<p>${inline(para.join(' '))}</p>`; para = []; } };
    const endL = () => { if (list) { html += '</ul>'; list = false; } };
    (src || '').replace(/^\s*---[\s\S]*?---\s*/, '').split('\n').forEach(raw => {
      const line = raw.trim(); let m;
      if (!line) { flushP(); endL(); }
      else if (m = line.match(/^(#{1,6})\s+(.*)/)) { flushP(); endL(); const lv = Math.min(3, m[1].length); html += `<h${lv}>${inline(m[2])}</h${lv}>`; }
      else if (/^(---+|\*\*\*+)$/.test(line)) { flushP(); endL(); html += '<hr>'; }
      else if (m = line.match(/^>\s?(.*)/)) { flushP(); endL(); html += `<blockquote>${inline(m[1])}</blockquote>`; }
      else if (m = line.match(/^[-*]\s+(.*)/)) { flushP(); if (!list) { html += '<ul>'; list = true; } html += `<li>${inline(m[1])}</li>`; }
      else { endL(); para.push(line); }
    });
    flushP(); endL();
    return html;
  }
  select(id) {
    const n = this.byId[id]; if (!n) return;
    this.selected = id; this.highlight(id);
    this.$('kg-d-type').innerHTML = `<span style="width:9px;height:9px;border-radius:50%;background:${n.color}"></span>${(n.typeLabel || '').toUpperCase()}`;
    this.$('kg-d-title').textContent = n.label;
    const pth = n.path || '';
    this.$('kg-d-path').innerHTML = pth ? `<a href="${this.esc(pth)}" target="_blank" rel="noopener" style="color:inherit;border:0">${this.esc(pth)} ↗</a>` : '';
    this.renderMeta(n);
    const rel = [...this.adj[id]].map(i => this.byId[i]).sort((a, b) => b.deg - a.deg).slice(0, 12);
    this.$('kg-d-related').innerHTML = rel.map(r => `<span class="kg-chip" data-id="${r.id}" style="cursor:pointer;display:inline-flex;align-items:center;gap:6px;padding:5px 10px;border-radius:999px;background:#f5f1e8;border:1px solid #e7e2d6;font-size:11.5px;color:#45454d"><span style="width:7px;height:7px;border-radius:50%;background:${r.color}"></span>${this.esc(r.name)}</span>`).join('');
    this.$('kg-d-body').innerHTML = this.md(n.markdown); this.$('kg-d-scroll').scrollTop = 0;
    this.$('kg-drawer').style.transform = 'translateX(0)';
    this.syncNodeListSel();
  }
  deselect() { this.selected = null; this.highlight(null); this.$('kg-drawer').style.transform = 'translateX(112%)'; this.syncNodeListSel(); }
  bindDrawer() {
    this.metaMode = 'clean';
    this.$('kg-d-close').addEventListener('click', () => this.deselect());
    this.$('kg-meta-tabs').addEventListener('click', e => { const b = e.target.closest('.kg-gb'); if (!b) return; this.metaMode = b.dataset.m; this.applyMetaMode(); });
    this.$('kg-d-related').addEventListener('click', e => { const c = e.target.closest('.kg-chip'); if (c) this.select(c.dataset.id); });
    this.$('kg-d-body').addEventListener('click', e => { const w = e.target.closest('.wikilink'); if (w) { const id = this.idx[w.dataset.link]; if (id) this.select(id); } });
  }

  // ---- legend built from the types actually present, each a show/hide toggle ----
  bindCategories() {
    const C = this.C, counts = {};
    this.nodes.forEach(n => { counts[n.type] = (counts[n.type] || 0) + 1; });
    const present = Object.keys(counts).sort((a, b) => (C.order.indexOf(a) + 1 || 99) - (C.order.indexOf(b) + 1 || 99));
    let fb = 0; this.cats = {};
    present.forEach(t => {
      const known = C.types[t], color = known ? known.color : C.fallback[fb++ % C.fallback.length];
      this.cats[t] = { color, label: known ? known.label : t[0].toUpperCase() + t.slice(1), count: counts[t] };
      if (!known) this.nodes.forEach(n => { if (n.type === t) { n.color = color; this.paintDot(n); } });
    });

    this.hidden = new Set();
    const wrap = this.$('kg-cats'); this.switches = {};
    present.forEach(t => {
      const m = this.cats[t];
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:center;justify-content:space-between;gap:10px;padding:5px 0;cursor:pointer';
      row.innerHTML = `<div style="display:flex;align-items:center;gap:9px"><span style="width:12px;height:12px;border-radius:50%;background:${m.color}"></span><span style="font-size:12.5px;color:#45454d">${m.label}</span><span style="font-size:11px;color:#a3a19a">${m.count}</span></div>`;
      const track = document.createElement('span'); track.className = 'kg-switch'; track.style.width = '31px'; track.style.height = '18px';
      const knob = document.createElement('span'); knob.className = 'kg-knob'; knob.style.width = knob.style.height = '14px';
      track.appendChild(knob); row.appendChild(track);
      this.switches[t] = { track, knob };
      row.addEventListener('click', () => { this.hidden.has(t) ? this.hidden.delete(t) : this.hidden.add(t); this.applyVisibility(); });
      wrap.appendChild(row);
    });
    this.present = present;
    this.$('kg-allnodes').addEventListener('click', () => { this.hidden.size ? this.hidden.clear() : present.forEach(t => this.hidden.add(t)); this.applyVisibility(); });
    this.applyVisibility();
  }
  applyVisibility() {
    this.nodes.forEach(n => { const hid = this.hidden.has(n.type); n.el.style.display = n.lab.style.display = hid ? 'none' : ''; });
    this.edges.forEach(e => { e.el.style.display = (this.hidden.has(e.a.type) || this.hidden.has(e.b.type)) ? 'none' : ''; });
    this.present.forEach(t => { const on = !this.hidden.has(t); const s = this.switches[t]; s.track.style.background = on ? this.cats[t].color : '#cbd3dd'; s.knob.style.transform = on ? 'translateX(13px)' : 'none'; });
    const allOn = this.hidden.size === 0;
    this.$('kg-allnodes-track').style.background = allOn ? this.C.hi : '#cbd3dd';
    this.$('kg-allnodes-knob').style.transform = allOn ? 'translateX(16px)' : 'none';
  }

  bindPanel() {
    const btn = this.$('kg-collapse'), body = this.$('kg-panel-body');
    btn.addEventListener('click', () => {
      const open = body.style.display === 'none';
      body.style.display = open ? '' : 'none';
      btn.style.transform = open ? '' : 'rotate(-90deg)';
    });
  }

  // ---- Net | Grid chooser (segmented, by the PAGES list): Net = force layout, Grid = screen-filling rectangular grid. ----
  bindView() {
    this.gridView = false;
    const mark = v => [...this.$('kg-view').children].forEach(b => b.classList.toggle('on', b.dataset.v === v));
    this.$('kg-view').addEventListener('click', e => {
      const b = e.target.closest('.kg-gb'); if (!b) return;
      const grid = b.dataset.v === 'grid';
      if (grid === this.gridView) return;                                       // no-op when re-clicking the active view
      this.gridView = grid;
      if (grid) this.gridLayout(); else { this.restoreForce(); this.fit(); }
      this.svg.style.opacity = grid ? '0.4' : '';                               // edges step back so labels read clean
      this.lod(); this.render(); mark(b.dataset.v);
    });
    mark('net');                                                                 // force layout is already in place from run()
  }
  gridLayout() {
    const N = this.nodes;
    const widthOf = n => n.lab.scrollWidth > 0 ? n.lab.scrollWidth : n.name.length * 7.8 + 16;   // real width, text-length fallback
    const minCellW = Math.max(190, Math.max(...N.map(widthOf)) + 34), minCellH = 80;            // floored by label size -> no overlap
    const rank = {}; (this.C.order || []).forEach((t, i) => rank[t] = i);
    const order = [...N].sort((a, b) => a.hero !== b.hero ? (a.hero ? -1 : 1)            // hero first
      : (rank[a.type] ?? 99) - (rank[b.type] ?? 99)                                      // then group by type
      || b.deg - a.deg);                                                                 // then by prominence
    const padL = this.w < 760 ? 60 : 340, padR = 70, padY = 96, effW = Math.max(360, this.w - padL - padR), effH = Math.max(360, this.h - padY * 2);
    // columns: match the canvas aspect ratio (fills the screen), but never so many that a column drops below label width
    const cols = Math.max(1, Math.min(Math.floor(effW / minCellW), Math.round(Math.sqrt(N.length * (minCellH / minCellW) * (effW / effH)))));
    const rows = Math.ceil(N.length / cols);
    const cellW = Math.max(minCellW, effW / cols), cellH = Math.max(minCellH, effH / rows);      // grow cells to fill the canvas
    order.forEach((n, i) => {
      if (n._fbx === undefined) { n._fbx = n.bx; n._fby = n.by; n._famp = n.amp; n._flat = n.labelAt; }  // stash force state once
      const col = i % cols, row = Math.floor(i / cols);
      n.x = n.bx = (col - (cols - 1) / 2) * cellW;
      n.y = n.by = (row - (rows - 1) / 2) * cellH;
      n.amp = 0; n.labelAt = 0; n.lox = 0; n.loy = n.r + 9; n.lanchor = 'c';             // hold still, label below, always show
    });
    this.grid = { cols, rows, cellW, cellH };
    this.scale = 1;                                                                       // screen-space layout: cells already in screen px, labels unscaled
    this.tx = padL + effW / 2;                                                            // centred in the canvas area (right of the panel)
    this.ty = this.h / 2;
    this._syncZoom();
  }
  restoreForce() {
    this.nodes.forEach(n => { n.x = n.bx = n._fbx; n.y = n.by = n._fby; n.amp = n._famp; n.labelAt = n._flat; });
    this.nodes.forEach(n => {                                                            // re-fan label anchors outward
      const dist = Math.hypot(n.x, n.y);
      if (dist < 55) { n.lox = 0; n.loy = n.r + 9; n.lanchor = 'c'; }
      else { const cx = n.x / dist, cy = n.y / dist, off = n.r + 7; n.lox = cx * off; n.loy = cy * off; n.lanchor = cx > 0.4 ? 'l' : cx < -0.4 ? 'r' : 'c'; }
    });
  }

  // ---- panel directory: every page grouped (type / folder / flat A–Z), name-sorted ----
  bindNodeList() {
    this.groupBy = 'type';
    this.$('kg-groupby').addEventListener('click', e => {
      const b = e.target.closest('.kg-gb'); if (!b) return;
      this.groupBy = b.dataset.g; this.buildNodeList();
    });
    this.$('kg-nodelist').addEventListener('click', e => {
      const it = e.target.closest('.kg-item'); if (it) this.select(it.dataset.id);
    });
    this.buildNodeList();
  }
  buildNodeList() {
    [...this.$('kg-groupby').children].forEach(b => b.classList.toggle('on', b.dataset.g === this.groupBy));
    const folderOf = n => n.id.includes('/') ? n.id.split('/')[0] : '(root)';
    const byName = (a, b) => (a.name || a.label || '').localeCompare(b.name || b.label || '', undefined, { sensitivity: 'base' });
    const catColor = t => this.cats && this.cats[t] ? this.cats[t].color : (this.C.types[t] || this.C.types.unknown).color;
    const catLabel = t => this.cats && this.cats[t] ? this.cats[t].label : (this.C.types[t] || this.C.types.unknown).label;
    let groups;
    if (this.groupBy === 'az') {
      groups = [{ key: null, color: null, items: [...this.nodes].sort(byName) }];
    } else if (this.groupBy === 'folder') {
      const m = {};
      this.nodes.forEach(n => { const f = folderOf(n); (m[f] = m[f] || []).push(n); });
      groups = Object.keys(m).sort().map(f => ({ key: f, color: '#b3a98f', items: m[f].sort(byName) }));
    } else {
      const m = {};
      this.nodes.forEach(n => { (m[n.type] = m[n.type] || []).push(n); });
      const order = this.C.order || Object.keys(m);
      groups = Object.keys(m).sort((a, b) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99))
        .map(t => ({ key: catLabel(t), color: catColor(t), items: m[t].sort(byName) }));
    }
    let html = '';
    groups.forEach(g => {
      if (g.key !== null) html += `<div class="kg-grp">${g.color ? `<span class="gc" style="background:${g.color}"></span>` : ''}${this.esc(g.key)}<span class="gn">${g.items.length}</span></div>`;
      g.items.forEach(n => {
        html += `<div class="kg-item${this.selected === n.id ? ' sel' : ''}" data-id="${this.esc(n.id)}" title="${this.esc(n.label)}"><span class="ic" style="background:${n.color}"></span><span class="nm">${this.esc(n.name)}</span></div>`;
      });
    });
    this.$('kg-nodelist').innerHTML = html;
  }
  syncNodeListSel() {
    const list = this.$('kg-nodelist'); if (!list) return;
    list.querySelectorAll('.kg-item').forEach(it => it.classList.toggle('sel', it.dataset.id === this.selected));
    const cur = list.querySelector('.kg-item.sel');
    if (cur) cur.scrollIntoView({ block: 'nearest' });
  }

  // ---- drawer frontmatter: tidy "Details" view + literal "Raw" YAML, toggled ----
  renderMeta(n) {
    const m = n.meta || {}, d = s => (s || '').slice(0, 10);
    let clean = '';
    if (m.description) clean += `<div class="kg-desc">${this.esc(m.description)}</div>`;
    const bits = [];
    if (m.status) bits.push(`<span class="kg-pill status">${this.esc(m.status)}</span>`);
    if (m.updated) bits.push(`<span class="kg-pill">updated ${this.esc(d(m.updated))}</span>`);
    else if (m.created) bits.push(`<span class="kg-pill">created ${this.esc(d(m.created))}</span>`);
    if (m.version) bits.push(`<span class="kg-pill">v${this.esc(String(m.version))}</span>`);
    if (bits.length) clean += `<div class="kg-mrow">${bits.join('')}</div>`;
    const tags = Array.isArray(m.tags) ? m.tags : (m.tags ? [m.tags] : []);
    if (tags.length) clean += `<div class="kg-mrow">${tags.map(t => `<span class="kg-tag">${this.esc(t)}</span>`).join('')}</div>`;
    this.$('kg-d-meta-clean').innerHTML = clean || '<div class="kg-desc">No extra metadata.</div>';
    this.$('kg-d-meta-raw').textContent = '---\n' + (n.fmraw || '') + '\n---';
    this.applyMetaMode();
  }
  applyMetaMode() {
    const raw = this.metaMode === 'raw';
    this.$('kg-d-meta-clean').style.display = raw ? 'none' : '';
    this.$('kg-d-meta-raw').style.display = raw ? '' : 'none';
    [...this.$('kg-meta-tabs').children].forEach(b => b.classList.toggle('on', b.dataset.m === this.metaMode));
  }

  // ---- light / dark theme: toggle button, OS default, best-effort persistence ----
  setTheme(mode) {
    this.theme = mode;
    const rs = document.documentElement, dark = mode === 'dark';
    rs.dataset.theme = mode;
    rs.style.setProperty('--kg-bg', dark ? this.C.bgDark : this.C.bg);
    rs.style.setProperty('--kg-edge', dark ? this.C.edgeDark : this.C.edge);
    rs.style.setProperty('--kg-hi', dark ? this.C.hiDark : this.C.hi);
    const btn = this.$('kg-theme'); if (btn) btn.textContent = dark ? '☀️' : '🌙';
    try { localStorage.setItem('kg-theme', mode); } catch (e) {}
  }
  bindTheme() {
    this.$('kg-theme').addEventListener('click', () => this.setTheme(this.theme === 'dark' ? 'light' : 'dark'));
    let pref = null;
    try { pref = localStorage.getItem('kg-theme'); } catch (e) {}
    if (!pref) pref = (window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
    this.setTheme(pref);
  }

  // ---- zoom + name-density sliders, each with clickable +/- steppers ----
  bindControls() {
    const z = this.$('kg-zoom');
    this.density = 0; z.value = this.scale;
    z.addEventListener('input', () => this.setZoom(parseFloat(z.value)));
    this.$('kg-zoomctl').addEventListener('click', e => { const b = e.target.closest('.kg-step'); if (b) this.setZoom(this.scale * (b.dataset.z === '+' ? 1.12 : 0.893)); });
  }
  setZoom(s) {
    s = Math.max(0.4, Math.min(2.6, s));
    const cx = this.w / 2, cy = this.h / 2, wx = (cx - this.tx) / this.scale, wy = (cy - this.ty) / this.scale;
    this.scale = s; this.tx = cx - wx * s; this.ty = cy - wy * s; this.moved = true;
    this._syncZoom(); this.lod(); this.render();
  }
  _syncZoom() { const z = this.$('kg-zoom'); if (z) z.value = this.scale; }
}

document.addEventListener('DOMContentLoaded', () => new KG().run());
</script>
</body>
</html>
"""


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    target = Path(arg).expanduser() if arg else (Path(__file__).resolve().parents[1] / "knowledge")
    print(write_graph(target))
