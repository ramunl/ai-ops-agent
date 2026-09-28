# Python rules review — 2026-09-27

Reviewed `ai_ops_agent/` against `/opt/ai-rules/global/python.md`.
This completes an existing unpublished refactor and integrates the latest
`origin/main`. The deployed checkout is unchanged.

## Changes

- Split the 1,324-line Telegram module into command adapters and services for
  metrics, package reports, fleet updates, tool updates, and metadata discovery.
- Consolidated command-failure detection, including timeout responses, and used
  an AgentUpdateError for checkout update failures.
- Preserved shared-core authorization, command hints, version/core reporting,
  configured service allowlists, and report-before-self-restart ordering.
- Retained recursive submodule initialization after pulling a checkout; failures
  prevent service restarts. Added regression cases for this sequence.
- Added diagnostic logging for previously silent metadata and measurement
  parsing failures without logging environment-file contents.
- Enforced imports, 88-character lines, naming, parameter and return annotations,
  public and constructor docstrings, and mutable/default-call checks through Ruff.
  CI runs the same lint, formatting, and pytest checks with submodules initialized.
- Updated the architecture and development instructions.

## Validation and limits

- 106 tests and 2 subtests pass, including the pinned shared-core tests.
- Ruff lint and formatter checks pass.
- Largest production module: 209 lines. No production function exceeds
  55 lines; no function requires more than five parameters.
- No wildcard imports, mutable parameter defaults, or unowned TODO comments found.
- Tests use mock operations and temporary files; no live deployments, package
  upgrades, service restarts, or Telegram messages were performed.

The shared core is tested but not modified. Type annotations are lint-enforced;
a full mypy/pyright analysis and exhaustive behavioral coverage are not claimed.
