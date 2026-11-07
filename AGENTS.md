# AGENTS.md

Multi-zone voice bot: wake gate → STT → intelligence → TTS → room speakers. `bot.py`'s docstring and `README.md` document the pipeline, setup, and repo layout — read those first.

## Laws

- **Public repo.** Commits, issues, comments, and tags carry no personal details, LAN addresses, or private paths. Grep staged `+` lines for them before every commit.
- **Comments are not logs.** One to three sentences, intent only — free of dates, names, and incident stories. Docstrings may run longer under the same content rule.
- **Issues are the working task list.** File every confirmed bug and every idea worth keeping as a GitHub issue when it comes up; a thin body is fine, sweep for duplicates first.
- **Ask first on breaking changes.** Config keys, dashboard API response shapes, and the node command protocol change through an issue proposal, never a surprise.
- **Suite green reaches main.** Nothing merges without the full suite passing on the committed tree.
- **Secrets live only in `.env`.** Never print a value; `config.yaml` is secrets-free by law.
- **Hot paths.** The turn pipeline and the speaker/reply paths change in small, isolated commits with the suite green.
- **Shipped features earn a CHANGELOG entry.** Ride new entries in [Unreleased] until the next release.

## Commands

One test at a time — each test's own `Run:` header is the per-file source of truth:

```bash
(cd tests && ../.venv/bin/python <test>.py)            # most tests
(cd tests && ../.venv/bin/python <test>.py ../core)    # some take the core path as argv[1]
.venv/bin/python tests/<test>.py                       # a few run from the repo root
```

A wrong cwd surfaces as `ModuleNotFoundError`, which reads like a missing dependency but is not one. Live tests (real speaker, LAN scan) stay off unless `LVI_TEST_LIVE=1`. The kokoro PCM goldens in `localai_golden_test.py` were recorded on one machine and fail on any other (issue #49) — a golden failure on a fresh box is expected, not a regression.
