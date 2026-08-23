# slurm-mcp

**A read-only MCP server exposing Slurm scheduler state to agents.** The
allowlist is enforced in code, not requested in a prompt, and the tool surface
uses progressive disclosure so a Slurm-shaped question does not cost a
Slurm-shaped context window.

> **v0.1.0.** The guard, the topic surface, and the stdio server are built and
> tested. Runs against a real cluster or against recorded fixtures with no
> Slurm installed.

---

## Why the guard is in code

A system prompt saying *"only use read-only commands"* is a request, not a
control. It fails open. A jailbreak, a confused tool call, or an ordinary
hallucination is enough to reach `scontrol update` on a production controller.

Anything that could drain a node must be impossible to **express**, not merely
discouraged. So the allowlist lives in [`guard.py`](src/slurm_mcp/guard.py) and
runs on every invocation:

- only eight read binaries may run at all;
- `scontrol` is permitted for `show` and refused for `update`, `reconfigure`,
  `shutdown`, `reboot`, `requeue`, `hold`, `power`, and fifteen more;
- shell metacharacters in arguments are refused, and commands execute with
  `shell=False` anyway — defence in depth, not the only barrier.

**The threat model is not a malicious user.** It is an agent that has read a
confusing log line at 3am and is about to do something decisive.

The tests drive this with 51 real mutating and injection attempts rather than
asserting on prompt text, because a prompt-level promise cannot be tested:

```bash
make test
```

## Progressive disclosure

The obvious design exposes one tool per binary — `sinfo`, `squeue`, `sacct`,
`sdiag`, `sprio`, `sshare`, `scontrol`, `sacctmgr` — each carrying a schema for
its flags. Slurm's flag surface is enormous and most of it is irrelevant to any
given question, but all of it sits in context on every turn.

This server exposes **three** tools:

| tool | when |
|---|---|
| `slurm_overview` | the snapshot most sessions open with — nodes, queue, diagnostics in one call |
| `slurm_query` | one topic plus optional filters. Topics are a closed vocabulary, not a command line |
| `slurm_describe` | column meanings and filters for **one** topic, fetched only when needed |

Measured, and reproducible with `make footprint`:

```
  resident, three tools            1088 chars
  detail, fetched on request       3029 chars
  flat one-tool-per-binary         4441 chars  (4.1x resident)
```

That is a context-cost measurement, not a quality claim. It says the detail is
not resident until asked for; it does not say the agent will diagnose anything
better. Nothing here has been scored on
[slurm-rca-bench](https://github.com/Zhanyl-tech/slurm-rca-bench).

**Topics:** `queue`, `nodes`, `accounting`, `priority`, `fairshare`,
`diagnostics`, `config`.

## Quickstart

No cluster required.

```bash
make install
make surface      # the read-only allowlist, and what is denied
make demo         # a full overview against recorded fixtures
make footprint    # reproduce the context measurement above
make check        # ruff, ruff format, mypy --strict, pytest
```

Against a real cluster, drop `--fixtures`:

```bash
slurm-mcp overview
slurm-mcp query queue --filter user=alice
slurm-mcp describe accounting
```

### As an MCP server

```bash
pip install -e ".[server]"
slurm-mcp serve              # stdio; add --fixtures to serve recorded data
```

Point any MCP client at that command. Every response is labelled `[live]` or
`[fixture]` so recorded data can never be mistaken for a cluster read.

## Limitations

- **Read-only is enforced for this server's own surface.** It does not sandbox
  the host, and it says nothing about what other tools an agent has been given.
- **The fixtures are illustrative, not a benchmark.** They are a plausible
  cluster shape used to make the surface runnable; no result should be quoted
  from them.
- **No scoring.** The claim here is about the tool surface, not diagnostic
  accuracy. Measuring whether progressive disclosure changes an agent's
  diagnosis is `cluster-sre-agent`'s job, and it has not been done.
- **Single Slurm dialect.** Output formats are tested against Slurm 25.x column
  layouts. Older versions may differ.
- **`sacct` can block.** A degraded accounting path makes it hang rather than
  error; calls time out at 20s and say so, because an agent waiting forever is
  worse than an agent told the read failed.

## Related

- [cluster-sre-agent](https://github.com/Zhanyl-tech/cluster-sre-agent) — the
  agent that consumes a surface like this one. Its internal read-only tool
  layer is where this design came from; this repo is the standalone server.
- [cluster-ops-skills](https://github.com/Zhanyl-tech/cluster-ops-skills) —
  the runbooks an agent follows once it can read the cluster.
- [slurm-rca-bench](https://github.com/Zhanyl-tech/slurm-rca-bench) — where a
  claim about diagnostic quality would have to be proven.

## License

MIT.
