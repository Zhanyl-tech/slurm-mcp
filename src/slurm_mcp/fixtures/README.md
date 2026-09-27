# Fixtures

**Hand-written, illustrative data.** None of these files was captured from a
real cluster. Each one is shaped like the output of its topic's command so the
whole surface (`--fixtures`) runs on a laptop. No result should be quoted from
them.

| file | imitates | shape |
|---|---|---|
| `queue.txt` | `squeue --noheader -o '%i\|%P\|%u\|%T\|%M\|%D\|%R'` | 7 pipe-delimited columns |
| `nodes.txt` | `sinfo --noheader -N -o '%N\|%P\|%T\|%C\|%G\|%E'` | 6 columns, one line per node |
| `accounting.txt` | `sacct -a -X --parsable2 --noheader --format=…` | 8 columns |
| `priority.txt` | `sprio --noheader -o '%i\|%r\|%u\|%Y\|%A\|%F\|%J\|%P\|%Q'` | 9 columns; the factors sum to the total |
| `fairshare.txt` | `sshare --noheader --parsable2 -a --format=Account,User,RawShares,NormShares,RawUsage,EffectvUsage,FairShare` | 7 columns |
| `diagnostics.txt` | `sdiag` | abbreviated: real output has more sections |
| `config.txt` | `scontrol show config` | abbreviated: a handful of keys |

The column layouts follow the Slurm 26.05 man pages (checked 2026-09-26). They
have not been compared with a live cluster's output. When a real or
containerised Slurm is available, these should be replaced by recordings whose
header states the Slurm version and the exact command.

Timestamps in `diagnostics.txt` are UTC, and the epoch in brackets matches the
printed date.
