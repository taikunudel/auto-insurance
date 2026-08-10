# knowledge-mcp — user manual

> **Audience: you, the HUMAN** maintainer/operator. The agent never reads this file; it
> gets its tool descriptions from the server itself. For the rules the agent must follow,
> see `AGENT_RULES.md`.

One MCP server serves a knowledge base (an OKF wiki) to agents and lets a manager edit it.
It runs in two modes from the same `server.py`.

## Launch

Read mode (look only, 5 tools). Pin to one version (`--version v1`), or let it default to the repo's current branch:
```
python server.py --mode read   --registry ~/knowledge-mcp-design/knowledge \
                 --topic auto-insurance-pure-premium-modeling [--version v1]
```

Manage mode (look + edit + version, 15 tools). Start it deliberately:
```
python server.py --mode manage --registry ~/knowledge-mcp-design/knowledge \
                 --topic auto-insurance-pure-premium-modeling
```

In read mode the 10 manage tools are not registered at all, so a read agent cannot see or
call them. In read mode the server is pinned to one version and cannot reach the others.

## The rules gate (manage mode)

The first manage call in a session does not run. It returns the rulebook
(`AGENT_RULES.md`) and asks the agent to call the tool again, so the rules are read
before any edit happens. Every manage result also ends with a short rule reminder. An
agent can also read the rules at any time with `kb_rules()`.

---

## The 15 tools

### 1. kb_index  (read)
Return the table of contents (`index.md`) for a folder. Empty argument = the top level.
```
IN:  kb_index("concepts")
OUT:
# Concepts

* [Adverse Selection](AdverseSelection.md) - Asymmetric-information spiral where low-risk insureds leave.
* [Gini Index](GiniIndex.md) - Ordered Lorenz/Gini score measuring how well a model ranks risk.
* [Tweedie Distribution](TweedieDistribution.md) - Compound Poisson-Gamma family for pure-premium data.
* [Tweedie Variance Power Estimation](TweedieVariancePowerEstimation.md) - Profiling p before fitting.
```

### 2. kb_list  (read)
List the path (page_id) of every page, one per line.
```
IN:  kb_list()
OUT:
index.md
overview.md
gaps.md
concepts/index.md
concepts/TweedieDistribution.md
entities/index.md
sources/qian-2016-hdtweedie.md
examples/qian-2016-hdtweedie.R
```

### 3. kb_get  (read)
Return the full raw markdown of one page (frontmatter + body).
```
IN:  kb_get("concepts/TweedieDistribution.md")
OUT:
---
type: concept
title: "Tweedie Distribution"
tags: [method, distribution]
timestamp: 2026-05-31T00:00:00Z
---

# Tweedie Distribution

## Definition
A member of the exponential dispersion family with variance V(mu) = phi * mu^p. For
1 < p < 2 it is a compound Poisson-Gamma law with a point mass at zero, which fits
insurance pure premium (many zero-claim policies, positive losses otherwise).
```

### 4. kb_grep  (read)
Search every page for a term (case-insensitive regex, literal fallback).
```
IN:  kb_grep("dispersion")
OUT:
3 match(es) across 2 page(s):
concepts/TweedieDistribution.md:12: the dispersion phi scales the variance V(mu)=phi*mu^p
concepts/DoubleGeneralizedLinearModels.md:8: a second GLM models the dispersion phi by group
sources/smyth-jorgensen-2002-tweedie-dispersion.md:5: REML estimation of the dispersion submodel
```

### 5. kb_rules  (read)
Return the operating rulebook (`AGENT_RULES.md`).
```
IN:  kb_rules()
OUT:
# Knowledge Manager — Operating Rules (v0.1)

## 1. The three principles (inviolable)
1. Minimally opinionated. The only hard requirement on a document is a non-empty type.
2. Producer and consumer are independent. The folder is the contract.
3. A format, not a platform. Plain markdown and YAML on disk only.
...
```

### 6. kb_add  (manage)
Create a new page. Requires `type`. Writes frontmatter, saves the file, rebuilds the
parent `index.md`, logs, validates.
```
IN:  kb_add(page_id="concepts/Overdispersion.md", type="concept",
            title="Overdispersion", description="Variance beyond the model's assumption.")
OUT:
added concepts/Overdispersion.md
  frontmatter: type=concept, title="Overdispersion", status=draft, version=1, created=2026-06-21T14:30:00Z
  parent index rebuilt: concepts/index.md
  log appended
  validate: PASS (41 docs, 5 folders)

[knowledge rules] every page needs a non-empty `type`; index.md is rebuilt for you; ...
```

### 7. kb_update  (manage)
Edit a page's body, bump its version and `updated` date, and log the change.
```
IN:  kb_update(page_id="concepts/Overdispersion.md", note="added a worked example")
OUT:
updated concepts/Overdispersion.md
  version -> 2
  updated: 2026-06-21T14:32:00Z
  log appended

[knowledge rules] ...
```

### 8. kb_remove  (manage)
Delete a page and rebuild the parent folder's `index.md`.
```
IN:  kb_remove("concepts/Overdispersion.md")
OUT:
removed concepts/Overdispersion.md
  parent index rebuilt: concepts/index.md
  log appended

[knowledge rules] ...
```

### 9. kb_new_folder  (manage)
Create a new sub-topic folder and give it the required `index.md` automatically.
```
IN:  kb_new_folder(path="procedures", description="Step-by-step modeling recipes.")
OUT:
created procedures/
  wrote procedures/index.md
  parent index updated

[knowledge rules] ...
```

### 10. kb_reindex  (manage)
Rebuild a folder's `index.md` from its actual on-disk children. Empty argument = root.
```
IN:  kb_reindex("concepts")
OUT:
rebuilt concepts/index.md (15 entries)

[knowledge rules] ...
```

### 11. kb_validate  (manage)
Check the whole base against the rules and report anything broken.
```
IN:  kb_validate()
OUT (healthy):
OKF v0.1: PASS
  folders checked: 5
  every folder has index.md: yes
  knowledge documents: 40
  every document has a non-empty type: yes
  reserved files carry no frontmatter: yes

OUT (broken):
OKF v0.1: FAIL (2 issue(s))
  - entities: missing index.md
  - sources/Qian2016.md: missing `type`
```

### 12. kb_versions  (manage)
List the arms (git branches) and the recent timeline, marking the one checked out.
```
IN:  kb_versions()
OUT:
topic: auto-insurance-pure-premium-modeling  (git repo)
arms (branches):
  v1  62c4c30  v1 arm snapshot   <- current
  v2  4ee3a4e  v2 arm snapshot
  v3  7e07ce3  v3 arm snapshot
recent commits on v1:
  62c4c30 v1 arm snapshot
```

### 13. kb_snapshot  (manage)
Commit the working-tree changes on the current arm as a new immutable point. Earlier commits
never change. Pass a message describing the change.
```
IN:  kb_snapshot("add elastic-net example page")
OUT:
committed on v1: 9a1b2c3  add elastic-net example page
  a new immutable point; earlier commits are unchanged
  read servers pin a commit at startup, so running runs are unaffected

[knowledge rules] ...
```

### 14. kb_set_current  (manage)
Switch the working tree to another arm (git branch). Read servers started afterward default to
it; already-running readers keep their pinned commit.
```
IN:  kb_set_current("v2")
OUT:
current arm: v1 -> v2 (checked out)
  read servers started now default to v2
  already-running read servers keep their pinned commit

[knowledge rules] ...
```

### 15. kb_new_topic  (manage)
Create a NEW topic: a separate git repo beside the current one (or under `--topics-root`).
Two-phase. Called **without a charter** it creates nothing and returns the interview the
agent must run with you (materials available, purpose & consumers, scope, folder plan):
```
IN:  kb_new_topic(name="ios-app-design")
OUT:
STOP — nothing was created. A new topic needs a charter agreed with the USER first.
Interview the user now; do NOT invent the answers yourself:
  1. materials — what does the user have available (papers, repos, datasets, notes,
     run logs, URLs)?
  2. purpose & consumers — which agents/tasks will read this wiki, and to do what?
  3. scope — what is in, what is out?
  4. structure — folder plan (default: concepts, entities, sources, examples;
     override with `folders`).
...
```
Called **with the agreed charter** it scaffolds the repo (branch `v1`, initial commit), embeds
the charter in `overview.md`, and returns a ready-to-paste MCP config for the new topic:
```
IN:  kb_new_topic(name="ios-app-design",
                  charter="Purpose: HIG-grounded design wiki for the iOS agent. Materials: ...",
                  folders="concepts,sources,examples")
OUT:
created topic 'ios-app-design' at /home/you/knowledge-mcp-design/ios-app-design
  git repo, branch v1, commit 3f9c21a
  seeded: overview.md (charter embedded), log.md, index.md, folders: concepts, sources, examples
  NOTE: this server stays bound to topic 'knowledge'; the new topic needs its own server:
    manage: $HOME/knowledge-mcp-design/knowledge-mcp/start.sh --mode manage --registry $HOME/knowledge-mcp-design/ios-app-design
  read-mode MCP config snippet:
{ "mcpServers": { "knowledge-ios-app-design": { ... } } }

[knowledge rules] ...
```
Guards: slug-only names, refuses if the folder exists, refuses a charter under 80 chars
(a real charter records the user's answers). The current server never switches to the new
topic — launch a separate server for it.
