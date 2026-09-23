# Contributing

Thanks for looking. This is a small library with an unusually strong opinion
about what it is for, so it is worth a page explaining how to work with it.

## Getting set up

Python 3.9 or newer. No runtime dependencies.

```bash
python -m venv .venv
# Linux/macOS:  source .venv/bin/activate
# Windows:      .venv\Scripts\activate

pip install -e ".[dev]"

pytest                      # the full suite
python -m unittest discover # the same tests, no dev dependencies
ruff check .                # lint
```

CI runs the suite on Linux, macOS and Windows across the supported Python
versions. Please make sure `pytest` and `ruff check .` are clean locally first.

## The one rule

**The invariants are the product.**

Anyone can write a table of tasks with a status column. What this library
offers is a specific set of refusals: it will not complete a case without fresh
authoritative evidence, it will not replay an unsettled action attempt, it will
not hand a case to a second worker the instant the first one's lease lapses, it
will not restore a backup in an armed state.

Each of those exists because the alternative causes a duplicate effect in
somebody's real system.

So:

- **Changes that simplify integration are very welcome.** Better ergonomics,
  clearer errors, more useful CLI output, better documentation, more adapters
  in `examples/`.
- **Changes that weaken an invariant need a case made for them.** In the pull
  request, say which failure the invariant was there to prevent, and why that
  failure no longer matters. "It was inconvenient in my use case" is a reason
  to add a well-named option, not a reason to loosen the default.
- **New invariants need a failing test first**, showing the bad outcome the
  invariant prevents.

## Tests

Tests are plain `unittest` so the suite runs with no dev dependencies at all;
`pytest` runs them too and is what CI uses.

A few habits that matter here more than usual:

- **Prove the check can fail.** A test whose "nothing found" is
  indistinguishable from "the test never ran" is worse than no test, because it
  is trusted. `tests/test_snapshot.py::VerificationTests::test_the_verifier_can_fail`
  and `tests/test_filelock.py::test_a_contended_lock_times_out` are the shape to
  copy: damage the thing deliberately, assert the check notices.
- **Control time.** `ActionLedger` takes a `clock` callable. Use
  `tests/support.LedgerTestCase`, which gives you a movable `self.now`, rather
  than sleeping.
- **Test the refusal, not just the success.** For every new call that can
  refuse, assert both directions.
- **Use synthetic data.** Fictional identifiers, no real systems, no network.
  `tests/test_repository_hygiene.py` enforces the obvious parts of this and
  will fail the build on a committed ledger file, a credential-shaped string,
  a machine-specific absolute path or a contact address.

## Style

- Standard library only in `src/`. A component whose job is to be correct after
  a crash should not be able to break because of someone else's release. A dev
  dependency needs a justification; a runtime dependency needs a very good one.
- `ruff` settings live in `pyproject.toml`; line length 96.
- Comments should say *why*, especially where the code looks over-cautious.
  Most of the surprising lines in this library are surprising because they are
  protecting against something. Write that down — the next reader will
  otherwise "simplify" it away, and the tests may not catch it until it matters.
- Public functions get docstrings that name the failure they prevent.

## Reporting bugs

Please include the version, the Python version and platform, and a minimal
reproduction. **Do not attach a real ledger file** — see [SECURITY.md](SECURITY.md).
A synthetic reproduction in the style of `tests/` is ideal.

For anything that looks like a security issue, use GitHub Security Advisories
rather than a public issue.

## Pull requests

- One logical change per pull request.
- Update `README.md` when you change behaviour someone relies on.
- New public API needs a test and a docstring.
- By contributing, you agree your contribution is licensed under the MIT
  License, as in [LICENSE](LICENSE).
