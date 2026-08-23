# Server Deployment Runbook

Every command used to take AranMed from a bare Ubuntu GPU server to a running
stack, with the reasoning behind each one. Written from an actual deployment
(RTX 2080 Ti, Ubuntu 22.04), including the failures worth knowing about — the
mistakes are the useful part, and re-deriving them costs hours.

**Reference hardware:** RTX 2080 Ti (11 GB, Turing/sm75), 32 cores, 88 GB RAM,
582 GB disk, Ubuntu 22.04, SSH on port 3031.

---

## 0. Before you start

Have ready:

- Server IP and SSH port (many hosts use a non-standard port — check the panel).
- Root access, ideally an SSH key rather than a password.
- Somewhere safe to store two secrets this process generates.

> **Losing the PHI encryption key means losing every encrypted patient
> record.** It is generated on the server and stored nowhere else. Back it up
> off the machine before the system sees real data.

---

## 1. Establish key-based SSH

Password auth cannot be automated safely (and non-interactive shells cannot
answer a password prompt at all), so install a key first. Run this **from your
own machine** — it prompts for the root password once:

```bash
ssh-copy-id -i ~/.ssh/id_ed25519.pub -p 3031 root@SERVER_IP
```

If `ssh-copy-id` is unavailable (common on Windows):

```bash
ssh -p 3031 root@SERVER_IP "mkdir -p ~/.ssh && chmod 700 ~/.ssh && \
  echo 'ssh-ed25519 AAAA...your-public-key... user' >> ~/.ssh/authorized_keys && \
  chmod 600 ~/.ssh/authorized_keys"
```

Verify, then rotate the root password — it has now travelled through at least
one channel you do not control:

```bash
ssh -p 3031 -o BatchMode=yes root@SERVER_IP "hostname; uptime"
ssh -p 3031 root@SERVER_IP "passwd"
```

> **If SSH warns that the host key changed**, stop. Host keys regenerate on OS
> reinstall — so it is expected after a rebuild and alarming otherwise.
> Confirm with your provider before clearing it with
> `ssh-keygen -R "[SERVER_IP]:3031"`.

---

## 2. Survey the machine

Check all three prerequisites before installing anything:

```bash
ssh -p 3031 root@SERVER_IP bash -s <<'EOF'
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
nproc; free -g | awk '/Mem:/{print "RAM: "$2"GB"}'
df -h / | awk 'NR==2{print "disk free: "$4}'
command -v docker || echo "docker: NOT INSTALLED"
EOF
```

### If `nvidia-smi` reports a driver/library mismatch

```
Failed to initialize NVML: Driver/library version mismatch
```

This means the loaded kernel module and the userspace libraries are different
versions — usually a driver package upgrade without a reboot. Diagnose:

```bash
cat /proc/driver/nvidia/version          # version actually loaded
modinfo nvidia | grep -E '^(filename|version)'   # version on disk
dkms status
fuser -v /dev/nvidia*                    # anything using the GPU?
```

If the on-disk module is newer and nothing is using the GPU, reload rather
than reboot:

```bash
modprobe -r nvidia_drm nvidia_modeset nvidia_uvm nvidia
nvidia-smi                               # reloads the correct module
```

---

## 3. Install Docker and the NVIDIA container toolkit

The stack is entirely containerised; the toolkit is what lets containers see
the GPU.

```bash
ssh -p 3031 root@SERVER_IP bash -s <<'EOF'
set -e
export DEBIAN_FRONTEND=noninteractive
curl -fsSL https://get.docker.com | sh

curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  > /etc/apt/sources.list.d/nvidia-container-toolkit.list
apt-get update -qq && apt-get install -y -qq nvidia-container-toolkit
nvidia-ctk runtime configure --runtime=docker
systemctl restart docker
EOF
```

**Verify the GPU is visible *inside* a container** — this is the check that
matters, not `nvidia-smi` on the host:

```bash
ssh -p 3031 root@SERVER_IP \
  "docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi"
```

---

## 4. Transfer the project

`git archive` ships exactly the tracked files — no `node_modules`, no corpus,
no local `.env`:

```bash
# on your machine
git archive --format=tar.gz -o aranmed-deploy.tar.gz HEAD
scp -P 3031 aranmed-deploy.tar.gz root@SERVER_IP:/root/

ssh -p 3031 root@SERVER_IP bash -s <<'EOF'
mkdir -p /opt/aranmed
tar -xzf /root/aranmed-deploy.tar.gz -C /opt/aranmed
cd /opt/aranmed
chmod +x deploy.sh deploy/nginx/10-aranmed-tls.sh deploy/vllm/entrypoint.sh \
         scripts/server_e2e_verify.sh
bash -n deploy.sh && echo "deploy.sh parses"
EOF
```

> `tar` does not preserve the executable bit from a Windows checkout, hence
> the explicit `chmod +x`.

### Deploying from Windows: line endings

`.gitattributes` pins `eol=lf` for everything Linux executes, so a bundle
built on Windows is safe. If you ever hand-copy a file instead, strip CR
first — a `.env` with CRLF makes `source .env` yield `VLLM_PORT=8000\r`, and
Compose then fails with `invalid hostPort: 8000` with the `\r` invisible:

```bash
sed -i 's/\r$//' .env
grep -rlU $'\r' --include='*.sh' . | xargs -r sed -i 's/\r$//'
```

---

## 5. Generate secrets and configure

**Generate on the server. Never copy a `.env` between machines** — it carries
another deployment's signing secret and PHI key.

```bash
ssh -p 3031 root@SERVER_IP bash -s <<'EOF'
cd /opt/aranmed
cp .env.example .env && sed -i 's/\r$//' .env

SECRET=$(python3 -c "import secrets;print(secrets.token_urlsafe(48))")
PHIKEY="k1:$(python3 -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())")"
ADMINPW=$(python3 -c "import secrets;print(secrets.token_urlsafe(18))")

cat >> .env <<CONF
ASR_AGENT_SECRET=$SECRET
PHI_ENCRYPTION_KEYS=$PHIKEY
ASR_AGENT_ADMIN_PASSWORD=$ADMINPW
CONF
chmod 600 .env
echo "ADMIN PASSWORD: $ADMINPW"
EOF
```

- `ASR_AGENT_SECRET` — **required** before running more than one backend
  replica. Without it each worker signs tokens with its own random key and
  sessions break across replicas.
- `PHI_ENCRYPTION_KEYS` — AES-256-GCM for patient data at rest. **Back it up.**

### Appending to `.env` creates duplicate keys

`.env.example` already defines many keys. Appending shadows them — later wins,
so behaviour is correct, but the file becomes ambiguous and `deploy.sh` warns.
Comment out the earlier definition:

```bash
for k in VLLM_MODEL VLLM_GPU_MEM_UTIL ASR_DEVICE MEDRAG_EMBED_DEVICE; do
  sed -i "s|^${k}=|# superseded: ${k}=|" .env
done
grep -oE '^[A-Za-z_][A-Za-z0-9_]*=' .env | sort | uniq -d   # must print nothing
```

---

## 6. Choose the model topology

Everything is env-driven; the same image serves any arrangement.

### Core LLM: vLLM instead of Ollama

```bash
OLLAMA_ENABLED=false
OLLAMA_DEFAULT=false
VLLM_CORE_ENABLED=true
VLLM_CORE_DEFAULT=true
VLLM_MODEL=Qwen/Qwen3.5-4B
VLLM_QUANTIZATION=bitsandbytes
VLLM_DTYPE=float16
VLLM_TOOL_CALL_PARSER=qwen3_xml
VLLM_MAX_NUM_SEQS=16
VLLM_GPU_MEM_UTIL=0.50
VLLM_MAX_MODEL_LEN=8192
```

Each of these exists for a reason learned the hard way:

| Setting | Why |
| --- | --- |
| `VLLM_DTYPE=float16` | Turing (sm75) has no bfloat16. vLLM falls back anyway, but stating it keeps the log honest. |
| `VLLM_TOOL_CALL_PARSER` | The agent always sends `tools`; vLLM returns **400** without `--enable-auto-tool-choice --tool-call-parser`. Parser is model-family specific: `qwen3_xml` (Qwen3/3.5), `hermes` (Qwen2.5), `llama3_json`, `mistral`. |
| `VLLM_MAX_NUM_SEQS` | Qwen3.5 is hybrid Mamba/attention and needs one Mamba cache block per decode sequence. The default 256 fails on a small card: *"max_num_seqs (256) exceeds available Mamba cache blocks (46)"*. |
| `VLLM_MAX_MODEL_LEN=8192` | Must exceed the report prompt (naming rules ~4.5 KB + template ~1.5 KB + transcript) plus output tokens. Too small returns **400 on reports while plain chat still works**. |
| `VLLM_QUANTIZATION` | Leave **empty** for unquantized or self-describing checkpoints (AWQ/GPTQ). A literal `none` is rejected by vLLM. |

### ASR: Whisper served by Triton

```bash
ASR_HF_ENABLED=false
ASR_HF_DEFAULT=false
ASR_TRITON_ENABLED=true
ASR_TRITON_DEFAULT=true
TRITON_WHISPER_DEVICE=cuda      # or cpu, to leave the GPU to the LLM
WHISPER_TASK=transcribe         # see below — the accuracy-critical one
```

This keeps Whisper out of the API process so ASR and report generation can
scale apart.

> **`WHISPER_TASK=transcribe` is not optional for clinical use.** When it is
> unset the task is derived from `WHISPER_OUTPUT_ENGLISH=1`, which yields
> `translate` — a *paraphrasing* decode. Measured on the same audio file:
>
> | `translate` (derived default) | `transcribe` (set explicitly) |
> | --- | --- |
> | "hepato-biliary **surgery on the uterus**" | "hepatobiliary **ultrasound**" |
> | "normal **parasympathetic echo**" | "normal **echogenicity**" |
> | "the **gallus and midi stent**" | "the **gallbladder**" |
>
> The damage compounds: the mangled transcript also drove template
> auto-selection to `abdominopelvic_for_male` instead of `hepatobiliary`.

### Report generation: turn thinking off

```bash
VLLM_REASONING_PARSER=qwen3
VLLM_EXTRA_BODY={"chat_template_kwargs":{"enable_thinking":false}}
```

A reasoning model asked to fill a template spends the whole token budget
inside its thinking block and returns an **empty report with no error**. The
parser keeps any remaining reasoning out of `content`; `extra_body` stops it
being generated for a task that wants structured output rather than
deliberation.

### MedicalRAG's answer LLM

```bash
DOCKER_MEDRAG_LLM_PROVIDER=vllm
DOCKER_MEDRAG_LLM_BASE_URL=http://vllm:8000/v1
DOCKER_MEDRAG_LLM_MODEL=Qwen/Qwen3.5-4B
```

Without the base URL the service falls back to `http://127.0.0.1:8000/v1`,
which inside the medrag container is nothing at all. The symptom is subtle:
`/api/health` reports `medrag.ok: true` while `medrag.llm.ok` is false.

### Chat routing

```bash
CHAT_TEXT_ROUTE=agent           # default
```

`agent` runs the tool-calling loop for text turns, reaching MedicalRAG
through the `medical_knowledge` tool — so one endpoint serves both "what
causes X" and "list the EHR records". The legacy `medrag` value short-circuits
every text turn to retrieval and returns, which makes all 17 agent tools
unreachable unless audio or an image is attached.

---

## 7. GPU budget — the constraint that decides everything

An 11 GB card must hold the core LLM *and* Whisper (~3.1 GB fp16) at once.
Measured:

| Model | GPU weights | Verdict on 11 GB |
| --- | --- | --- |
| `Qwen2.5-7B-Instruct-AWQ` | ~5.5 GB | Works at `0.62` utilisation |
| `Qwen2.5-7B-Instruct` (fp16) | ~15 GB | Exceeds the card outright |
| `Qwen3.5-4B` (fp16) | 9.32 GB | **OOMs even with the whole card** |
| `Qwen3.5-4B` + bitsandbytes | ~3–4 GB | Comfortable |
| `Intel/Qwen3.5-4B-int4-AutoRound` | ~5.9 GB | Works, but tight beside Whisper |

`Qwen3.5-4B` deserves a warning: the name suggests a small model, but it is
**vision-language** (`Qwen3_5ForConditionalGeneration`) and its fp16
checkpoint is 9.32 GB. Quantize it or use a bigger card.

> **Do not run the card to its limit.** A deployment at
> `VLLM_GPU_MEM_UTIL=0.68` plus Whisper on GPU (~10.8 GB of 11.26 GB)
> **hung the entire host** — no SSH, no ICMP — and needed a power cycle.
> Leave ~2 GB of headroom.

Common vLLM startup failures and what they mean:

| Message | Cause | Fix |
| --- | --- | --- |
| `No available memory for the cache blocks` | Utilisation covers weights but leaves nothing for KV cache | Raise `VLLM_GPU_MEM_UTIL` |
| `max_num_seqs (256) exceeds available Mamba cache blocks` | Hybrid model, small card | Lower `VLLM_MAX_NUM_SEQS` |
| `CUDA out of memory` during init | Weights exceed the budget | Quantize or use a smaller model |
| `Unknown quantization method: none` | Literal `none` passed | Leave `VLLM_QUANTIZATION` empty |

Turing also logs three harmless fallbacks: no bfloat16, no FlashAttention-2,
no FlashInfer sampler. All are automatic.

---

## 8. Deploy

```bash
ssh -p 3031 root@SERVER_IP \
  "cd /opt/aranmed && nohup ./deploy.sh --with-vllm --yes > /root/deploy.log 2>&1 &"
```

Expect 30–45 minutes on first run (image builds plus a ~2 GB torch download).
Watch it:

```bash
ssh -p 3031 root@SERVER_IP "tail -f /root/deploy.log"
```

Other forms:

```bash
./deploy.sh                    # core stack, host Ollama
./deploy.sh --with-ollama      # Ollama in-compose
./deploy.sh --tls              # HTTPS at the gateway
./deploy.sh --skip-models      # skip model prefetch
./deploy.sh --no-up            # build only
```

### Changing a model requires recreating the backend

The backend reads `VLLM_MODEL` at startup to build its registry. Changing the
model and restarting only vLLM leaves the backend asking for a model name
vLLM no longer serves — every request fails.

```bash
docker compose --profile vllm up -d --force-recreate vllm backend
docker compose restart gateway     # nginx caches upstream IPs at start
```

### `--force-recreate` does not rebuild the image

This costs more debugging time than anything else on this page. Backend
sources are `COPY`ed into the image, not mounted, so editing a file on the
server and recreating the container silently reruns the **old** code — the
change appears to have no effect, which sends you looking for a second bug
that does not exist. After editing anything under `backend/`:

```bash
docker compose build backend && docker compose up -d backend
```

Env-var changes are the exception: those are read at container start, so
recreate alone is enough. Templates under `deploy/` are bind-mounted and need
only a restart of the service that reads them.

---

## 9. Verify

```bash
ssh -p 3031 root@SERVER_IP "cd /opt/aranmed && ./scripts/server_e2e_verify.sh"
```

Covers health, templates, template auto-selection, ASR, full dictate,
report-from-text, the rule engine, EHR build/read/list, PHI encryption at
rest, medication alerts, dose recording, FHIR `$everything`, terminology
binding, audit trail, sessions, RBAC (a real student account must get 403 on
EHR), knowledge Q&A, and HL7 ORU. Checks whose dependencies are absent report
`SKIP` rather than failing.

Spot checks:

```bash
curl -s http://SERVER_IP:8090/api/health | python3 -m json.tool
curl -s -X POST http://SERVER_IP:8090/api/transcribe -F "file=@/root/sample.m4a"
curl -s -X POST http://SERVER_IP:8090/api/dictate -F "file=@/root/sample.m4a"
```

### Full dictate, end to end

```bash
curl -s -X POST http://SERVER_IP:8090/api/dictate -F "file=@/root/sample.m4a"
```

`/api/dictate` returns the **report**, not the transcript — transcript comes
from `/api/transcribe`. A healthy run selects a template, reports
`model: vllm-core`, and returns a report with **no `*`-prefixed lines**. Those
asterisks mark mutually exclusive alternative phrasings in the template; if
they survive into the output the model dumped the template instead of filling
it (observed: 1507 characters of contradictory findings, versus 484 correct
ones from the same dictation).

---

## 10. Load and security testing

Both scripts are env-driven and run from inside the backend container, which
already has `httpx`. `scripts/` is baked into the image rather than mounted,
so pipe the current copy in over stdin:

```bash
cd /opt/aranmed
docker compose cp .env backend:/tmp/.env
```

### Concurrency

```bash
docker compose exec -T \
  -e LOAD_N=100 -e LOAD_CONCURRENCY=50 \
  -e LOAD_BASE=http://gateway:8080 -e LOAD_ENV_FILE=/tmp/.env \
  backend python - < scripts/load_test.py
```

Measured on one RTX 2080 Ti with `VLLM_MAX_NUM_SEQS=8`:

| Run | Success | Throughput | p50 | p95 | max |
| --- | --- | --- | --- | --- | --- |
| 50 req @ 25 | 50/50 (100%) | 0.13 req/s | 134 s | 221 s | 240 s |
| 100 req @ 50 | 100/100 (100%) | 0.31 req/s | 91 s | 189 s | 236 s |

**No errors, no dropped connections, no degradation at either level.** Latency
is queueing, not failure: raising concurrency *improved* throughput because
vLLM batched more per step. Latency is the thing to scale — raise
`VLLM_MAX_NUM_SEQS` (and `VLLM_GPU_MEM_UTIL` to back it), or add GPUs. Note
each agent turn is several LLM round-trips, so these are full agent
iterations, not single completions.

### Security

```bash
docker compose exec -T \
  -e SEC_BASE=http://gateway:8080 -e SEC_ENV_FILE=/tmp/.env \
  backend python - < scripts/security_probe.py
```

16 adversarial checks; the exit code is the number of failures. Covers SQL
injection (10 auth-bypass payloads plus a time-based blind probe and a
`DROP TABLE` integrity check), token forgery (`alg:none`, stripped signature,
bit-flip, garbage), revocation, anonymous access to protected paths,
privilege escalation via a self-declared role, weak passwords, path
traversal, oversized bodies, security headers, error leakage, and login
brute-force throttling.

> **Run it last.** Check 16 deliberately trips the login limiter, which locks
> the admin account for `LOGIN_LOCKOUT_SEC` (default 900 s). Re-running inside
> that window aborts with exit code 2 rather than reporting phantom failures.

Two findings from the first run, both since fixed:

| Finding | Fix |
| --- | --- |
| `Server: nginx/1.27.5` disclosed the exact build | `server_tokens off` in the gateway template |
| A 5 MB `text` field returned **500** (unhandled) instead of a stated limit | `CHAT_MAX_TEXT_CHARS` (default 32000) → clean `413`. The gateway's `client_max_body_size` must stay large for audio, so it cannot bound a text field. |

---

## 11. Access from another machine

The gateway publishes `GATEWAY_PUBLISH_PORT` (default 8090):

- App: `http://SERVER_IP:8090`
- API: `http://SERVER_IP:8090/api/health`

Sign in as `admin` with the generated password. If the port is unreachable
from outside, open it — a provider firewall is the usual cause:

```bash
ufw allow 8090/tcp || iptables -I INPUT -p tcp --dport 8090 -j ACCEPT
```

> **Plain HTTP sends credentials and clinical text in the clear.** For any
> deployment reachable beyond a trusted network, enable TLS (§12).

---

## 12. Optional extras

### TLS at the gateway

```bash
mkdir -p deploy/nginx/certs && openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout deploy/nginx/certs/tls.key -out deploy/nginx/certs/tls.crt \
  -days 365 -subj "/CN=SERVER_IP"
./deploy.sh --tls
```

### PostgreSQL, when SQLite's single writer is the bottleneck

```bash
docker compose --profile postgres up -d postgres
# .env: DATABASE_URL=postgresql://aranmed:aranmed@postgres:5432/aranmed
docker compose up -d --force-recreate backend
```

### The knowledge corpus

MedicalRAG serves 0 chunks without it — the stack still reports healthy, so
this is easy to miss. `deploy.sh` warns explicitly. To attach it:

```bash
tar -czf - qdrant_storage | ssh -p 3031 root@SERVER_IP "tar -xzf - -C /opt/aranmed"
# .env: MEDRAG_QDRANT_STORAGE=./qdrant_storage
docker compose up -d --force-recreate qdrant medrag
```

---

## 13. Operations

```bash
docker compose ps                                   # status
docker compose logs -f backend vllm triton          # follow logs
docker compose restart gateway                      # after upstream changes
docker compose down                                 # stop
nvidia-smi                                          # GPU usage
docker inspect -f '{{.RestartCount}}' $(docker compose ps -q vllm)
```

**`RestartCount` is the fastest signal of a crash-loop.** A container that
reports "Up 15 seconds" while its neighbours report hours is restarting, not
starting.

### Updating

```bash
# on your machine
git archive --format=tar.gz -o aranmed-deploy.tar.gz HEAD
scp -P 3031 aranmed-deploy.tar.gz root@SERVER_IP:/root/

ssh -p 3031 root@SERVER_IP bash -s <<'EOF'
cd /opt/aranmed
cp .env /root/.env.bak                    # tar would overwrite it
tar -xzf /root/aranmed-deploy.tar.gz -C /opt/aranmed
cp /root/.env.bak .env
chmod +x deploy.sh deploy/nginx/10-aranmed-tls.sh deploy/vllm/entrypoint.sh
docker compose --profile vllm up -d --force-recreate backend
docker compose restart gateway
EOF
```

---

## 14. Post-deployment checklist

- [ ] **Back up `PHI_ENCRYPTION_KEYS`** somewhere other than the server.
- [ ] Record the admin password, then change it after first sign-in.
- [ ] Rotate the root password if it was ever sent over chat or a ticket.
- [ ] Enable TLS if reachable beyond a trusted network.
- [ ] Attach the knowledge corpus, or accept that knowledge Q&A is unavailable.
- [ ] Confirm `nvidia-smi` shows ~2 GB headroom under load.
- [ ] Run `server_e2e_verify.sh` and keep the output as a baseline.
- [ ] Run `load_test.py` at your expected peak concurrency; confirm 100%
      success and decide whether p95 latency is acceptable.
- [ ] Run `security_probe.py` **last** (it locks the admin account for
      `LOGIN_LOCKOUT_SEC`); require 0 failures.
- [ ] Confirm `/api/dictate` returns a report with **no `*` lines** and that
      `/api/transcribe` output is literal, not paraphrased.
- [ ] Confirm `/api/health` shows `medrag.llm.ok: true`, not just `medrag.ok`.
