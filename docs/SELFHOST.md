# Self-hosting AgentCheck

Bring your own key, keep your data. AgentCheck is a single container (or a
single process, no Docker needed) that judges your agent's tool calls against
your rubrics, meters every question, and reports calibrated trust. This is the
ten-minute path; the full cloud runbook is [DEPLOY-GCP.md](DEPLOY-GCP.md).

## What you need

- Docker (or Python 3.11+)
- A judge key: **any OpenAI-compatible key** (`OPENAI_API_KEY` covers OpenAI,
  OpenRouter, Together, a local vLLM/Ollama via `OPENAI_BASE_URL`) or a
  TypeSafe key (`TYPESAFE_API_KEY`). No key at all also works: an offline stub
  judge keeps everything runnable, it just grades deterministically.

Nothing phones home. Traces, keys, and results live in a SQLite file you own;
telemetry export is opt-in and off by default.

## Docker (recommended)

```bash
mkdir agentcheck && cd agentcheck
curl -fsSL https://raw.githubusercontent.com/amitashwinibhagat/agentcheck/main/deploy/gcp/docker-compose.yml -o docker-compose.yml
echo 'OPENAI_API_KEY=sk-…' > .env          # your judge key
echo 'SITE_ADDRESS=localhost:7373' >> .env # TLS off, plain local serving
docker compose up -d
open http://localhost:7373/
```

To use a non-OpenAI endpoint (OpenRouter, Ollama, vLLM):

```bash
echo 'OPENAI_BASE_URL=https://openrouter.ai/api/v1' >> .env
echo 'OPENAI_JUDGE_MODEL=anthropic/claude-3.5-sonnet' >> .env   # optional
```

Data persists in the `agentcheck-data` volume across restarts and upgrades.
Back it up like any SQLite file (see `DEPLOY-GCP.md` §"Keeping data").

## No Docker

```bash
pip install agentcheck-verify        # or: pip install -e . from a checkout
agentcheck init                      # creates the store, live-tests the judge
agentcheck key my-laptop             # prints an API key once; paste it in the UI
agentcheck serve --port 7373
open http://localhost:7373/
```

## First judged call

```bash
curl http://localhost:7373/v1/check \
  -H "Authorization: Bearer ac_…" \
  -H "Content-Type: application/json" \
  -d '{"trace":{"request":"Summarize my unread inbox",
                "tool":"search_inbox","args":{"query":"unread"}}}'
```

Or paste traces in the UI (Score my traces), or wire your agent with the
OpenAI/Anthropic/LangChain wrappers (see README §History import).

## What is off by default in a self-host

| Feature | Default | How to turn on |
|---|---|---|
| Billing | off | `AGENTCHECK_BILLING=razorpay` + plan ids |
| Sign-in / teams | off | `AGENTCHECK_AUTH=workos` + WorkOS vars |
| API docs (`/docs`, `/openapi.json`) | off | `AGENTCHECK_DOCS=1` |
| Telemetry export | off | `AGENTCHECK_OTEL_ENDPOINT=…` |
| Postgres | SQLite | `AGENTCHECK_DB_URL=postgresql://…` |
| Demo mode | off | `AGENTCHECK_DEMO=1` (public demo key, seeded data) |

None of these are required; a self-host with just a judge key is complete.

## Upgrading

Pull the newer image (or `git pull && pip install -e .`) and restart. The
store migrates itself on boot. Releases are tagged; read `CHANGELOG.md`
before jumping majors.

## License

Apache-2.0. Self-hosting is free forever, including commercial use.
