#!/usr/bin/env python3
"""Minimal, dependency-free, read-only MCP server for one plain folder."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


INSTRUCTIONS = """The `knowledge` server provides optional reference material.
You decide independently whether, when, and how to use it. Check anything you use
against the task, the available data, installed software, and observed results.

Available tools:
- kb_index: read a folder's table of contents
- kb_list: list available pages
- kb_get: read one page
- kb_grep: search the material
- kb_rules: return these usage notes
"""

SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}

TOOLS = [
    {
        "name": "kb_index",
        "description": "Return index.md for a folder. An empty folder means the top level.",
        "inputSchema": {
            "type": "object",
            "properties": {"folder": {"type": "string", "default": ""}},
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "kb_list",
        "description": "List every available page path, one per line.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "kb_get",
        "description": "Return one page by its path relative to the knowledge folder.",
        "inputSchema": {
            "type": "object",
            "properties": {"page_id": {"type": "string"}},
            "required": ["page_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "kb_grep",
        "description": "Search pages with a case-insensitive regex, using a literal fallback.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "kb_rules",
        "description": "Return the short usage notes for this optional reference material.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True},
    },
]


parser = argparse.ArgumentParser(description="Serve one plain knowledge folder over MCP.")
parser.add_argument("--root", required=True, help="plain folder to serve read-only")
parser.add_argument("--log-dir", required=True, help="directory for access logs")
parser.add_argument("--name", default="knowledge")
args = parser.parse_args()

root = Path(args.root).expanduser().resolve()
log_dir = Path(args.log_dir).expanduser().resolve()
if not root.is_dir():
    raise SystemExit("[knowledge] configured knowledge folder is unavailable")
if any(path.is_symlink() for path in root.rglob("*")):
    raise SystemExit("[knowledge] symbolic links are not permitted in the knowledge folder")

log_dir.mkdir(parents=True, exist_ok=True)
log_path = log_dir / (
    f"access_{os.getpid()}_"
    f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
)


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(tool: str, **fields: object) -> None:
    record = {"ts": now(), "tool": tool, **fields}
    try:
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def normalize(page_id: str, *, allow_empty: bool = False) -> str:
    rel = page_id.strip().lstrip("/")
    parts = [part for part in rel.split("/") if part not in ("", ".")]
    if (not parts and not allow_empty) or any(part == ".." for part in parts):
        raise ValueError("page_id must name a page inside the knowledge folder")
    return "/".join(parts)


def resolve_page(page_id: str) -> tuple[str, Path]:
    rel = normalize(page_id)
    path = (root / rel).resolve()
    if not path.is_relative_to(root):
        raise ValueError("page_id escapes the knowledge folder")
    return rel, path


def pages() -> list[str]:
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    )


def serve(tool: str, page_id: str, text: str) -> str:
    blob = text.encode("utf-8")
    log(tool, page_id=page_id, bytes=len(blob), sha256=hashlib.sha256(blob).hexdigest())
    return text


def kb_index(arguments: dict[str, Any]) -> str:
    folder = arguments.get("folder", "")
    if not isinstance(folder, str):
        raise ValueError("folder must be a string")
    rel = normalize(folder, allow_empty=True)
    page_id = f"{rel}/index.md" if rel else "index.md"
    _, path = resolve_page(page_id)
    if not path.is_file() or path.is_symlink():
        return f"(no index.md in '{rel or '.'}')"
    return serve("kb_index", page_id, path.read_text(encoding="utf-8", errors="replace"))


def kb_list(arguments: dict[str, Any]) -> str:
    if arguments:
        raise ValueError("kb_list takes no arguments")
    available = pages()
    log("kb_list", n_pages=len(available))
    return "\n".join(available)


def kb_get(arguments: dict[str, Any]) -> str:
    page_id = arguments.get("page_id")
    if not isinstance(page_id, str):
        raise ValueError("page_id is required and must be a string")
    rel, path = resolve_page(page_id)
    if not path.is_file() or path.is_symlink():
        log("kb_get", requested=page_id, error="not_found")
        return f"ERROR: no such page '{page_id}'. Call kb_index() or kb_list()."
    return serve("kb_get", rel, path.read_text(encoding="utf-8", errors="replace"))


def kb_grep(arguments: dict[str, Any]) -> str:
    query = arguments.get("query")
    max_results = arguments.get("max_results", 50)
    if not isinstance(query, str):
        raise ValueError("query is required and must be a string")
    if isinstance(max_results, bool) or not isinstance(max_results, int):
        raise ValueError("max_results must be an integer")
    limit = max(1, min(max_results, 200))
    try:
        regex = re.compile(query, re.IGNORECASE)
    except re.error:
        regex = re.compile(re.escape(query), re.IGNORECASE)
    hits: list[str] = []
    matched_pages: set[str] = set()
    for page_id in pages():
        try:
            text = (root / page_id).read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                hits.append(f"{page_id}:{line_number}: {line.strip()}")
                matched_pages.add(page_id)
                if len(hits) >= limit:
                    break
        if len(hits) >= limit:
            break
    log("kb_grep", query=query, n_matches=len(hits), n_pages=len(matched_pages))
    if not hits:
        return f"(no matches for {query!r})"
    return f"{len(hits)} match(es) across {len(matched_pages)} page(s):\n" + "\n".join(hits)


def kb_rules(arguments: dict[str, Any]) -> str:
    if arguments:
        raise ValueError("kb_rules takes no arguments")
    log("kb_rules", bytes=len(INSTRUCTIONS.encode("utf-8")))
    return INSTRUCTIONS


HANDLERS = {
    "kb_index": kb_index,
    "kb_list": kb_list,
    "kb_get": kb_get,
    "kb_grep": kb_grep,
    "kb_rules": kb_rules,
}

framing = "jsonl"


def send(payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if framing == "content-length":
        sys.stdout.buffer.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii"))
        sys.stdout.buffer.write(encoded)
    else:
        sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()


def result(request_id: Any, value: Any) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": value})


def error(request_id: Any, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def handle(message: dict[str, Any]) -> None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    log("protocol", method=method, has_id=request_id is not None)
    if request_id is None:
        return
    if method == "initialize":
        requested = params.get("protocolVersion")
        protocol = requested if requested in SUPPORTED_PROTOCOLS else "2025-11-25"
        result(
            request_id,
            {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": args.name, "version": "1.0.0"},
                "instructions": INSTRUCTIONS,
            },
        )
    elif method == "ping" or method == "logging/setLevel":
        result(request_id, {})
    elif method == "tools/list":
        result(request_id, {"tools": TOOLS})
    elif method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in HANDLERS:
            error(request_id, -32602, f"unknown tool: {name}")
            return
        if not isinstance(arguments, dict):
            error(request_id, -32602, "tool arguments must be an object")
            return
        try:
            text = HANDLERS[name](arguments)
            result(request_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except (OSError, ValueError) as exc:
            result(
                request_id,
                {"content": [{"type": "text", "text": f"ERROR: {exc}"}], "isError": True},
            )
    else:
        error(request_id, -32601, f"method not found: {method}")


def extract_message(buffer: bytes) -> tuple[dict[str, Any] | None, bytes]:
    """Parse JSON-lines, undelimited JSON, or legacy Content-Length framing."""
    global framing
    stripped = buffer.lstrip()
    leading = len(buffer) - len(stripped)
    if not stripped:
        return None, b""

    if stripped.lower().startswith(b"content-length:"):
        framing = "content-length"
        header_end = stripped.find(b"\r\n\r\n")
        separator = 4
        if header_end < 0:
            header_end = stripped.find(b"\n\n")
            separator = 2
        if header_end < 0:
            return None, buffer
        headers = stripped[:header_end].decode("ascii", errors="replace").splitlines()
        length = None
        for header in headers:
            key, _, value = header.partition(":")
            if key.strip().lower() == "content-length":
                length = int(value.strip())
                break
        if length is None:
            raise ValueError("Content-Length header is missing")
        body_start = leading + header_end + separator
        body_end = body_start + length
        if len(buffer) < body_end:
            return None, buffer
        message = json.loads(buffer[body_start:body_end])
        if not isinstance(message, dict):
            raise ValueError("request must be an object")
        return message, buffer[body_end:]

    framing = "jsonl"
    try:
        text = stripped.decode("utf-8")
    except UnicodeDecodeError:
        return None, buffer
    try:
        message, end = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError:
        return None, buffer
    if not isinstance(message, dict):
        raise ValueError("request must be an object")
    consumed = leading + len(text[:end].encode("utf-8"))
    return message, buffer[consumed:]


def main() -> None:
    buffer = b""
    while True:
        while buffer:
            try:
                message, remainder = extract_message(buffer)
            except Exception as exc:
                log("protocol_error", error=type(exc).__name__, detail=str(exc))
                error(None, -32700, f"parse error: {exc}")
                buffer = b""
                break
            if message is None:
                break
            buffer = remainder
            try:
                handle(message)
            except Exception as exc:
                log("protocol_error", error=type(exc).__name__, detail=str(exc))
                error(message.get("id"), -32603, f"internal error: {exc}")

        chunk = os.read(sys.stdin.fileno(), 65536)
        if not chunk:
            if buffer.strip():
                log("protocol_error", error="incomplete_request", raw_prefix=repr(buffer[:500]))
            return
        buffer += chunk


if __name__ == "__main__":
    main()
