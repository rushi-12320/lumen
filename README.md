# Lumen — LLM observability, evaluation & ops agent

A self-hosted, multi-project platform for teams running LLM features in production. It traces every
request, scores quality online, finds the cause of regressions with an ops agent, gates releases with
evaluations, and keeps a tamper-evident audit trail. Agents propose and people approve.

**No cloud LLM and no API keys.** Lumen never reads environment variables and never sends auth headers.
Its only optional model is a **local Ollama** server, and every LLM job waits in an approval queue until a named
person approves it. The queue shows the purpose, number of calls, estimated tokens and the exact redacted prompt.
Endpoints must be local or private addresses unless an admin changes that policy.

## Run

```
pip install -r requirements.txt
python -m lumen                 # http://127.0.0.1:8100   (Windows: run.bat)
python -m pytest -q tests       # 14 tests incl. a fake local Ollama and an SDK end-to-end run
```
First start seeds a demo tenant ("Support Copilot") with 24 h of synthetic traffic and runs the ops agent once.
Delete `data/lumen.db` to start over. Type your name in **Signed in as**, because every decision is signed.

## Deploy a free demo on Render

**Live demo:** [https://lumen-isrk.onrender.com](https://lumen-isrk.onrender.com)

The demo prompts for HTTP Basic Auth. Get the generated `LUMEN_ACCESS_PASSWORD` from the Render
service's Environment settings; any non-empty username works.

The repository includes a Render Blueprint (`render.yaml`). Create a Render Blueprint from the GitHub
repository and Render will generate `LUMEN_ACCESS_PASSWORD`. The console and API require HTTP Basic
authentication on Render; the generated password is available in the service's Environment settings.
Keep it private. `/healthz` is unauthenticated for platform health checks.

Render's free web service has ephemeral storage and may sleep. Lumen's SQLite database is stored locally,
so demo data can reset after a sleep, restart, or redeploy. This setup is for a password-protected demo,
not production. For local development, the password is optional; set `LUMEN_ACCESS_PASSWORD` to enable
the same protection.

## What the demo shows

Prompt `rag_answer` v3 went to production 6 h ago. It passed the release gate, but on real traffic it adds
unsupported claims, so groundedness drops. At the same time `smart-model` latency spikes, one answer leaks PII,
and users try prompt injections. When the ops agent runs:

- **Monitor** – 4 of 6 alert rules breach (window vs baseline).
- **Investigator** – root causes are `prompt_version=3` for groundedness (first seen at the deploy time) and `model=smart-model` for latency.
- **Curator** – proposes adding 20 failing production traces to a regression dataset, so the golden set no longer misses this case.
- **Remediator** – proposes **rolling back rag_answer v3 → v2**, plus advisories for latency, PII and injection.
- **Critic** – checks evidence, that the target exists, and that v2 actually performed better.
- **Reporter** and **Auditor** – write a cited report (an optional local-LLM summary needs your permission and is number-checked), then verify the ledger and approvals.

Approve the rollback on **Approvals**. New traffic (`+20 live traces` on Traces) then uses v2 and groundedness recovers.
Your app picks up the change because it reads `lumen.get_prompt("rag_answer")`.

## Pieces

| | |
|---|---|
| Ingestion | `sdk/lumen_sdk.py` (stdlib only: `start_trace`/`span`, `@trace`, background batching, `get_prompt`) and `POST /v1/traces` with an `X-Lumen-Key` project key (only a SHA-256 hash is stored) |
| Data protection | Email / phone / card (Luhn) / API key / IP redaction **before storage**; prompt-injection detection on inputs |
| Online evaluators | not_empty, json_valid, pii_leak, prompt_injection, refusal, toxicity, groundedness (RAG), latency_slo |
| Monitoring | Dashboards by route / model / prompt version / segment, alert rules, incidents with root-cause tables |
| Evaluation | Datasets (JSONL/CSV or from traces), experiments (upload outputs, or generate with the local LLM after approval), A/B compare with bootstrap CI and sign test, LLM judge (local, approval-gated) |
| Release gate | Pass rate, no evaluator regression vs the production baseline, no PII, cost increase limit. `tools/lumen_gate.py` exits 1 in CI when it fails |
| Prompt registry | Versions and labels; promotion only with a passing gate and a named approver; rollbacks only after the Critic passes them |
| Guardrails | Default-deny policy engine, kill switch, rate limits, step budget, data-residency check. Red-team suite: 19 attacks |
| Audit | SHA-256 hash-chained ledger + Merkle root, tamper test, deterministic **replay** of every agent run |

## Local LLM (optional)

Install [Ollama](https://ollama.com), then run `ollama pull llama3.1`. In **Settings**, enable the local LLM (`http://localhost:11434`).
Use **Test connection**, which only lists the installed models. LLM judge and generation jobs then appear on **Approvals**.

## CI example

```
python tools/lumen_gate.py export --key lmn_... --dataset rag-golden > items.jsonl
# run your candidate on each item -> outputs.jsonl lines: {"item_id": "...", "output": "..."}
python tools/lumen_gate.py gate --key lmn_... --dataset rag-golden --name "PR-42" \
    --prompt-name rag_answer --prompt-version 5 --outputs outputs.jsonl
```

## Before production

Add SSO/RBAC (names are self-declared here), put HTTPS in front, move SQLite to Postgres for high volume,
and set your real model prices in Settings. The default prices are examples.
