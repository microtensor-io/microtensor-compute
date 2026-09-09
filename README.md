# microtensor-compute

Two deployables from one repo: the rig agent that runs on a contributor's GPU machine, and the compute validator that runs on our side and drives rigs over SSH. They share the wire types in `protocol/` and nothing else. The pool server in microtensor-server is the third plane: it holds every durable record and passes messages between the two.

```
protocol/    types both sides speak, no logic
agent/       the rig agent: opens the door, stays reachable, keeps a log
validator/   the compute validator: decides, verifies, drives, recovers
challenge/   the compiled GPU challenge, miner build and validator build
scripts/     import scan and the challenge reference vectors used by CI
```

## The split

The agent is untrusted. It runs on hardware owned by someone with an incentive to cheat, so it verifies nothing, chooses no work and judges nothing. It grants an SSH session per request, holds a websocket to the pool server, reports cheap liveness, prefetches images, watches the kernel log and appends to a size capped JSONL file. Nothing under `agent/` decides anything: `hardware/` collects, `api/` serves, `control/` transports, `store/` records.

The validator holds every decision, every key and every piece of state that matters. It opens a session on a rig, uploads its own code, runs it over the shell and reads the result directly. `validator/verify/` is one file per check and every check returns a classification from `protocol/failures.py`, never a boolean. `validator/scoring/formula.py` is the one function with the score in it.

## Install a rig

```bash
curl -fsSL https://raw.githubusercontent.com/microtensor-io/microtensor-compute/main/agent/deploy/install.sh | sudo bash
```

The installer checks the host (Ubuntu 22.04 or 24.04, kernel 5.19 or newer, x86_64, a public IPv4), installs docker, the NVIDIA container toolkit and sysbox, pins the agent image to the digest the validators signed, and starts the three containers. The agent prints a registration code; enter it on the portal under Compute Pool, then approve the claiming hotkey at the terminal:

```bash
docker compose -f /opt/rig-agent/docker-compose.yml logs agent
docker compose -f /opt/rig-agent/docker-compose.yml exec agent rig-agent approve <hotkey>
```

## Run a validator

```bash
pip install ".[validator]"
make -C challenge validator
export CV_HOTKEY_SEED=0x...     # or CV_HOTKEY_MNEMONIC, or CV_WALLET_HOTKEY_FILE
compute-validator register
compute-validator run
```

`validator/deploy/` carries a Dockerfile and compose file. The validator registers with the pool server and waits for the operator to activate it. Every setting is a `CV_*` variable; every agent setting is a `RIG_*` variable. `agent/deploy/.env.example` and `validator/deploy/.env.example` list them.

## The challenge

`challenge/` is built separately with two targets. `make miner` needs nvcc and produces the library the validator uploads into sessions; `make validator` needs only a C compiler and produces the in process verifier. `make check` proves the C reference and the Python reference in `scripts/challenge_vector.py` agree, which CI runs on every push.

## Checks

CI runs ruff, compiles every module, imports every module and checks every local import resolves (`scripts/import_scan.py`), and builds and verifies the challenge reference. There are no test files in this repo by design.
