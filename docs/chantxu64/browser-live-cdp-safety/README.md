# Live CDP browser tab safety prompts

## Purpose

Make the model-visible Browser tool contracts describe the destructive side
effect of navigating a user-owned Chrome tab through a live CDP connection, and
the possibility that a Browser screenshot belongs to a different target than a
DrissionPage or raw-CDP workflow is diagnosing.

## Difference From Upstream

The upstream descriptions presented `browser_navigate` as ordinary navigation
and `browser_vision` as a screenshot of the current page. This fork explicitly
states that, under live CDP, navigation can replace preserved app or login
state; it prohibits using FIP, BPM, and other preserved tabs for unrelated
navigation, and requires a task-owned safe tab/session first. It also requires
target verification before using a Browser screenshot as evidence about a
separately bound target.

This remains a prompt/schema guard, not a runtime isolation feature. It does
not create a new tab, select a target, or prevent a caller from navigating an
existing target. A future runtime ownership/isolation design must be reviewed
as a separate feature rather than assumed to exist here.

## Files

- `tools/browser_tool.py` — model-visible `browser_navigate` and
  `browser_vision` schema descriptions.
- `tests/fork/test_browser_live_cdp_safety.py` — fork-preservation contract
  that imports the registered schemas and checks the safety clauses.
- `docs/LOCAL_MODIFICATIONS.md` — fork delta and merge index entry.

## Configuration / Usage

No configuration is added. The warnings are included in the tool schemas sent
to models after the running Gateway has loaded this source. A source edit alone
does not reload an already-running Gateway.

For ordinary web research, prefer web retrieval tools. When interactive Browser
work is necessary against a live CDP browser, do not navigate a preserved tab;
establish a task-owned safe tab/session first.

## Merge Guidance

- Preserve when: upstream lacks an equivalent or stronger model-visible warning
  about live-CDP tab replacement and cross-target screenshot evidence.
- Drop when: upstream provides an equivalent or stronger contract and tests it
  at the registered schema boundary.
- Ask user when: upstream replaces the Browser/CDP architecture with explicit
  target ownership or isolation, because that may supersede this prompt guard.

## Verification

```bash
.venv/bin/python -m pytest tests/fork/test_browser_live_cdp_safety.py -q -o 'addopts='
.venv/bin/python -m py_compile tools/browser_tool.py tests/fork/test_browser_live_cdp_safety.py
```

The preservation test must fail if the live-CDP or cross-target safety clauses
are removed from the registered schemas, then pass after the clauses are
restored.

## LOCAL_MODIFICATIONS Entry

Corresponding entry in `docs/LOCAL_MODIFICATIONS.md`:
`### 14. Live CDP browser tab safety prompts`.
