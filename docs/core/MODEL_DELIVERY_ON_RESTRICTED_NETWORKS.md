# Getting model weights onto a network-restricted host

Four deployments in, the single reliable predictor of how long a deploy takes
is not the GPU, the driver, or the image build. It is whether the host can
fetch ~12 GB of weights. This page records what to test, in what order, and
what each result means, so the next deploy spends ten minutes on it instead of
two hours.

## Test reachability BEFORE anything else

Run this first — before installing Docker, before transferring the project.
It costs a minute and decides whether the deploy is possible at all.

```bash
t(){ printf "%-24s " "$1"; timeout 45 curl -sL -o /dev/null -r 0-20000000 \
       -w "%{http_code} %{size_download}B %{speed_download}B/s\n" "$2" \
       2>/dev/null || echo "FAIL"; }

t "huggingface"  "https://huggingface.co/openai/whisper-large-v3/resolve/main/model.safetensors"
t "hf-mirror"    "https://hf-mirror.com/openai/whisper-large-v3/resolve/main/model.safetensors"
t "modelscope"   "https://modelscope.cn/api/v1/models/AI-ModelScope/whisper-large-v3/repo?Revision=master&FilePath=model.safetensors"
t "pypi"         "https://files.pythonhosted.org/packages/source/n/numpy/numpy-1.26.4.tar.gz"
t "docker hub"   "https://registry-1.docker.io/v2/"
```

**A ranged 20 MB GET is the only test that means anything.** Every smaller
check gives a false pass:

| Check | Why it lies |
| --- | --- |
| `ping` / DNS | Resolves fine on every blocked host observed |
| `curl` of `config.json` | Files under ~1 MB download normally on hosts where weights do not |
| `HEAD` on the weight file | Observed returning `HTTP 200` with a correct `content-length: 493869` on a host that then delivered **zero bytes** to `GET` |

That last row is the trap. Metadata succeeds, the transfer silently returns
nothing, and the symptom looks like a slow download rather than a block.

## Reading the result

| Result | Meaning |
| --- | --- |
| `200/206` at > 1 MB/s | Fine. Deploy normally. |
| `200/206` at < 100 KB/s | Throttled, not blocked. 12 GB will take many hours — treat as unusable. |
| `200` but `0B` downloaded | Blocked mid-transfer. No amount of retrying helps. |
| `403` | Endpoint refuses this network (observed on `cdn-lfs.hf.co`). |

## Observed profiles

Two hosts, both Iranian, with **opposite** blocking profiles — which is why
this must be measured per host rather than assumed:

| Source | Host A | Host B (RTX 4090) |
| --- | --- | --- |
| huggingface.co | blocked | blocked (`HEAD` 200, `GET` 0B) |
| hf-mirror.com | blocked | blocked |
| modelscope.cn | **6.5 MB/s** | **~1 MB/s** |
| Docker Hub | blocked (needed a mirror) | **~47 MB/s** |
| PyPI | ok | **9.2 MB/s** |
| NVIDIA repo | blocked | ok |
| GitHub releases | — | blocked |
| ghcr.io / quay.io | — | blocked |

Host A needed a Docker registry mirror; Host B pulls Docker images at 47 MB/s
without one. Both reach ModelScope, at very different speeds.

> **A truncated or transient test result will send you down the wrong path.**
> ModelScope was initially recorded as blocked on Host B on the strength of
> one test whose output scrolled off, plus a `got=0` line in a fetch log. A
> clean re-test returned `206`, 20 MB at ~1 MB/s. Before concluding a source
> is unreachable, run the ranged GET on its own and read the whole result --
> wrongly writing off a working mirror costs far more than re-testing, and in
> this case sent the deploy down a relay path measured at 0.01 MB/s that could
> never have finished.

## Sources worth testing beyond the obvious two

HF and ModelScope are not the only places these weights live, and on a host
where both are blocked another CDN may be wide open. Measured on Host B,
where huggingface.co delivered zero bytes:

| Source | Result | Carries |
| --- | --- | --- |
| `modelscope.cn` | ~1 MB/s | both models, HF layout — **the path this project uses** |
| `openaipublic.azureedge.net` | 5.2 MB/s | Whisper only, OpenAI `.pt` format |
| Docker Hub | ~47 MB/s | anything you can bake into an image |
| PyPI | 9.2 MB/s | anything packaged as a wheel |

The Azure one is worth knowing about because it is the upstream OpenAI
publishes Whisper to, and it is unrelated to the HF CDN that gets blocked.
The catch is format: it serves `large-v3.pt`, while the Triton compat server
loads HF `safetensors` through transformers. Using it means a conversion step
or a loader change, so it is a fallback rather than a drop-in -- but on a host
with no other route to Whisper it is the difference between working ASR and
none.

It carries no LLM, so it does not help the core model.

## Docker Hub carries the models, and is rarely filtered

The single most useful finding across four deployments. Every source tried
first -- HF, ModelScope, their mirrors -- is a *model host*, and the networks
that block one tend to block all of them. Docker Hub is not a model host, is
needed for the deploy anyway, and was pulling at ~47 MB/s on a host where
every model CDN returned nothing.

Docker publishes an official model catalog under the `ai/` namespace, and it
includes safetensors builds -- not just GGUF:

```bash
# list available formats and sizes
curl -s 'https://hub.docker.com/v2/repositories/ai/qwen3.5/tags?page_size=25' \
  | tr ',' '
' | grep -oE '"name":"[^"]*"|"full_size":[0-9]+' | paste - -

docker pull ai/qwen3.5:4b-safetensors    # 9.34 GB, the same weights HF serves
```

Tags follow `<size>-<format>`: `4b-safetensors`, `4b-q8_0`, `9b-bf16` and so
on. Take the safetensors tag -- vLLM loads it directly, whereas the GGUF and
MLX tags are for other runtimes.

These are OCI **artifacts**, not runnable images, so `docker export` yields
nothing useful: the layers are the model files themselves. Use `docker save`
and untar, then verify each shard parses before serving it.

> **Widen the kind of host, not the list of mirrors.** Hours went into
> re-probing HF and ModelScope variants -- different regions, CDN siblings,
> plain HTTP, TLS 1.2, forcing IPv4, the official `hf_hub_download` -- when
> all of them terminate at the same class of filtered CDN. The thing that
> worked was a completely different sort of host that happened to carry the
> same bytes. Ask what else ships this artifact, not which mirror of the same
> service might be open.

## Options, in order of preference

1. **Ask the provider to unblock `huggingface.co` bulk transfer.** By far the
   cheapest fix. Everything else on Host B worked first time — driver, CUDA,
   toolkit, GPU passthrough, image builds — and the deploy would have taken
   ~30 minutes with weights available.

2. **Point `HF_ENDPOINT` at a reachable mirror.** Plumbed through compose to
   every service that fetches weights. Redirects all hub calls without code
   changes. Requires finding a mirror that passes the 20 MB test above.

3. **Fetch from ModelScope** via `scripts/fetch_models_modelscope.sh`, when
   reachable. Stages into `LOCAL_MODELS_DIR`; `VLLM_MODEL` and
   `WHISPER_MODEL_DIR` both take a path as readily as a hub id.

4. **Pre-stage the weights into the image or a volume** on a machine that has
   access, and ship that. Viable because Docker Hub is often reachable where
   HF is not — the 47 MB/s figure above is a Docker layer pull.

5. **Relay through an operator machine.** Measure the *upload* leg first:
   observed 0.01 MB/s from a workstation to Host B, which is an ETA of over a
   year for 3 GB. The download leg being fast says nothing about the upload
   leg, and the upload leg is usually the worse one.

## What does not work

- **Retrying.** A blocked transfer fails identically every time; five retries
  cost five times as long to learn the same thing.
- **Assuming yesterday's workaround.** See the table above.
- **Trusting a partial download.** An aborted HF fetch leaves ~4.5 MB of cache
  metadata that passes an "is the model present" check, and the stack will
  start, report healthy, and fail only at first inference. Verify weights
  parse, never just that they exist — `fetch_models_modelscope.sh` does this
  and the reasoning is in `docs/core/ASR_TRANSLATION_CONFABULATION.md`.
