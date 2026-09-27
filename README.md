# slurm-mcp

**A read-only MCP server exposing Slurm scheduler state to agents.** The
allowlist is enforced in code, not requested in a prompt, and the tool surface
uses progressive disclosure so the flag surface of seven Slurm binaries is not
resident in the agent's context on every turn.

> **v0.2.0 (unreleased).** The guard, the topic surface and the stdio server
> are built. The tests drive the guard adversarially, run the live path against
> fake Slurm binaries, and exercise the MCP transport in-process and over real
> stdio against fixtures. **It has not been run against a real Slurm cluster**,
> and the fixtures are hand-written, not recorded. See
> [CHANGELOG.md](CHANGELOG.md) for what changed since v0.1.0 and why.

---

## Why the guard is in code

A system prompt saying *"only use read-only commands"* is a request, not a
control. It fails open. A jailbreak, a confused tool call, or an ordinary
hallucination is enough to reach `scontrol update` on a production controller.

Anything that could drain a node must be impossible to **express**, not merely
discouraged. So the allowlist lives in [`guard.py`](src/slurm_mcp/guard.py) and
runs on every invocation. It is an allowlist of *shapes*, in two layers that
must both pass:

- **Binary shape.** Only seven read binaries may run, and each may carry only
  the exact option spellings listed for it. No abbreviations, no bundled short
  options, no option in front of a subcommand. `scontrol` may run only as
  `scontrol show <config|job|node|partition> [name]`. `sdiag` takes no options,
  because `sdiag --reset` zeroes the counters a diagnosis reads.
- **Topic shape.** The argv must be exactly one topic's command followed by
  that topic's own filter flags. Every filter value must be a plain name, list,
  state or time: it cannot start with `-` and cannot contain whitespace, a shell
  metacharacter or a control character.
- Commands execute with `shell=False` anyway. That is defence in depth, not the
  only barrier.

v0.1.0 did this with a list of forbidden subcommands, and an audit found 23
argv that passed it and should not have. Twenty-two come from Slurm's own
behaviour, and each writes, mints a credential, resets counters or opens an
interactive prompt. Slurm accepts unique abbreviations (`scontrol upd` is
`update`). An option with a value can hide the verb (`scontrol -u 0 shutdown`).
Verbs were missing from the list (`uhold`, `token`, `sacctmgr shutdown`). A bare
`scontrol` reads commands from stdin. The 23rd was v0.1.0's own: a filter value
starting with `--format=` skipped the metacharacter check. All 23 are in
[`tests/test_guard.py`](tests/test_guard.py), and each is refused by *either*
layer on its own. Each of the 22 upstream cases cites the man page or source
line it relies on.

**The threat model is not a malicious user.** It is an agent that has read a
confusing log line at 3am and is about to do something decisive.

The tests drive the guard with 70 adversarial argv (a test checks that this
number matches the corpus) rather than asserting on prompt text, because a
prompt-level promise cannot be tested:

```bash
make test
```

## Deployment: Slurm's own authorization is the backstop

The guard decides what this server will *ask* Slurm to do. Slurm decides what
the asking account is *allowed* to do, and that is the stronger control. Run
the server as a dedicated, unprivileged Slurm user: no `AdminLevel`, not an
operator or coordinator. Then a guard bug that let a write through would still
be refused by slurmctld. (`sdiag --reset`, for example, is "Only supported for
Slurm operators and administrators", per sdiag(1).)

Two consequences of that choice:

- `accounting` runs `sacct -a` and `fairshare` runs `sshare -a`. Per sacct(1),
  `-a` shows all users' jobs only "when run by user root or if PrivateData is
  not configured to jobs"; otherwise it shows the server account's own jobs.
  Depending on your `PrivateData`, the agent sees either everyone's jobs or
  almost nothing. Decide which you want before deploying.
- While serving, every live read is logged to stderr as one bare JSON object
  per line, with `argv`, `exit_code`, `seconds` and `cached`. Reads answered
  from the cache are logged too, with `"cached": true`, although no Slurm
  command ran for them. Calls refused by the guard or the gate run nothing and
  are not logged. stdio servers may write to stderr freely, and an SRE-facing
  server should leave its own audit trail rather than rely on the client's.

## Load on slurmctld

A read-only server can still hurt a struggling controller. The squeue man
page's PERFORMANCE section says: "Do not run squeue or other Slurm client
commands that send remote procedure calls to slurmctld from loops in shell
scripts or other programs." An agent retrying `slurm_overview` is exactly that
loop. The MCP spec also says servers MUST "Rate limit tool invocations"
([tools, 2026-07-28](https://modelcontextprotocol.io/specification/latest/server/tools)).

So every live read goes through a gate:

| limit | default | flag |
|---|---|---|
| reuse an answer for the identical command | 10 s | `--cache-ttl` (0 disables) |
| Slurm commands running at once | 2 | `--max-concurrent` |
| Slurm commands started per 60 s | 30 | `--max-calls-per-minute` (0 disables) |
| rows returned for a tabular topic | 200 | `--max-rows` (at least 1; no "off") |

Concurrent identical calls share one subprocess while the cache is on
(`--cache-ttl` above 0). With it off they take turns, and each runs its own. A
cached answer is labelled `[live, cached 4s ago]` and does not spend the rate
budget. A refused call says why: the rate-limit refusal says when to retry,
and a busy refusal says every slot stayed full for 20 s. Output past the row
cap ends with
`[truncated: 200 of N rows shown; narrow with filters: …]`, so a Slurm-sized
answer does not become a Slurm-sized context window. Slurm calls run in a worker
thread, so the server keeps answering other requests while slurmctld is slow.

The defaults are judgement calls, not tuned against a real controller. They
apply **per server process**: ten clients each launching their own stdio server
get ten times the budget.

## Progressive disclosure

The obvious design exposes one tool per binary — `sinfo`, `squeue`, `sacct`,
`sdiag`, `sprio`, `sshare`, `scontrol` — each carrying a schema for its flags.
Slurm's flag surface is enormous and most of it is irrelevant to any given
question, but all of it sits in context on every turn.

This server exposes **three** tools, all annotated `readOnlyHint: true`:

| tool | when |
|---|---|
| `slurm_overview` | the snapshot most sessions open with — nodes, queue, diagnostics in one call |
| `slurm_query` | one topic plus optional filters. Topics are a closed vocabulary, not a command line |
| `slurm_describe` | column meanings and filters for **one** topic, fetched only when needed |

Measured with `make footprint` (output of `slurm-mcp footprint` at v0.2.0; a
test fails if these numbers drift from the code):

```
  resident: three tool descriptors    1486 chars
  resident: server instructions        369 chars
  resident total                      1855 chars
  detail, fetched on request          4229 chars
  flat, 7 schemas only                2009 chars  (1.08x resident)
  flat, schemas + inlined detail      6238 chars  (3.36x resident)
```

Read this carefully, because the headline depends on an assumption. A flat
server with one generic tool per binary and **no** guidance is about the same
size as this server's resident surface (1.08x). The 3.36x only holds if a flat
server would carry guidance comparable to `slurm_describe` in its tool
descriptions. That assumption is not measured against any real server. The
resident side counts the server instructions, sent at initialize; the flat side
is given none, which favours the flat design. What the measurement does show
is where the 4229 characters of detail live: here, off the resident path until
asked for.

That is a context-cost measurement, not a quality claim. It does not say the
agent will diagnose anything better. Nothing here has been scored on
[slurm-rca-bench](https://github.com/Zhanyl-tech/slurm-rca-bench).

v0.1.0 reported 1088 / 3029 / 4441 chars (4.1x). Those numbers reproduced, but
the ratio came mostly from the unstated inlined-detail assumption, the flat
baseline included `sacctmgr` (which no topic used), and the instructions were
not counted.

**Topics:** `queue`, `nodes`, `accounting`, `priority`, `fairshare`,
`diagnostics`, `config`.

## Quickstart

No cluster required. `make install` uses [uv](https://docs.astral.sh/uv/) when
it is on PATH, and otherwise needs `PY` to be Python 3.11 or newer.

```bash
make install
make surface      # the read-only allowlist, and what is refused
make demo         # a full overview against the hand-written fixtures
make footprint    # reproduce the context measurement above
make check        # ruff, ruff format, mypy --strict, pytest
make smoke        # the MCP server over real stdio, against fixtures
```

Against a real cluster, drop `--fixtures`:

```bash
slurm-mcp overview
slurm-mcp query queue --filter user=alice
slurm-mcp describe accounting
```

`overview` and `query` exit non-zero when any read failed, so a script can tell
"the read failed" from "nothing is queued".

### As an MCP server

From a checkout:

```bash
pip install -e ".[server]"      # or: uv pip install -e ".[server]"
slurm-mcp --fixtures serve      # stdio; drop --fixtures to read the cluster
```

Or straight from git. This is the standard pip form, but this exact command
has not been run while the branch is unpublished. The equivalent non-editable
install of a local checkout has been, and the CI workflow repeats it from a
built wheel.

```bash
pip install "slurm-readonly-mcp[server] @ git+https://github.com/Zhanyl-tech/slurm-mcp"
```

The distribution is `slurm-readonly-mcp` because `slurm-mcp` on PyPI is an
unrelated project; `pip install slurm-mcp` installs someone else's code. The
command is still `slurm-mcp`. The server needs `mcp>=2.0,<3`: it is written
against the mcp 2.x API, and every mcp 1.x release fails at start-up.

Then point an MCP client at the command. Clients need the absolute path. For
Claude Code, in a project `.mcp.json`:

```json
{
  "mcpServers": {
    "slurm": {
      "command": "/absolute/path/to/slurm-mcp/.venv/bin/slurm-mcp",
      "args": ["--fixtures", "serve"]
    }
  }
}
```

or `claude mcp add --transport stdio slurm -- /absolute/path/to/.venv/bin/slurm-mcp --fixtures serve`
([format](https://code.claude.com/docs/en/mcp)). Every response is labelled
`[live]` or `[fixture]` so sample data can never be mistaken for a cluster
read. In fixture mode the `user`, `partition` and `account` filters are applied
by the server to the sample rows, and the response says so, truncated or not.
Other filters are refused rather than imitated, and a fixture-mode truncation
hint does not suggest them.

## Why the CLI, and when slurmrestd would be better

This server runs the Slurm client commands. That needs the client binaries, a
`slurm.conf` and working Slurm authentication (typically munge) on the host,
which suits a login node or an admin box. It also means parsing text with the
column legends in [`topics.py`](src/slurm_mcp/topics.py).

Slurm also "provides a REST API through the slurmrestd daemon, using JSON Web
Tokens for authentication" ([rest.html](https://slurm.schedmd.com/rest.html),
Slurm 26.05). Slinky's slurm-operator "obtains a JWT so it can talk to each
Slurm cluster it manages via slurmrestd"
([architecture](https://github.com/SlinkyProject/slurm-operator/blob/main/docs/concepts/architecture.md)).
For a server running as a pod next to a Slinky-managed cluster, a slurmrestd
backend would avoid client binaries and munge in the pod and would return typed
JSON. Its read-only property would then rest on two things: a closed list of
GET requests, and a JWT for a Slurm user with no admin or operator rights, so
slurmctld refuses writes even if the server had a bug. It is also why the guard
refuses `scontrol token`: minting a JWT is not a read.

**That backend does not exist here.** Neither does `--json` parsing into MCP
`structuredContent`, although several of these commands offer `--json`. Both
are deferred; the tool surface would stay the same three tools.

## Prior art

Other public Slurm MCP servers exist. The ones checked (their READMEs, read
2026-09-26) expose write paths:

- [dongwookim-ml/slurm-mcp](https://github.com/dongwookim-ml/slurm-mcp) —
  `submit_job`, `cancel_job`, plus file management and shell access.
- [yidong72/slurm_mcp](https://github.com/yidong72/slurm_mcp) — 34 tools over
  SSH, including `submit_job`, `cancel_job`, `hold_job`.
- [sahuno/slurm-mcp](https://github.com/sahuno/slurm-mcp) — submit, monitor and
  cancel jobs, including `--wrap` commands.
- [tejasvinu/mcp-slurm](https://github.com/tejasvinu/mcp-slurm) — submit, and
  cancel / hold / release / suspend / resume / requeue / modify jobs.

This one differs in scope, not quality. It is read-only by construction, with
the allowlist enforced in code and tested adversarially; the agent picks from a
closed vocabulary of topics rather than writing a command line; and detail is
disclosed progressively.

## Limitations

- **Read-only is enforced for this server's own surface.** It does not sandbox
  the host, and it says nothing about what other tools an agent has been given.
  Slurm authorization is the backstop (see Deployment).
- **The fixtures are hand-written and illustrative, not recordings.** They are
  shaped like each command's output so the surface is runnable; no result
  should be quoted from them. See [`fixtures/README.md`](src/slurm_mcp/fixtures/README.md).
- **Column layouts follow the Slurm 26.05 man pages (checked 2026-09-26), and
  are unverified against a live cluster.** Older or newer Slurm versions may
  print differently. Verifying this needs a real or containerised Slurm, and
  fixtures recorded from it with the version and command in a header.
- **No scoring.** The claim here is about the tool surface, not diagnostic
  accuracy. Measuring whether progressive disclosure changes an agent's
  diagnosis is `cluster-sre-agent`'s job, and it has not been done.
- **Limits are per process, and output is capped after capture.** The row cap
  bounds what reaches the agent, not what the subprocess writes into this
  server's memory first.
- **`sacct` can block.** A degraded accounting path makes it hang rather than
  error; calls time out at 20s and say so, because an agent waiting forever is
  worse than an agent told the read failed. (In slurm-rca-bench scenario S01 —
  one run, Slurm 25.11.4 in Docker Compose — sacct blocked with no error while
  scheduling continued.)

## Related

- [cluster-sre-agent](https://github.com/Zhanyl-tech/cluster-sre-agent) — the
  agent that consumes a surface like this one. Its internal read-only tool
  layer is where this design came from; this repo is the standalone server.
  It keeps its own read-only guard. The two are separate code, not a shared
  module, so a fix to one does not reach the other.
- [cluster-ops-skills](https://github.com/Zhanyl-tech/cluster-ops-skills) —
  the runbooks an agent follows once it can read the cluster.
- [slurm-rca-bench](https://github.com/Zhanyl-tech/slurm-rca-bench) — where a
  claim about diagnostic quality would have to be proven.

## License

MIT.
