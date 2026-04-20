# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.1] — 2026-04-20

### Fixed
- Anonymous-tier `transcribe_url` / `transcribe_file` now propagate the `claim_token` returned by `POST /transcribe[/url]` through to subsequent `GET /jobs/{id}` and `GET /jobs/{id}/result` polls via the `X-Claim-Token` header. Previously the token was dropped on the floor and anonymous polls returned `404 job_not_found` even though the backend had accepted the submission and was processing it.

### Changed
- Local SQLite cache schema gained a `claim_token` column with **automatic in-place migration** for databases created by 0.1.0. No user action required — the cache opens existing 0.1.0 databases, detects the missing column via `PRAGMA table_info`, runs `ALTER TABLE jobs ADD COLUMN claim_token TEXT`, and continues. Pre-0.1.1 jobs simply have a `NULL` claim_token (they wouldn't have worked anonymously anyway because of the bug above; new submissions store the token correctly).

## [0.1.0] — 2026-04-19

Initial beta release.

### Added
- Five MCP tools wired over stdio: `transcribe_url`, `transcribe_file`, `get_job_status`, `get_transcript`, `list_recent_jobs`.
- Async HTTP client (`httpx`) with exponential-backoff retries, jittered delays, and `Retry-After` honoring on `429`/`502`/`503`/`504`.
- `Idempotency-Key` header support on `POST /transcribe` and `POST /transcribe/url`. Tool-layer keys are deterministic per source, so re-running the same URL or unchanged file returns the cached `job_id` instead of double-billing.
- Local SQLite cache at `~/.whipscribe-mcp/jobs.db` powering `list_recent_jobs`. Stores only `job_id`, source kind, status, duration, and timestamp — never URLs, file paths, filenames, or transcript text.
- Anonymous opt-in telemetry (`telemetry.py`) with per-install UUIDv4 hashing under a public salt. Disable with `WHIPSCRIBE_MCP_TELEMETRY=0`.
- Configurable polling for `transcribe_*` tools via `WHIPSCRIBE_MCP_POLL_TIMEOUT_SECONDS` (default 600) and `WHIPSCRIBE_MCP_POLL_INTERVAL_SECONDS` (default 3).
- Structured error envelope (`ok: false, error: {code, message, retryable}`) consistent across every tool.

### Beta caveats
- Endpoints, response shapes, and quotas may change without notice.
- Beta credits can be invalidated without notice.
- Not suitable for production use cases where transcription failure has legal, safety, or financial consequences.
- See [`whipscribe.com/terms`](https://whipscribe.com/terms) for the full terms of service.

[Unreleased]: https://github.com/neugence/whipscribe-mcp/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/neugence/whipscribe-mcp/releases/tag/v0.1.1
[0.1.0]: https://github.com/neugence/whipscribe-mcp/releases/tag/v0.1.0
