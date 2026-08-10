# knowledge-mcp

The server. One program (`server.py`) that serves a knowledge base to agents and, in manage mode, lets a manager edit it. This folder holds **code only**; the data lives next door in `knowledge/`.

Position in the project: the bridge between the knowledge (`knowledge/`) and the agents (in `bench/`). An agent never touches the files directly; it calls the server's tools.

## Two modes (one program, one flag)
- **read** 5 tools (`kb_index`, `kb_list`, `kb_get`, `kb_grep`, `kb_rules`), pinned to one version, read-only. This is what a benchmark run uses.
- **manage** all 15 tools (adds create / edit / delete / validate, version control, and `kb_new_topic` — new-topic creation gated on a charter agreed with the user). Started on purpose, to maintain the base.

## Files
- `server.py` the server (both modes, 15 tools, rules gate, traversal guard, access log)
- `start.sh` self-locating launcher: `./start.sh --mode read`
- `mcp.read.json` / `mcp.manage.json` configs telling a client how to start it
- `AGENT_RULES.md` the rulebook the server hands every agent (also via `kb_rules()`)
- `build_okf.py` helpers that build and validate OKF bundles (used by the write tools)
- `smoke_test.py` the test that checks both modes
- `requirements.txt` the single dependency (`mcp`)
- `logs/` one access-log file per session
- `.venv/` the Python environment

## Run it
```bash
./start.sh --mode read              # serves the current version of the only topic
./.venv/bin/python smoke_test.py    # all checks pass
```
