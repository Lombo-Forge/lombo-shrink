# AGENTS.md

Notes for an AI coding agent (or a person) working in this repo. `CLAUDE.md` pulls this file in.

## Contents

- [What this is](#what-this-is)
- [Layout](#layout)
- [Rules](#rules)
- [Test](#test)
- [Working efficiently](#working-efficiently)
- [Skills](#skills-claudeskills)

## What this is

LomboShrink is a small Python 3.12 program with two halves. On a main PC with a discrete graphics card it
re-encodes library video from H.264 to H.265 with ffmpeg, into a mirror folder. On the server it swaps the
smaller, verified files into the library. **The swap replaces real files, so accuracy matters more than speed.**
Standard library only (add a dependency only when it clearly earns its place).

## Layout

| Path | What |
|---|---|
| `src/lombo_shrink/core.py` | The decisions. Pure functions: no network, disk, clock or subprocess. Skip rules, accept rules, ffmpeg arguments, resume rules, manifest checks. |
| `src/lombo_shrink/media.py` | ffprobe/ffmpeg calls, the watchdog, `convert_file`. |
| `src/lombo_shrink/scan.py` | The candidate list (replaces a panel that no longer exists in this repo). |
| `src/lombo_shrink/batch.py` | The PC loop: list, convert, log, report, STOP file, limits. |
| `src/lombo_shrink/ship.py` | PC to server over ssh and scp. |
| `src/lombo_shrink/swap.py` | Server side swap. The dangerous part. |
| `src/lombo_shrink/session.py` | The expiring lock file that allows `swap --apply`. |
| `src/lombo_shrink/config.py` | Settings from `LOMBO_SHRINK_*` environment variables. |
| `src/lombo_shrink/jellyfin.py`, `notify.py` | Optional library refresh and webhook alerts. |
| `src/lombo_shrink/cli.py` | `doctor`, `candidates`, `plan`, `run`, `review`, `swap`, `session`. The only place that prints. |
| `tests/` | pytest. Shell-script stand-ins for ffmpeg, ffprobe, ssh and scp live in `conftest.py` and `test_batch.py`. |
| `scripts/leak-check.sh` | Fails when private or personal strings appear. CI runs it. |
| `docs/` | One topic per file, each with a `## Contents` list. |

## Rules

- **Keep `core.py` pure.** If a rule needs data, pass it in.
- **Safe by default.** `DRY_RUN` defaults to true. `plan`, `candidates`, `review` and `swap` without `--apply` must never change anything.
- **Never touch real files in tests.** Everything that swaps or moves runs in `tmp_path`. No test may use a real library, disk or server.
- **Do not weaken the swap.** Every check in `swap.py` (session, size and sha256, original unchanged, codec, room, same-disk copy, sha256 again, rename, only then remove) stays, and a failed check leaves the library untouched.
- **Do not weaken the accept rules** in `core.accept_problems` to make a file pass; a rejected file keeps its original.
- **A new setting** goes in `config.py` (validated, message names the variable), `.env.example` (one comment line; a test keeps them in sync), and `compose.example.yaml` if it needs passing through.
- **Fail with a message, not a traceback.** Wrap outside calls; one failed file never stops a run.
- **Never put private details in the repo:** real hostnames, IPs, webhook URLs, keys, local paths. Use `jellyfin.example.com`, `/mnt/media`, `changeme`. `scripts/leak-check.sh` enforces it; patterns live in `scripts/leak-patterns.txt`.
- Plain language in docs and messages. Call the encoding PC a "main PC with a discrete graphics card". Never imply the Jellyfin project made this.
- The Lombo look (every icon has the Lombo L) is in `docs/branding.md`.

## Test

```sh
pip install -e '.[dev]'
pre-commit install            # once: runs the checks below before every commit
pre-commit run --all-files    # ruff lint + format, strict mypy, file hygiene, leak check
pytest -q --cov               # tests with coverage (the gate is `fail_under` in pyproject.toml)
```

These are the same checks CI runs. Do not loosen a rule to make a change pass; fix the code, or say why in a
comment next to a narrow `noqa`. Tests that use shell-script stand-ins skip themselves on Windows.

Without Python installed: `docker run --rm -v "$PWD:/w" -w /w python:3.12-slim sh -c "pip install -q -e '.[dev]' && pytest -q"`.
Before claiming an encoder works, run `lombo-shrink doctor` on a machine that has the hardware.

## Working efficiently

- Optional tools that live in your own profile, not in this repo. Skip any you do not have.
- Graphify: build the code-only graph with `graphify update .` (never `/graphify .`, which costs tokens) and run
  `graphify query "<question>"` before grepping. `graphify-out/` is generated and gitignored; INFERRED edges are guesses.
- Ponytail: the smallest change that fully solves the task. Keep diffs small and focused.

## Skills (`.claude/skills/`)

- `install-into-my-stack`: set up the PC and the server, then run `doctor`, `plan` and one real file.
- `troubleshoot`: read `doctor` output, the log and swap results.
- `adapt`: change thresholds, add an encoder or a setting safely.
- `ship-a-change`, `release`: get a change into `main`; cut a release.
