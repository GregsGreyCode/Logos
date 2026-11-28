# llama-swap: the local model without LM Studio

The agents' model can run on llama.cpp's `llama-server` behind
[llama-swap](https://github.com/mostlygeek/llama-swap) instead of LM Studio (since 28 Nov).
Every setting (window, slots, GPU layers, experts on the CPU, KV-cache type) is in one
`config.yaml` on the model PC, so a model is only ever loaded with those settings: there is
no "just in time" load at someone else's defaults, which the model guard had to undo for
LM Studio.

Measured on the same PC (RTX 4080 Super 16 GB, qwen3.6-35b-a3b Q4_K_M), 28 Nov:

| | LM Studio | llama-swap |
| --- | --- | --- |
| Reading a 6,830-token prompt | ~735 tokens/s | ~2,450 tokens/s |
| Writing | ~40 tokens/s | ~64 tokens/s |

Most of the gain is `--n-cpu-moe`: some layers' experts on the GPU instead of all on the CPU,
which LM Studio's API cannot set.

## On the model PC (Windows)

1. From <https://github.com/ggml-org/llama.cpp/releases>: `llama-…-bin-win-cuda-<ver>-x64.zip`
   and the matching `cudart-llama-bin-win-cuda-<ver>-x64.zip` (CUDA 13 needs driver 580 or
   newer; otherwise 12.4). From <https://github.com/mostlygeek/llama-swap/releases>:
   `llama-swap_…_windows_amd64.zip`. Unzip the files of all three into one folder, `C:\llama`.
2. `C:\llama\config.yaml`; the model id must be the one in the agents' `model.default`:

   ```yaml
   models:
     "qwen/qwen3.6-35b-a3b":
       cmd: >
         C:\llama\llama-server.exe --port ${PORT}
         -m "C:\Users\<you>\.lmstudio\models\...\Qwen3.6-35B-A3B-Q4_K_M.gguf"
         --mmproj "C:\Users\<you>\.lmstudio\models\...\mmproj-Qwen3.6-35B-A3B-BF16.gguf"
         -c 135168 --parallel 3 --kv-unified
         -ngl 99 --n-cpu-moe 25 -b 2048 -ub 2048 --load-mode none
         -fa on -ctk q8_0 -ctv q8_0
         --jinja
       ttl: 0
   ```

   - `--mmproj` lets the agents see photos; without it a photo fails.
   - `--parallel 3`: the guard runs two replies at once, and a waiting speaker question a
     third. `--kv-unified` lets them share the one window.
   - `--n-cpu-moe N`: the experts of N layers stay on the CPU, the rest go to the GPU. Lower
     is faster until the GPU is full: keep Task Manager's "Shared GPU memory" near 0 (it was
     14.8 of 16 GB dedicated at 25, with the image part).
   - Keep every line of the `>` block at the same indent.
3. `infra/gpu-pc/logos-gpu-watch.ps1` starts llama-swap at logon and stops it while a game
   runs (install steps at its top). By hand: `llama-swap.exe --config config.yaml --listen 0.0.0.0:8080`.

## In Logos

- Each agent's `model.base_url` (in `agents/<name>.local.yaml`) is `http://<model PC>:8080/v1`.
- `gateway/local_models.py` tells the two servers apart (llama-swap answers `/running`) and
  gives llama-swap's list in LM Studio's shapes, so the Models page, the model picker and the
  chat's "Loading the model" work as before. Waking the model asks `/upstream/<model>/health`.
- The model guard (`gateway/model_guard.py`) loads nothing on llama-swap, and reads the
  window and slots from its list and command line.
- The Models page shows what is running (window, slots, KV cache, experts on the CPU) and
  "Restart the model" (with the config's settings) instead of LM Studio's app-only advice.
- Back to LM Studio: put port 1234 back in the agents' `base_url` and run `scripts/logos-deploy`.

## Numbers for Prometheus and Grafana (28 Nov)

With Settings › Metrics on (docs/metrics.md), Logos's `/metrics` also carries the model PC's own
numbers, and the Models page shows the live ones in the model's card (GPU memory and temperature,
reading and writing speed, prompt cache, what the PC is working on). Logos asks the PC at most every
15 s, with 2.5 s timeouts, and never waits more than 4 s in a scrape; when the PC is off only
`logos_model_server_up{server} 0` is exported (never an old number).

### What llama-swap and llama-server offer

- **llama-server `/metrics`** (only with `--metrics`), reached through llama-swap at
  `/upstream/<model>/metrics`. Counters: `llamacpp:prompt_tokens_total` (read, *excluding* cached),
  `prompt_tokens_cached_total`, `prompt_seconds_total`, `tokens_predicted_total`,
  `tokens_predicted_seconds_total`, `n_decode_total`, `n_tokens_max`, `spec_decode_*`. Gauges:
  `prompt_tokens_seconds`, `predicted_tokens_seconds` (both *since the previous scrape*: llama-server
  resets them each time, so they are 0 when idle), `requests_processing`, `requests_deferred`,
  `n_busy_slots_per_decode`. Current builds have no `kv_cache_usage_ratio` / `kv_cache_tokens`;
  Logos passes them on when an older build sends them. Source: llama.cpp `tools/server/README.md`
  and `server-task.cpp` (`to_metrics`). Logos only asks `/upstream/<model>/…` for a model that
  `/running` says is `ready`: asking it for another would load that model.
- **llama-swap `/running`**: each model's `state` and `cmd` (window `-c`, `--parallel`,
  `--n-cpu-moe`, `-ctk`, `-fa` are read from the command line).
- **llama-swap `/metrics`** (its own, v212+; nvidia-smi on Windows since v217, the "using
  nvidia-smi for GPU monitoring" line): `llamaswap_gpu_memory_used_bytes`, `_memory_total_bytes`,
  `_util_percent`, `_memory_util_percent`, `_temperature_celsius`, `_power_draw_watts`,
  `_fan_speed_percent` (labels `id`, `name`), plus CPU, RAM and network. Source:
  `internal/perf/prometheus.go`, route `GET /metrics` in `internal/server/server.go`. The same data as
  JSON at `/api/performance`.
- **Not used**: `/api/metrics/activity` and `/api/metrics/stats` (per-request history of llama-swap's
  Activity page), `/api/events` (a server-sent event stream with model state changes, which would
  give load and swap timings but needs a connection kept open), `/api/hardware`. Load timings are not
  exported for now; the guard's log already shows a request that waited for a load.

### Turning on llama-server's numbers

In `C:\llama\config.yaml`, add `--metrics` to the model's `cmd` (any line of the `>` block, same
indent), for example after `--jinja`:

```yaml
         -fa on -ctk q8_0 -ctv q8_0
         --jinja --metrics
```

llama-swap reloads the config by itself only when started with `-watch-config`; otherwise restart it
(or run `infra/gpu-pc/logos-gpu-watch.ps1` again). The model restarts with it. Nothing changes in
Logos: the Models card shows "Speed: Not reported · add --metrics" until it is there. The GPU numbers
need nothing (llama-swap's own monitor).

### What Logos exports (`logos_model_…`)

Labels `server` (host:port of the model PC) and `model`:

| Metric | Meaning |
|---|---|
| `logos_model_server_up{server}` | 1 when the model server answered Logos's last look |
| `logos_model_loaded`, `logos_model_info{…,state,kv_cache_type,n_cpu_moe,flash_attention}` | ready (1) or starting; the settings as labels |
| `logos_model_load_window_tokens`, `logos_model_parallel_slots`, `logos_model_n_cpu_moe_layers` | window, slots, experts on the CPU (-1: all) |
| `logos_model_metrics_up` | 1 when llama-server answers `/metrics` (needs `--metrics`) |
| `logos_model_prompt_tokens_per_second`, `logos_model_generation_tokens_per_second` | llama-server's speed gauges (since its previous scrape) |
| `logos_model_server_requests_processing`, `..._deferred` | requests on the PC now, and waiting for a slot |
| `logos_model_prompt_tokens_processed_total`, `..._cached_total`, `logos_model_prompt_seconds_total` | prompt tokens read, reused from cache, and seconds reading |
| `logos_model_generated_tokens_total`, `logos_model_generation_seconds_total` | tokens written and seconds writing |
| `logos_model_decode_calls_total`, `logos_model_busy_slots_per_decode`, `logos_model_context_high_watermark_tokens` | decode calls, slots per call, largest request seen |
| `logos_model_spec_draft_tokens_total`, `..._accepted_tokens_total` | speculative decoding (0 when off) |
| `logos_model_kv_cache_usage_ratio`, `logos_model_kv_cache_tokens` | only from older llama.cpp builds |
| `logos_model_gpu_memory_used_bytes`, `..._total_bytes`, `logos_model_gpu_utilization_percent`, `logos_model_gpu_memory_utilization_percent`, `logos_model_gpu_temperature_celsius`, `logos_model_gpu_power_watts` `{server,gpu,name}` | from llama-swap |

From the model guard's log (`{"event": "chat"}` lines, counted since the dashboard started), label `who`
(`person`, `speaker`, `scheduled`):

| Metric | Meaning |
|---|---|
| `logos_model_request_seconds` (histogram) | a request's time from its turn to its last token |
| `logos_model_request_wait_seconds` (histogram) | its wait in the guard's queue |
| `logos_model_requests_total{who,outcome}` | `ok`, `loop_cut`, `model_off`, `dropped`, `error` |
| `logos_model_loop_cuts_total`, `logos_model_prompt_tokens_total`, `logos_model_completion_tokens_total` | loop cuts and tokens reported |
| `logos_model_guard_log_up` | 1 when Logos could read the guard's log |

Speed while working, without the idle time: `rate(logos_model_generated_tokens_total[5m]) /
rate(logos_model_generation_seconds_total[5m])`. Prompt cache hit rate (the speaker's warm-ups at work):
`rate(…_prompt_tokens_cached_total) / (rate(…_cached_total) + rate(…_processed_total))`.

### Grafana

Import `docs/grafana/logos-model.json` (Dashboards › New › Import) and pick the Prometheus that scrapes
Logos. Panels: model server on/loaded and its settings, GPU memory, temperature and use, tokens per
second (reading and writing), prompt cache hit rate, context and KV cache in use, the queue (on the PC
and in the guard), request seconds p50/p95 by who, the guard's wait p95, the house speaker's seconds
per question (`logos_voice_turn_seconds`, and the nightly check), tokens and loop cuts per hour.
