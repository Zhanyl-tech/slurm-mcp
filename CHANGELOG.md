# Changelog

## 0.2.0 — unreleased

Applies an adversarially verified audit of v0.1.0. The headline: the guard was
a denylist, and 23 argv that should have been refused passed it. 22 of them
write, mint a credential, reset counters or open an interactive prompt. None
of those 22 was reachable through the three MCP tools, because tool argv came
from the fixed topic table and a caller's value only ever filled a filter
flag's required argument. The 23rd was reachable: caller values starting with
`--format=` skipped the metacharacter check, and `slurm_query` delivered one to
squeue, sprio or sacct. It was harmless, because `-u` takes a required argument
upstream and commands ran with `shell=False`. Still, the layer the README
called "the control" did not control any of the 23. Nothing below was run
against a real Slurm cluster. Live
paths were tested with fake binaries on `PATH`, and the MCP transport was tested
in-process and over real stdio against fixtures.

### Security

- **The guard is now an allowlist of shapes** (`guard.py`), replacing the
  subcommand denylist. There are two layers, and both must pass:
  - Each binary may carry only exact, listed option spellings. `scontrol` may
    run only as `scontrol show <config|job|node|partition> [name]`, and `sdiag`
    takes no options.
  - The argv must be exactly a topic's command plus that topic's filter pairs.
  - All 23 audit bypasses are regression tests. The 22 from Slurm's own
    behaviour each cite the man page or source line they rely on:
    abbreviations (`scontrol upd`), an option hiding the verb (`scontrol -u 0
    shutdown`), missing verbs (`uhold`, `token`, `sacctmgr shutdown`, `sdiag
    --reset`), and bare interactive `scontrol`/`sacctmgr`. The 23rd is the
    `--format=` filter value described below.
  - Each layer alone refuses all 23. A test shows that a topic edited to run
    `scontrol update` is still refused.
- **`sacctmgr` is removed** from the allowlist. No topic used it, and its verbs
  abbreviate to writes (`mod`, `rem`).
- **Filter values are allowlisted characters** (`topics.FILTER_VALUE`), checked
  in `build_argv` and again in the guard. A value can no longer start with `-`
  or carry a control character. The v0.1.0 exemption that let any argument
  starting with `--format=` skip the metacharacter check, including caller
  values, is gone. So is `GLOBAL_FORBIDDEN_FLAGS`, which guarded nothing: none
  of these binaries has an exec flag, and `sacct -e` is `--helpformat`.

### Load on slurmctld and on the agent's context

- Live reads go through a gate:
  - a 10 s per-argv cache;
  - one subprocess for concurrent identical calls, while the cache is on
    (with `--cache-ttl 0` they take turns and each runs its own);
  - at most 2 Slurm commands at once;
  - at most 30 starts per 60 s;
  - clear errors when a limit refuses a call.
  All are configurable (`--cache-ttl`, `--max-concurrent`,
  `--max-calls-per-minute`). Cached answers are labelled
  `[live, cached Ns ago]`. The defaults are judgement calls, not tuned against a
  controller, and they apply per server process.
- Tool calls run in a worker thread (`asyncio.to_thread`), so a slow Slurm call
  no longer freezes the server. A test measures it. While a fake `squeue`
  slept for 1 s, a second request answered in about 0.21 s; with the handler
  run inline it took about 1.02 s. These timings were measured locally once, as
  a sanity check on the test.
- Tabular output is capped at 200 rows (`--max-rows`) and free-text output at
  50,000 characters, each with an explicit `[truncated: …]` trailer. Both caps
  must be at least 1: a cap of 0 would keep no rows, and a non-empty queue
  would read as `(no rows)`. In fixture mode the trailer suggests only the
  filters fixture mode applies, and it no longer replaces the note saying the
  server, not Slurm, applied a filter.
- While serving, each live read is logged to stderr as one bare JSON object per
  line (`argv`, `exit_code`, `seconds`, `cached`). Cache hits are logged too,
  with `"cached": true`.

### Correctness

- A failed read is no longer rendered as `(no rows)`. A non-zero exit always
  produces an error with the exit code and stderr (or `(empty)`), and any
  stdout is kept.
- Slurm output is decoded with `errors="replace"`. One invalid UTF-8 byte in a
  drain reason used to raise `UnicodeDecodeError` and take down
  `slurm_overview`. `OSError` and `ValueError` (for example a NUL byte) now
  become results instead of exceptions.
- Fixture mode applies `user`, `partition` and `account` filters to the sample
  rows, and says it did. It refuses the other filters rather than imitating
  Slurm's matching. v0.1.0 echoed `-u mchen` in the header and returned every
  user's rows.
- The fixtures are packaged (`src/slurm_mcp/fixtures/`) and read through
  `importlib.resources`. Every non-editable install used to print
  `no fixture for topic` and exit 0.

### MCP conformance

- Tool results set `isError` for failed reads, refusals and invalid input.
  Previously every result was `isError=false`.
- All inputs are validated by the server. `filters="notadict"` used to crash
  the handler into a JSON-RPC error. An unknown tool is now a protocol error
  (-32602), as the spec lists it.
- All three tools carry `readOnlyHint: true`, `destructiveHint: false`,
  `idempotentHint: true` and `openWorldHint: false`. Annotations are hints that
  clients must treat as untrusted, so the guard remains the control.
- `serverInfo.version` is set; it was empty.
- `slurm_overview`'s schema is `{"type": "object", "additionalProperties": false}`,
  as the spec recommends.

### Packaging, CLI and CI

- `server = ["mcp>=2.0,<3"]`, replacing `mcp>=1.2`: every mcp 1.x release fails
  at start-up. The following were verified locally:
  - mcp 2.0.0 on Python 3.11 and mcp 2.2.0 on Python 3.12 and 3.14: mypy
    strict, the full suite, and the stdio smoke test all pass.
  - mcp 1.30.0: construction fails.
- The distribution is renamed **`slurm-readonly-mcp`**, because `slurm-mcp` on
  PyPI is an unrelated project. The command is still `slurm-mcp` and the import
  package is still `slurm_mcp`.
- The CLI exits non-zero when any read failed; v0.1.0 exited 0 while printing
  `sinfo not found`. `serve` without the `[server]` extra prints install
  commands instead of a traceback: the git form the README documents, and
  `pip install -e '.[server]'` from a checkout. It does not print a bare
  `pip install 'slurm-readonly-mcp[server]'`, because that name is not on PyPI
  (404 on 2026-09-26). That command would fail, or install whoever registered
  the name first. The bare form belongs in the message only once the name is
  registered.
- `build_server()` is split out of `serve()`, so tests can connect in-process.
- CI:
  - the workflow token is read-only (`permissions: contents: read`);
  - actions are pinned by commit SHA, with Dependabot proposing bumps;
  - the guard-only job asserts `mcp` is absent and covers Python 3.11–3.14;
  - a server job runs mypy against the real SDK, the transport tests (a skip
    fails there) and the stdio smoke test, on mcp 2.0.0 and the newest 2.x;
  - a wheel job installs the built wheel outside the source tree and runs
    fixture mode and the stdio smoke test against the installed command.
  The workflow has not run yet; this branch is unpublished.
- `make install` uses uv when present, and otherwise refuses a Python older
  than 3.11 before creating the venv, and never re-runs `venv` over an
  existing `.venv`. A test runs the recipe with stub Pythons.
  `make smoke` runs the stdio smoke test.
- Test suite: 64 tests became 214 on 2026-09-26. Branch coverage went from the
  audit's 58% to 98% (pytest-cov 7.1.0 with `--cov-branch`, in a scratch venv
  with Python 3.14.5 and mcp 2.2.0; `serve()` stays excluded as it needs a
  stdio pipe, and is exercised by the stdio tests in a subprocess). It includes
  fake-binary tests of the live path, the gate, the CLI's exit codes, the
  Makefile's venv bootstrap, and two hypothesis properties. The stdio tests
  pass the child `PYTHONPATH=src`, so they pass from a checkout without
  installing the package (checked with mcp 2.0.0).

### Honest wording

- The fixtures are called **hand-written illustrative data** everywhere. They
  were never recordings: `diagnostics.txt` printed epochs 47.7 h away from its
  dates, now fixed. `fairshare.txt` now has sshare's 7-column shape, pinned
  with an explicit `--format`. `fixtures/README.md` says what each file
  imitates.
- "Output formats are tested against Slurm 25.x" became "column layouts follow
  the Slurm 26.05 man pages; unverified against a live cluster". "Runs against
  a real cluster" became a statement that it has not been.
- The README count "51 real mutating and injection attempts" could not be
  derived from the suite. It is now 70 adversarial argv, and a test fails if
  the README number and the corpus disagree. "fifteen more" and "permitted for
  `show`" are gone with the denylist.
- Topic detail corrections:
  - `nodes` now says drained/draining (the `%T` form), uses `%N`, and runs
    `sinfo -N` so each node gets its own line.
  - `priority` labels `%P` as the weighted partition priority and adds `%r`,
    the partition name.
  - `queue` quotes the reason-code definitions from `job_reason_codes.html`.
  - `diagnostics` scopes its "does not halt scheduling" claim to the one
    slurm-rca-bench S01 run it came from.
  - `accounting` explains `-a` and PrivateData.
- **Footprint.** v0.1.0 reported 1088 / 3029 / 4441 chars (4.1x). Those
  numbers reproduced, but:
  - most of the ratio came from an unstated assumption that a flat server
    would inline comparable guidance;
  - the flat baseline counted `sacctmgr`;
  - the server instructions were not counted.
  `slurm-mcp footprint` now prints the breakdown: 1855 chars resident
  (including 369 of instructions) against 2009 for flat schemas alone (1.08x)
  and 6238 with inlined detail (3.36x). The README states the assumption, and a
  test fails if the README numbers drift from the code.
- New README sections cover deployment (run as an unprivileged Slurm user;
  Slurm authorization is the backstop), load on slurmctld, why the CLI rather
  than slurmrestd, and prior art.

### Not done (deferred)

- A read-only slurmrestd backend, and `--json` parsing into `structuredContent`.
  Both are new features, and the tool surface would stay the same.
- A second footprint baseline captured from a real published Slurm MCP
  server's `tools/list`.
- Fixtures recorded from a real or containerised Slurm, with the version and
  command in a header.

## 0.1.0

Initial release: a three-tool read-only MCP server (`slurm_overview`,
`slurm_query`, `slurm_describe`) over seven topics, a subcommand denylist
guard, fixture mode, and the progressive-disclosure footprint measurement.
