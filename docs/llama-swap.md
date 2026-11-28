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
