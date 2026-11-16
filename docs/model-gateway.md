# Model gateway

Every agent with `model.gateway: true` sends its model calls through one gateway container
instead of talking to a model provider itself (story S2.9).

```
agent sandbox --(its own capped key)--> model gateway --+--> LM Studio (local, free)
                                                        +--> paid providers (real keys live here only)
```

## Why

- **A hard spend cap per agent.** Each agent has `budget.monthly_usd`. Past it, the gateway answers
  HTTP 402 until the month rolls over. Local model calls cost nothing and never count.
- **Real API keys never enter a sandbox.** An agent holds only its own gateway key, and its network
  policy allows the gateway address, not the providers.
- **One place to add a provider.** Add a key, list the provider on the agents that may use it.

## Setting it up

There is nothing to install by hand. `scripts/logos-agent up <name>` starts the gateway when the
agent needs it and restarts it when budgets, agents or provider keys change. To manage it directly:

```shell
scripts/logos-models up       # write config from agents/*.yaml, start or restart
scripts/logos-models status   # address, paid providers, each agent's budget
scripts/logos-models down     # stop (spend history stays on the logos-models volume)
```

In an agent definition:

```yaml
model:
  provider: lmstudio
  base_url: http://192.0.2.117:1234/v1   # real address in agents/<name>.local.yaml
  default: qwen/qwen3.6-35b-a3b
  context_length: 65536
  gateway: true
  allow: []            # paid providers this agent may use, e.g. [anthropic]
budget:
  monthly_usd: 5
```

## Adding a paid provider

1. Put its key in `~/.config/logos/models.env` (create it, mode 600):

   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```

   Recognised names: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`.
2. Add the provider to `model.allow` of each agent that may use it.
3. Run `scripts/logos-models up`.

The agent then asks for a paid model by its full name, `<provider>/<model>`. To make one the
agent's default, set `model.default` to that name.

## How it works

- The gateway is [Bifrost](https://github.com/maximhq/bifrost), pinned by image digest in
  `gateway/model_gateway.py`. Its config file is generated; do not edit it.
- It listens on the Docker bridge address (usually `172.17.0.1:8089`), which sandboxes can reach and
  the LAN cannot. Its admin API and web UI stay locked.
- Each agent's gateway key is generated on its first `up` and kept in its home volume (`.env`,
  `LOGOS_MODEL_KEY`). hermes is configured with `provider: custom` pointing at the gateway.
- Spend is tracked in the `logos-models` Docker volume and survives restarts and config changes.

## What was tested

With a made-up price on the local model: a key over its cap is refused (HTTP 402) for plain and
streaming requests, a wrong key gets 401, spend survives a restart, and a changed cap in the config
takes effect on restart. An agent behind the gateway passed the brain-write checks.

Not yet tested: a real paid provider (no key has been attached).

## The local model server

Local model calls end at LM Studio. It loads a model by itself when a request names one, but
with its own default context size, and it treats a second download of the same model (for
example `qwen3.6-35b-a3b` next to `qwen/qwen3.6-35b-a3b`) as a different model and loads both.
`gateway/local_models.py` is the one place that decides what should be loaded:

```shell
scripts/logos-models local               # what the agents need, what is loaded, anything extra
scripts/logos-models local load          # load what the agents need, with their context size
scripts/logos-models local unload <id>   # unload one model
scripts/logos-models local tidy          # unload loaded models that no agent uses
```

- `logos-agent up` loads the agent's model if it is missing, and replaces it if it is loaded
  with too small a window. It never loads a second copy.
- Web chat checks before each message. If the model was unloaded, it loads it with the agent's
  settings and the chat says "Loading the model" while it waits.
- Telegram messages go straight from the agent to the gateway, so there LM Studio's own
  on-demand loading applies.
