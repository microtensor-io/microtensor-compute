# microtensor-compute

Two deployables from one repo: the rig agent that runs on a contributor's GPU machine, and the compute validator that runs on our side and drives rigs over SSH. They share the wire types in `protocol/` and nothing else. The pool server in microtensor-server is the third plane: it holds every durable record and passes messages between the two.

```text
protocol/    types both sides speak, no logic
agent/       the rig agent: opens the door, stays reachable, keeps a log
validator/   the compute validator: decides, verifies, drives, recovers
challenge/   the compiled GPU challenge, miner build and validator build
scripts/     import scan and the challenge reference vectors used by CI
```

## The split

The agent is untrusted. It runs on hardware owned by someone with an incentive to cheat, so it verifies nothing, chooses no work and judges nothing. It grants an SSH session per request, holds a websocket to the pool server, reports cheap liveness, prefetches images, watches the kernel log and appends to a size capped JSONL file. Nothing under `agent/` decides anything: `hardware/` collects, `api/` serves, `control/` transports, `store/` records.

The validator holds every decision, every key and every piece of state that matters. It opens a session on a rig, uploads its own code, runs it over the shell and reads the result directly. `validator/verify/` is one file per check and every check returns a classification from `protocol/failures.py`, never a boolean. `validator/scoring/formula.py` is the one function with the score in it.

## Images

Every push to main builds the challenge library with nvcc and publishes two images to GitHub Container Registry with the library inside:

- `ghcr.io/microtensor-io/rig-agent` carries the miner build at `/usr/lib/libmtchallenge.so` and the agent.
- `ghcr.io/microtensor-io/compute-validator` carries the CPU verifier at `/usr/lib/libmtverify.so`, the same miner build for uploading into sessions, and the validator.

Nobody builds the challenge by hand. A validator generates a fresh seed and cipher for every deep pass, uploads its own copy of the library into the session, runs it there, and verifies the answer in process.

## Install a rig

```bash
curl -fsSL https://raw.githubusercontent.com/microtensor-io/microtensor-compute/main/agent/deploy/install.sh | sudo bash
```

The installer sets nothing up for the miner. It checks the host against the requirements (Ubuntu 22.04 or 24.04, kernel 5.19 or newer, x86_64, the NVIDIA driver, docker with the compose plugin, the NVIDIA runtime, sysbox 0.6.6 with ID mapped mounts, storage quotas) and refuses with the fix for each missing item. When the host passes it pins the agent image to the digest the validators signed and starts the three containers. The agent prints a registration code; enter it on the portal under Compute Pool, then approve the claiming hotkey at the terminal:

```bash
docker compose -f /opt/rig-agent/docker-compose.yml logs agent
docker compose -f /opt/rig-agent/docker-compose.yml exec agent rig-agent approve <hotkey>
```

## Run a validator

```bash
mkdir -p /opt/compute-validator && cd /opt/compute-validator
curl -fsSLO https://raw.githubusercontent.com/microtensor-io/microtensor-compute/main/validator/deploy/docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/microtensor-io/microtensor-compute/main/validator/deploy/.env.example -o .env
```

Edit `.env`: point `CV_WALLET_HOTKEY_FILE` at the hotkey file on this host and set a label. Then:

```bash
docker compose up -d
docker compose exec validator compute-validator register
docker compose logs -f validator
```

The validator registers with the pool server and waits for the operator to activate it. From then on it runs the express tick every thirty seconds and the deep pass every hour on its own. Every setting is a `CV_*` variable; every agent setting is a `RIG_*` variable. `agent/deploy/.env.example` and `validator/deploy/.env.example` list them.

Running from source instead of the image works too: `pip install ".[validator]"`, `make -C challenge validator` for the verifier, and point `CV_AGENT_LIBRARY` at a miner build taken from a CI artifact or built with `make -C challenge miner` on a machine with nvcc.

## The challenge

`challenge/` is built with two targets. `make miner` needs nvcc and produces the library the validator uploads into sessions; `make validator` needs only a C compiler and produces the in process verifier. `make check` proves the C reference and the Python reference in `scripts/challenge_vector.py` agree, which CI runs on every push.

## Checks

CI runs ruff, compiles every module, imports every module and checks every local import resolves (`scripts/import_scan.py`), and builds and verifies the challenge reference. There are no test files in this repo by design.
