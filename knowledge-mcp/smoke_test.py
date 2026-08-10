#!/usr/bin/env python3
"""Standalone MCP client that drives the git-backed knowledge-mcp server in both modes.

Self-locating. Read mode serves a frozen commit (v1). Manage runs on a THROWAWAY branch
(_smoketest, branched from v1), so the published arms v1/v2/v3 are never touched. The test
records the v1/v2/v3 commit ids before and after and asserts they are unchanged.
"""
import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent            # the knowledge-mcp/ folder
REG = str(HERE.parent / "knowledge")              # sibling knowledge repo (flat: it IS the bundle repo)
TOPIC = "knowledge"
REPO = Path(REG)
WORK = "_smoketest"        # throwaway branch the write/version tests use
TOPICS = Path("/tmp/kb-smoke-topics")             # throwaway topics-root for kb_new_topic
PY = sys.executable

READ_TOOLS = {"kb_index", "kb_list", "kb_get", "kb_grep", "kb_rules"}
MANAGE_TOOLS = READ_TOOLS | {"kb_add", "kb_update", "kb_remove", "kb_new_folder",
                             "kb_reindex", "kb_validate", "kb_versions",
                             "kb_snapshot", "kb_set_current", "kb_new_topic"}


def git(*a):
    return subprocess.run(["git", "-C", str(REPO), *a], capture_output=True, text=True)


def sha(ref):
    return git("rev-parse", ref).stdout.strip()


def params(mode, version=None, extra=()):
    args = [str(HERE / "server.py"), "--mode", mode, "--registry", REG,
            "--topic", TOPIC, "--log-dir", "/tmp/kb-smoke"]
    if version:
        args += ["--version", version]
    args += list(extra)
    return StdioServerParameters(command=PY, args=args)


async def text(session, name, args=None):
    r = await session.call_tool(name, args or {})
    return r.content[0].text


async def read_checks():
    async with stdio_client(params("read")) as (r, w):       # default branch = v1
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}
            assert tools == READ_TOOLS, f"read tools wrong: {sorted(tools)}"
            print(f"read mode: exactly {len(tools)} tools, no write tools  OK")

            got = await text(s, "kb_get", {"page_id": "concepts/TweedieDistribution.md"})
            truth = (REPO / "concepts/TweedieDistribution.md").read_text()
            assert got == truth, "byte-parity FAILED (git-served != working file)"
            print(f"kb_get byte-parity (served from commit): {len(got)} chars match  OK")

            idx = await text(s, "kb_index", {"folder": "concepts"})
            assert idx.lstrip().startswith("#"), "kb_index not a catalog"
            print(f"kb_index('concepts'): {idx.splitlines()[0]!r}  OK")

            grep = await text(s, "kb_grep", {"query": "tweedie"})
            assert "match(es)" in grep, "kb_grep no matches"
            print(f"kb_grep('tweedie'): {grep.splitlines()[0]}  OK")

            rules = await text(s, "kb_rules")
            assert "Knowledge Manager" in rules, "kb_rules wrong"
            print("kb_rules() returns the rulebook  OK")

            trav = await text(s, "kb_get", {"page_id": "../../../etc/passwd"})
            assert trav.startswith("ERROR"), "traversal guard FAILED"
            print("traversal guard blocks ../../../etc/passwd  OK")


CHARTER = ("Purpose: smoke-test wiki for the kb_new_topic tool. Materials: none, this is a "
           "test. Consumers: smoke_test.py only. Scope: prove scaffolding works; nothing else.")


async def manage_checks():
    async with stdio_client(params("manage", WORK,
                                   extra=["--topics-root", str(TOPICS)])) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}
            assert tools == MANAGE_TOOLS, f"manage tools wrong: {sorted(tools)}"
            print(f"manage mode: exactly {len(tools)} tools  OK")

            first = await text(s, "kb_validate")
            assert first.startswith("STOP: read the knowledge rules"), "rules gate did not fire"
            print("rules gate: first manage call returns the rulebook  OK")

            v = await text(s, "kb_validate")
            assert v.startswith("OKF v0.1: PASS"), f"validate not pass: {v[:80]}"
            print(f"kb_validate after gate: {v.splitlines()[0]}  OK")

            add = await text(s, "kb_add", {"page_id": "concepts/_SmokeTest.md",
                                           "type": "concept", "title": "Smoke Test",
                                           "description": "temporary test page."})
            assert add.startswith("added concepts/_SmokeTest.md"), add
            assert "validate: PASS" in add, add
            print("kb_add: created page in working tree, rebuilt index, validate PASS  OK")

            back = await text(s, "kb_get", {"page_id": "concepts/_SmokeTest.md"})
            assert "type: concept" in back and "Smoke Test" in back, back
            print("kb_get sees the uncommitted new page  OK")

            rm = await text(s, "kb_remove", {"page_id": "concepts/_SmokeTest.md"})
            assert rm.startswith("removed"), rm
            print("kb_remove: deleted page, rebuilt index  OK")

            snap = await text(s, "kb_snapshot", {"message": "smoke: round-trip"})
            assert snap.startswith("committed on _smoketest"), snap
            print(f"kb_snapshot: {snap.splitlines()[0]}  OK")

            vers = await text(s, "kb_versions")
            assert WORK in vers and "v1" in vers, vers
            print("kb_versions lists the arms incl the throwaway branch  OK")

            sc = await text(s, "kb_set_current", {"ref": "v1"})
            assert "_smoketest -> v1" in sc, sc
            await text(s, "kb_set_current", {"ref": WORK})  # switch back so cleanup is simple
            print("kb_set_current: switched arms and back  OK")

            # -- kb_new_topic: phase 1 (interview) creates nothing --------------------
            iv = await text(s, "kb_new_topic", {"name": "smoke-topic"})
            assert iv.startswith("STOP") and "charter" in iv, iv
            assert not (TOPICS / "smoke-topic").exists(), "interview phase must not create"
            print("kb_new_topic: no charter -> interview script, nothing created  OK")

            bad = await text(s, "kb_new_topic", {"name": "Bad Name!", "charter": CHARTER})
            assert bad.startswith("ERROR") and "slug" in bad, bad
            thin = await text(s, "kb_new_topic", {"name": "smoke-topic", "charter": "too short"})
            assert thin.startswith("ERROR") and "too thin" in thin, thin
            print("kb_new_topic: bad slug and thin charter both rejected  OK")

            # -- kb_new_topic: phase 2 (charter) scaffolds a servable repo ------------
            made = await text(s, "kb_new_topic", {"name": "smoke-topic", "charter": CHARTER,
                                                  "folders": "concepts,sources"})
            assert made.startswith("created topic 'smoke-topic'"), made
            nt = TOPICS / "smoke-topic"
            head = subprocess.run(["git", "-C", str(nt), "rev-parse", "--abbrev-ref", "HEAD"],
                                  capture_output=True, text=True).stdout.strip()
            assert head == "v1", f"new repo not on v1: {head!r}"
            for f in ("index.md", "log.md", "overview.md",
                      "concepts/index.md", "sources/index.md"):
                assert (nt / f).is_file(), f"missing {f}"
            assert CHARTER in (nt / "overview.md").read_text(), "charter not embedded"
            print("kb_new_topic: repo on v1, all seed files present, charter embedded  OK")

            dup = await text(s, "kb_new_topic", {"name": "smoke-topic", "charter": CHARTER})
            assert dup.startswith("ERROR") and "already exists" in dup, dup
            print("kb_new_topic: duplicate name rejected  OK")


async def new_topic_serve_checks():
    """The created topic must be servable by its own read server (flat-repo detection)."""
    sp = StdioServerParameters(command=PY, args=[str(HERE / "server.py"), "--mode", "read",
                                                 "--registry", str(TOPICS / "smoke-topic"),
                                                 "--log-dir", "/tmp/kb-smoke"])
    async with stdio_client(sp) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}
            assert tools == READ_TOOLS, f"new-topic read tools wrong: {sorted(tools)}"
            ov = await text(s, "kb_get", {"page_id": "overview.md"})
            assert CHARTER in ov, "served overview.md lacks the charter"
            idx = await text(s, "kb_index")
            assert "overview" in idx.lower(), f"root index not a catalog: {idx[:80]}"
            print("new topic served read-only by its own server; charter readable  OK")


def setup():
    git("checkout", "-q", "v1")
    git("branch", "-D", WORK)              # ignore error if absent
    git("branch", WORK, "v1")             # fresh throwaway branch from v1
    shutil.rmtree(TOPICS, ignore_errors=True)


def cleanup():
    git("checkout", "-q", "v1")
    git("branch", "-D", WORK)
    shutil.rmtree(TOPICS, ignore_errors=True)


async def main():
    setup()
    before = {b: sha(b) for b in ("v1", "v2", "v3")}
    await read_checks()
    print()
    await manage_checks()
    print()
    await new_topic_serve_checks()
    cleanup()
    after = {b: sha(b) for b in ("v1", "v2", "v3")}
    assert before == after, f"IMMUTABILITY FAILED: {before} -> {after}"
    print(f"\nimmutability: v1/v2/v3 commit ids unchanged by managing  OK\n  {after}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        cleanup()
    print("\nALL CHECKS PASSED")
