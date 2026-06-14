# Hermes Model Guidelines

Guidance for choosing the OpenRouter model that powers **Hermes** — the AI
assistant that calls the MCP tools (`semantic_search`, `rag_query`) defined in
[`digest_mcp.py`](./digest_mcp.py) and may also serve as a general-purpose
assistant.

The page was created by Opus 4.8 on 2026-06-14.

## ZDR requirement

Every model below is run through an OpenRouter **provider that obeys ZDR (Zero
Data Retention)**. ZDR means the provider does **not store your prompts or
completions for any period of time** — nothing is retained, logged for
training, or persisted after the request is served. This is a hard requirement
for Hermes, so only ZDR-eligible endpoints are listed.

Keep **ZDR enforced at the OpenRouter account level** (Privacy settings) so that
any provider fallback also stays inside the ZDR pool.

You select **`openrouter`** as the provider in Hermes (`config.yaml`) and set the
model id; OpenRouter then routes to one of its ZDR-compliant *upstream*
sub-providers under the hood (e.g. `deepseek-v3.2` → DeepInfra / AtlasCloud /
Novita). You do **not** pick the upstream — and won't see names like "DeepInfra"
in Hermes.

> **Tool support is per-endpoint, not per-model.** A model can speak the
> tool-call format yet have no ZDR endpoint that exposes the OpenAI-style `tools`
> parameter — in which case Hermes fails with `404: No endpoints found that
> support tool use`. Always verify a candidate has a **tool-capable ZDR
> endpoint** before adopting it (see the catalog query at the bottom).

## Candidate models

| Model | Cost (in / out, $/Mtok) | Context | Reasoning | Latency | Tools under ZDR | Agentic / tool use | Access |
|---|---|---|---|---|---|---|---|
| `deepseek/deepseek-v3.2` ⭐ | $0.26 / $0.38 | 164K | Yes | Medium | ✅ | Strongest cheap agentic tool-caller; good multi-step | OpenRouter (ZDR-routed) |
| `qwen/qwen3-235b-a22b-2507` | $0.09 / $0.10 | 262K | No | Low (fast) | ✅ | Frontier-class tool calling, snappy for chat | OpenRouter (ZDR-routed) |
| `z-ai/glm-4.7-flash` | $0.06 / $0.40 | 203K | Yes | Low | ✅ | Agent-tuned GLM line; cheapest with reasoning | OpenRouter (ZDR-routed) |
| `nousresearch/hermes-4-70b` | $0.13 / $0.40 | 131K | Yes | Low | ❌ | First-party Nous fit, **but unusable for tools** (see below) | OpenRouter (ZDR-routed) |

**First-party caveat (why Hermes 4 is *not* the pick):** Hermes Agent and the
`nousresearch/hermes-4-*` models are both Nous Research, and the models are
trained natively on the Hermes function-calling format — so on paper they're the
tightest fit. **In practice they don't work for tool use under ZDR:** the only
ZDR endpoint for `hermes-4-70b` (and `hermes-4-405b`) is Nebius, and it does not
expose the `tools` parameter. There is no other ZDR provider to fall back to, so
Hermes returns `404: No endpoints found that support tool use`. The Hermes
tool-call format is also baked into Qwen3 and well-supported by DeepSeek, so the
tool-capable rows above lose little by not being the literal first-party model.

## Chosen default: `deepseek/deepseek-v3.2`

Hermes 4 would have been the first-party pick, but it has no tool-capable ZDR
endpoint (see caveat above), so the default is the strongest tool-capable ZDR
model. Pick by what you weight:

- **`deepseek/deepseek-v3.2` (default) — the stronger *reasoner*.** Best at the
  hard parts of agentic workflows: planning a multi-step task, deciding *which*
  tool and *when*, chaining calls, and recovering when a tool returns something
  unexpected. For open-ended, general-assistant agentic work, it's clearly ahead
  (τ²-Bench 84.7% Pass@1; 685B total / 37B-active MoE). Medium latency.
- **`qwen/qwen3-235b-a22b-2507` — the low-latency alternative.** Frontier-class
  tool calling and the cheapest of the bunch, noticeably snappier in chat, but
  non-reasoning — better for the bounded "route to a tool, cite the answer" loop
  than for less-scripted multi-step work.

Use `deepseek-v3.2` as the general-assistant default; switch to
`qwen3-235b-a22b-2507` if latency matters more than multi-step reasoning depth.

Other ZDR options:
* `z-ai/glm-4.7-flash` - cheapest agent-tuned fallback (tools ✅)
* `nousresearch/hermes-4-70b` / `-405b` - first-party but **no tool use under
  ZDR**, so not viable for the agent

> Prices pulled from OpenRouter's live ZDR catalog
> (`https://openrouter.ai/api/v1/endpoints/zdr`). Re-check periodically, as
> pricing and provider availability change. ZDR mechanics:
> <https://openrouter.ai/docs/guides/features/zdr>.

## Verifying a tool-capable ZDR endpoint

Before adopting any model, confirm it has at least one ZDR endpoint that exposes
the `tools` parameter (otherwise Hermes 404s on tool use):

```bash
export MODEL="deepseek/deepseek-v3.2"   # the model id you want to check
curl -s https://openrouter.ai/api/v1/endpoints/zdr \
  | python3 -c "import sys,json,os; m=os.environ['MODEL']; \
rows=[r for r in json.load(sys.stdin)['data'] if r['model_id']==m]; \
print(f'{m}: {len(rows)} ZDR endpoint(s)'); \
[print(' ', r['provider_name'], 'tools=' + str('tools' in r['supported_parameters'])) for r in rows]"
```

A model is safe to use for the agent only if at least one line prints
`tools=True`.
