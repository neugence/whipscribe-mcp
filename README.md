# whipscribe-mcp

MCP server for [Whipscribe](https://whipscribe.com) — transcribe audio and video from a URL or local file via Claude Desktop, Claude Code, Cursor, Windsurf, or any MCP-compatible client.

> ### ⚠️ Beta service
> - `whipscribe-mcp` and the Whipscribe API are in beta. Endpoints, response shapes, and quotas may change without notice.
> - Jobs can fail, stall, or return partial output. Retry logic is your responsibility.
> - **Beta credits can be invalidated without notice** — e.g. for infrastructure migration, key compromise, or pricing reset. Beta credits do not convert to cash.
> - We will give 7 days' written notice before any pricing change that affects active keys, and honor unused credits at the old rate for 7 days after the notice.
> - Not suitable for production use cases where transcription failure has legal, safety, or financial consequences.
> - By installing and using this package, you accept the full terms at [whipscribe.com/terms](https://whipscribe.com/terms).

## Install

```bash
uvx whipscribe-mcp
```

Alternatives:

```bash
pipx install whipscribe-mcp
pip install whipscribe-mcp
```

Requires Python 3.10+.

## Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "whipscribe": {
      "command": "uvx",
      "args": ["whipscribe-mcp"]
    }
  }
}
```

Optional: set `WHIPSCRIBE_API_KEY` in `env` to unlock paid quota.

```json
{
  "mcpServers": {
    "whipscribe": {
      "command": "uvx",
      "args": ["whipscribe-mcp"],
      "env": {
        "WHIPSCRIBE_API_KEY": "your-key"
      }
    }
  }
}
```

Restart Claude Desktop. Type "transcribe this podcast URL: …" and Claude will call the tool.

## Tools

| Tool | Description |
|---|---|
| `transcribe_url(url, language?, diarize?, word_timestamps?)` | Transcribe audio/video from any URL (YouTube, podcast feeds, direct file links). |
| `transcribe_file(path, language?, diarize?, word_timestamps?)` | Transcribe a local audio/video file. |
| `get_job_status(job_id)` | Check progress of a running job. |
| `get_transcript(job_id, format)` | Fetch the transcript in `txt`, `json`, `srt`, `vtt`, or `docx`. |
| `list_recent_jobs(limit?)` | Browse your recent jobs (local cache). |

All tools return a JSON object of the shape:

```json
{
  "ok": true,
  "job_id": "...",
  "status": "done",
  "transcript_preview": "first 300 chars...",
  "url_to_full": "https://whipscribe.com/view?id=...",
  "duration_sec": 967.3,
  "beta_notice": "..."
}
```

On failure:

```json
{
  "ok": false,
  "error": {
    "code": "upload_failed",
    "message": "human-readable explanation",
    "retryable": true
  }
}
```

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `WHIPSCRIBE_API_KEY` | No | — | API key (unlocks paid quota; anonymous free tier works without it) |
| `WHIPSCRIBE_API_BASE` | No | `https://whipscribe.com/api/v1` | Override API base URL (e.g. for staging) |
| `WHIPSCRIBE_MCP_TELEMETRY` | No | `1` | Set to `0` to disable anonymous usage telemetry |
| `WHIPSCRIBE_MCP_POLL_TIMEOUT_SECONDS` | No | `600` | Max seconds `transcribe_url` / `transcribe_file` waits before returning the job_id with a non-terminal status |
| `WHIPSCRIBE_MCP_POLL_INTERVAL_SECONDS` | No | `3` | Seconds between job-status polls during transcription |

## Privacy

Opt-in anonymous telemetry (on by default; disable with `WHIPSCRIBE_MCP_TELEMETRY=0`):

**What we collect** (metadata only):
- Anonymous install hash: `sha256(machine_id + salt)[:16]`
- Package version, OS, Python version
- Tool name, duration in milliseconds, error code

**What we never collect:**
- URLs you transcribe
- Local file paths
- API keys
- Transcript text
- Email or any personally identifying information

The anonymization algorithm is in this repo (`src/whipscribe_mcp/telemetry.py`). Inspect it. If it's not acceptable, turn it off.

## Pricing

See [whipscribe.com/pricing](https://whipscribe.com/pricing) for current rates. The free tier works without an API key at reduced rate limits.

## License

[Apache License 2.0](./LICENSE). Copyright 2026 Neugence Technology Pvt. Ltd.

## Contact

- Website: [whipscribe.com](https://whipscribe.com)
- Email: [contact@neugence.ai](mailto:contact@neugence.ai)
- Issues: [github.com/neugence/whipscribe-mcp/issues](https://github.com/neugence/whipscribe-mcp/issues)
