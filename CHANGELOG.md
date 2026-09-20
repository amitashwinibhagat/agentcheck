# Changelog

All notable changes to AgentCheck. Formats: [Keep a Changelog]; versions are
tagged on GitHub and published as `agentcheck-verify` wheels / GHCR images.

## [0.2.0] — 2026-09-20

### Added
- **OpenAI-compatible judge (`AGENTCHECK_JUDGE=openai` or just set
  `OPENAI_API_KEY`)** — the BYOK path. Works with any OpenAI-compatible
  endpoint via `OPENAI_BASE_URL` (OpenRouter, Together, vLLM, Ollama).
  Same robustness contract as the TypeSafe judge: transient retries, permanent
  failures carry the request id, corrupt confidence is refused, unknown
  choice labels never carry confidence.
- Judge selection follows whichever key exists: `AGENTCHECK_JUDGE` env >
  `TYPESAFE_API_KEY` > `OPENAI_API_KEY` > stub. Keyless containers still boot.
- **Self-serve signup** — first sign-in creates the user's workspace and the
  UI mints a first key (`POST /v1/me/keys`), shown once. Sign in/out in the
  header on hosted instances.
- `docs/SELFHOST.md` — the ten-minute private-deploy guide.
- Apache-2.0 license; the repo is public.

### Fixed
- Multi-key legacy stores no longer crash on migration (per-row sentinel).
- Connect snippet uses the served origin instead of hardcoded localhost.
- JSONL upload parsing reports `line N` and no longer swallows valid rows.

## [0.1.0] — 2026-09

Initial release: the check pipeline (judge → reliability correction →
deterministic screens → verdict), calibration (ECE/Brier, measured trust
tier), the red-team suite (467 attacks, 28 families), workspaces/invites,
WorkOS + OIDC sign-in, the Decision Log UI, and the eval harness.

[Keep a Changelog]: https://keepachangelog.com/
[0.2.0]: https://github.com/amitashwinibhagat/agentcheck/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/amitashwinibhagat/agentcheck/releases/tag/v0.1.0
