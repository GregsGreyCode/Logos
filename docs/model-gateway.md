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
scripts/logos-models local use <id>      # switch every agent to another model on the server
scripts/logos-models local use default   # back to the model in each agent's definition
```

`use` is for when the graphics card is needed for something else: it writes the model into the
untracked `agents/all.local.yaml` (which applies to every agent), unloads the model the agents
were using, loads the new one and restarts the agents, about two minutes in all. If the model
server is switched off it still switches the agents; the model loads when the server is back.

- `logos-agent up` loads the agent's model if it is missing, and replaces it if it is loaded
  with too small a window. It never loads a second copy.
- Web chat checks before each message. If the model was unloaded, it loads it with the agent's
  settings and the chat says "Loading the model" while it waits.
- Telegram messages go straight from the agent to the gateway, so there LM Studio's own
  on-demand loading applies.

## When the model server is off

LM Studio runs on a machine that is also used for other things, so it is sometimes closed.
That machine then drops connections without answering, and without help every caller would
wait out a fifteen-minute timeout in silence. Three things prevent that:

| Where | What happens | How fast |
| --- | --- | --- |
| Model guard (`logos-model-guard`, `gateway/model_guard.py`) | Sits between the gateway and LM Studio. If LM Studio does not accept the connection in 3 seconds, the call is refused with a plain 503: "The local model server is switched off or busy". Otherwise traffic passes straight through, streamed. | 3 s |
| Agents (hermes `agent.auto_recovery_cycles: 0`) | After the normal three attempts the agent tells the person the model is unavailable, with the guard's words, instead of retrying in the background for another eight minutes. This is what a Telegram message gets. | about 20 s |
| Web chat | Checks the model server before handing the message to the agent. If it is off, the chat says so, gives the typed words back, and shows "Model server off" in the header and on the Agents screen. | 3 s |

Scheduled jobs that need the model fail the same way while it is off and run again at their
next time. Voice notes are still turned into text, because that runs on this server.

## Paid fallbacks (switched off: the brain stays local)

**Since 2026-11-17 no agent may use a paid model.** Every agent request carries its memories
and brain index, so any request that reaches a cloud provider takes the brain out of the house.
`BRAIN_STAYS_LOCAL` in `gateway/model_gateway.py` enforces this in code: the gateway config
gives each agent only the local model, the gateway container never receives `models.env`, and
`logos-agent up` refuses a definition with `model.fallbacks`, a paid provider in `model.allow`,
a model provider in `providers`, a non-local `model.provider`, or a paid model for the
consolidation or digest job. When the local model is off, agents say so and do not answer.

The planned replacement is a brain-free advisor: a stateless cloud helper that only ever sees
questions the private agent writes for it, through a filter that checks them for personal
identifiers and logs what was sent. The fallback code below is kept, and tested, for that work.


The local model is free but not always there. An agent can name paid models to fall back on,
tried in order:

```yaml
model:
  fallbacks:
    - model: anthropic/claude-haiku-5-5     # first choice when the local model cannot answer
      monthly_usd: 4                        # this provider's share of the agent's monthly total
    - openrouter/google/gemini-2.5-flash-lite   # gets what is left of the total
budget:
  monthly_usd: 5                            # the agent's total for the month, across all paid providers
```

Then `scripts/logos-models up`. Agents do not need restarting: they keep asking the gateway for
their local model, and the gateway does the rest.

- **When it falls back.** Whenever the local model fails: the model guard's refusal while LM
  Studio is off, a connection error, a timeout, an error reply. And again from one paid model to
  the next when a provider fails (5xx, 429) or has no budget left.
- **Providers need a key.** An entry whose provider has no key in `models.env` is left out, so
  the chain can be written before the keys exist.
- **The money limit is per paid provider, and it is hard.** Each provider may spend its share
  and no more; the shares add up to at most `budget.monthly_usd`. When a provider's share is
  spent it is skipped and the next is tried. When every share is spent the agent has the local
  model only, and says the model is unavailable while that is off.
- **The local model is never refused for money.** The limit is deliberately not put on the
  agent's key as a whole: Bifrost enforces a key-wide limit on every call, including free local
  ones, so reaching it would stop the agent altogether. (When a key-wide limit from an earlier
  config is no longer wanted, `logos-models up` removes it from the gateway's store; Bifrost
  does not do that by itself.)
- **Which model answered.** Every reply carries it: `model` in the body, and the
  `x-bifrost-provider` / `x-bifrost-resolved-model` / `x-bifrost-routing-info-is-fallback`
  headers. The gateway's log (`logs.db` on the `logos-models` volume) records it with the cost.
- **Speed.** While LM Studio is off the guard refuses instantly (it remembers for 15 seconds
  that the server did not answer), so a fallback reply does not start late. A server that has
  just come back is used again within those 15 seconds.
- **`model.fallbacks` replaces `model.allow`** for that agent: with a chain, only the models in
  it can be reached.

Under the hood this is one Bifrost routing rule per agent (`governance.routing_rules`, scoped
to the agent's virtual key) and a `budgets` entry on each paid provider config.
