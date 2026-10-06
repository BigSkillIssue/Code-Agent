# Providers

Forge talks to models through four adapters:

| Adapter (`kind`) | Module | Covers |
| --- | --- | --- |
| `openai_compat` | `providers/openai_compat.py` (+ `responses.py`) | OpenAI (Chat Completions or Responses wire), OpenRouter, Groq, DeepSeek, Mistral, xAI, Together, Fireworks, Perplexity, Cerebras, Azure OpenAI, Ollama, LM Studio, vLLM |
| `anthropic` | `providers/anthropic.py` | Claude on the Anthropic API, Amazon Bedrock, Google Vertex AI |
| `google` | `providers/google.py` | Gemini API, Vertex AI |
| `litellm` | `providers/litellm.py` | Everything else LiteLLM reaches (Cohere, Watsonx, Sagemaker, Cloudflare, ...) |

Models without native tool calling (set `tools = false` under `[models."<provider>/<model>"]`)
get prompt-based tool calling automatically (`providers/fallback_tools.py`).

Every provider below has a **preset**: if a role names it and there is no `[providers.<name>]`
table, the preset is used, so only the API key has to be set. The snippets show the explicit
form, which you need only to change something.

## Test status

| # | Provider | Offline tests | Live contract test (`pytest tests/contract -m live`) |
| --- | --- | --- | --- |
| 1 | Anthropic | `tests/test_anthropic.py` (recorded SSE) | `anthropic` case |
| 2 | OpenAI, Chat Completions | `tests/test_openai_compat.py` | `openai` case |
| 3 | OpenAI, Responses | `tests/test_openai_responses.py` | `openai-responses` case |
| 4 | Google Gemini | `tests/test_google.py` (recorded stream) | `gemini` case, **passed live 2026-10-06** (gemini-3.8-flash) |
| 5 | OpenRouter | shared with OpenAI-compatible | `openrouter` case |
| 6 | Groq | shared with OpenAI-compatible | `groq` case, **passed live 2026-10-06** (openai/gpt-oss-120b) |
| 7 | DeepSeek | shared with OpenAI-compatible | `deepseek` case |
| 8 | Mistral | shared with OpenAI-compatible | `mistral` case |
| 9 | xAI | shared with OpenAI-compatible | `xai` case |
| 10 | Together | shared with OpenAI-compatible | `together` case |
| 11 | Ollama (local) | `tests/test_ollama_setup.py` | `ollama` case (`FORGE_LIVE_OLLAMA=1`), **passed live 2026-10-06** (forge-qwen3:4b-instruct on CPU) |
| 12 | LiteLLM | `tests/test_fallback_tools.py` (mock response) | `litellm` case |

The live contract cases are skipped unless the provider's API key is set. Each case checks a
text reply with usage and a full tool-call round trip. See `PROGRESS.md` for which live runs
have been recorded.

## Configuration snippets

Put these in `~/.forge/forge.toml` (or `.forge/config.toml` in a trusted project), then point a
role at `"<provider>/<model>"`:

```toml
[roles]
coder = ["anthropic/claude-sonnet", "openai/gpt-5"]   # fallback chain
```

### 1. Anthropic

```toml
[providers.anthropic]
kind = "anthropic"
api_key_env = "ANTHROPIC_API_KEY"
```

Amazon Bedrock and Google Vertex use the same adapter with a special `base_url`
(credentials come from the usual AWS / Google environment):

```toml
[providers.bedrock]
kind = "anthropic"
base_url = "bedrock://us-east-1"

[providers.claude-vertex]
kind = "anthropic"
base_url = "vertex://my-gcp-project/us-east5"
```

### 2. OpenAI (Chat Completions)

```toml
[providers.openai]
kind = "openai_compat"
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"
```

### 3. OpenAI (Responses)

```toml
[providers.openai]
kind = "openai_compat"
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"
wire = "responses"
```

### 4. Google Gemini

```toml
[providers.gemini]
kind = "google"
api_key_env = "GEMINI_API_KEY"

[providers.gemini-vertex]
kind = "google"
base_url = "vertex://my-gcp-project/europe-west4"
```

### 5. OpenRouter

```toml
[providers.openrouter]
kind = "openai_compat"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"
```

### 6. Groq

```toml
[providers.groq]
kind = "openai_compat"
base_url = "https://api.groq.com/openai/v1"
api_key_env = "GROQ_API_KEY"
```

### 7. DeepSeek

```toml
[providers.deepseek]
kind = "openai_compat"
base_url = "https://api.deepseek.com/v1"
api_key_env = "DEEPSEEK_API_KEY"
```

### 8. Mistral

```toml
[providers.mistral]
kind = "openai_compat"
base_url = "https://api.mistral.ai/v1"
api_key_env = "MISTRAL_API_KEY"
```

### 9. xAI

```toml
[providers.xai]
kind = "openai_compat"
base_url = "https://api.x.ai/v1"
api_key_env = "XAI_API_KEY"
```

### 10. Together

```toml
[providers.together]
kind = "openai_compat"
base_url = "https://api.together.xyz/v1"
api_key_env = "TOGETHER_API_KEY"
```

### 11. Ollama (local, no key)

Run `forge ollama setup`; it does the following steps for you (details in the quick start).
1. It picks a model for the machine and pulls it.
2. It creates `forge-<model>` with a `num_ctx` that fits Forge's prompts. Ollama's default
   window is a few thousand tokens and longer prompts are cut silently.
3. It writes the roles and the model entry into `~/.forge/forge.toml`.

`forge ollama status` checks the server and the context windows. By hand:

```toml
[providers.ollama]          # preset; only needed for another host or port
kind = "openai_compat"
base_url = "http://localhost:11434/v1"

[models."ollama/forge-qwen3:8b"]
context_window = 32768   # must match the model's num_ctx
tools = true             # false: prompt-based tools are used
```

Thinking models (e.g. `qwen3:4b`) spend many tokens before each answer; on a CPU that is very
slow. That is why setup suggests the `-instruct` variant there. Measured on 4 CPU cores: one tool
call took 21 output tokens and 14 s with `qwen3:4b-instruct`, against 248 tokens and 50 s with
`qwen3:4b`.

### 12. LiteLLM

The model id after the provider name is LiteLLM's own id; LiteLLM reads the vendor's usual key
variable itself.

```toml
[providers.litellm]
kind = "litellm"

[roles]
coder = ["litellm/cohere/command-r-plus"]
```

### Azure OpenAI

```toml
[providers.azure]
kind = "openai_compat"
base_url = "https://my-resource.openai.azure.com/openai/v1"
api_key_env = "AZURE_OPENAI_API_KEY"   # sent as the api-key header
```

## Model settings

Known models (Claude, GPT-5, Gemini 2.5, DeepSeek, Llama on Groq) have built-in context sizes and
prices in `providers/catalog.py`. Override or add any model:

```toml
[models."openrouter/qwen/qwen3-coder"]
context_window = 262144
max_output = 65536
cost_in = 0.40
cost_out = 1.60
```

## Free tiers for testing

These providers have free tiers (limits change; check each provider's page). Put the key in
the variable shown, then run `uv run pytest -q -m live tests/contract`; cases without a key
are skipped.

| Provider | Variable | Notes |
|---|---|---|
| Google Gemini (AI Studio) | `GEMINI_API_KEY` | measured 2026-10-06 for gemini-3.8-flash: 5 requests per minute and 20 per day; free-tier data may be used for training |
| Groq | `GROQ_API_KEY` | measured 2026-10-06 for openai/gpt-oss-120b: 8,000 tokens per minute (one agent request is 4-9k tokens, so expect waits) |
| OpenRouter | `OPENROUTER_API_KEY` | models ending in `:free` cost nothing (daily limit) |
| Mistral | `MISTRAL_API_KEY` | free "Experiment" plan |
| Ollama (local) | none | free and unlimited; needs a capable machine |

An agent makes many requests per task, so free tiers run out quickly. Forge copes with that:
- it waits as long as the provider asks (also when the wait is only in the error body);
- it switches to the next model of the role when a request is above a per-minute quota;
- it skips a model for the rest of a long block (a daily quota) instead of asking again.

Put two providers in every role so one can take over. A free-only setup for `~/.forge/forge.toml`,
the one used for the live runs on 2026-10-06:

```toml
[roles]
refiner    = ["groq/openai/gpt-oss-120b", "gemini/gemini-3.8-flash"]
planner    = ["groq/openai/gpt-oss-120b", "gemini/gemini-3.8-flash"]
coder      = ["groq/openai/gpt-oss-120b", "gemini/gemini-3.8-flash"]
reviewer   = ["groq/openai/gpt-oss-120b", "gemini/gemini-3.8-flash"]
compressor = ["groq/openai/gpt-oss-120b", "gemini/gemini-3.8-flash"]
explore    = ["groq/openai/gpt-oss-120b", "gemini/gemini-3.8-flash"]
researcher = ["groq/openai/gpt-oss-120b", "gemini/gemini-3.8-flash"]
```

Model names change often: `curl -s https://api.groq.com/openai/v1/models -H "Authorization: Bearer $GROQ_API_KEY"` lists Groq's,
and Gemini's error message names the replacement when a model is retired.
