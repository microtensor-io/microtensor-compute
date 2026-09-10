# Compute validator guide

Run the pool's verifier: install, register, the deep pass step by step and every check it runs, weights, and signing agent releases.

The same page on the site: https://www.microtensor.cloud/docs/compute/guides/validator

---

### What the validator holds

Anything that can live on the validator's side does. The agent runs on hardware owned by someone with an incentive to cheat, so the validator holds every decision, every key and every piece of state that matters. Four jobs:

- **Decide what runs where.** Assignment, priority, eligibility.
- **Verify.** Send challenges, read scrapes, judge the answers.
- **Drive.** Open a session on a rig and run everything remotely over SSH.
- **Recover.** Clean up after failures, because a broken rig cannot fix itself.

The compute validator is its own program, `compute-validator`, with nothing shared with the arena validator.

### Requirements

| | |
|---|---|
| Host | Any Linux machine with a public address and outbound SSH; no GPU needed |
| Python | 3.10 or newer |
| Identity | A hotkey registered on netuid 92 with a validator permit, as a seed, a mnemonic or a bittensor wallet hotkey file |
| Access | Registered with the pool and activated by the operator |

### Install

The validator ships as an image with the compiled challenge inside, so nothing is built by hand:

```bash
mkdir -p /opt/compute-validator && cd /opt/compute-validator
curl -fsSLO https://raw.githubusercontent.com/microtensor-io/microtensor-compute/main/validator/deploy/docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/microtensor-io/microtensor-compute/main/validator/deploy/.env.example -o .env
```

Edit `.env`: point `CV_WALLET_HOTKEY_FILE` at the hotkey file on this host, set a label, and leave the rest at their defaults unless you know why. Then start it:

```bash
docker compose up -d
```

See [Configuration](https://www.microtensor.cloud/docs/compute/guides/operating#configuration) for every variable. Running from source is possible too (`pip install ".[validator]"` in a clone of microtensor-compute, then `make -C challenge validator`), but the miner build of the challenge library the validator uploads into sessions needs nvcc, so the image is the normal route.

### Register and get activated

```bash
docker compose exec validator compute-validator register
```

Registration is pending until the operator activates the hotkey. Every request you make to the pool is signed by the hotkey over the method, path, timestamp and body, so nothing else identifies you. Rig agents fetch the active validator list from the pool and refuse a session from any hotkey not on it.

### Run

```bash
docker compose logs -f validator
```

Every thirty seconds the express tick reads each online rig's version, ping and utilisation as signals. Every hour the validator asks the pool for rigs due a deep pass, takes at most four in flight and two per miner, and runs one pass per rig:

1. Reads the agent's version at `GET /version` and refuses anything below the pool minimum.
2. Generates a fresh SSH key and a 32 byte nonce, signs the key and nonce together with its hotkey, and opens a session with `POST /session/open`. The agent verifies the signature against the active validator list, appends the key to `authorized_keys` and returns the SSH details and an attestation bound to the nonce.
3. Connects over SSH, checks the host key against the grant, and uploads its checks into the agent's root directory: host reality, the encrypted NVML scrape, the compiled challenge with its throughput benchmark, and the inspector.
4. Runs them, reads what each prints, verifies the challenge answer in process, compares the scrape with the enrolment and the fleet, reconciles GPU processes against placements, and cleans up.
5. Closes the session with `POST /session/close`, which revokes the key.
6. Classifies the result and posts it to `POST /v1/compute/validators/rigs/{rig}/verifications` with the checks, the seed and the verified specification. The pool turns that into the rig's state, tier and eligibility.

Nothing here goes through the agent's API except the grant. The agent opens the door and is then irrelevant to what walks through it. See [The deep pass](validator_setup.md#the-deep-pass) for what each check does and how failures are classified.

### Weights

The pool computes an epoch score for every rig each hour and publishes the share per hotkey at `GET /v1/compute/validators/scores`. With `CV_SET_WEIGHTS=1` the validator submits those shares as weights on the configured chain endpoint every `CV_WEIGHTS_SECONDS`. Leave it off while you are testing.

```bash
compute-validator show scores
compute-validator show due
```

### Signing agent releases

The agent updater applies only an image digest signed by a compute validator. To publish one:

```bash
compute-validator sign-release sha256:<image digest>
```

The signature covers the digest and a timestamp, agents check the timestamp against a ten minute skew window, and the pool serves the latest release at `GET /v1/pool/agent-release`. This matters because the agent runs privileged with the docker socket mounted; an unauthenticated update channel would be a route to root on every rig in the pool.

### What the validator never delegates

Not one of these may be answered by the agent, because the miner controls the agent:

- whether a check passed
- what work a rig gets
- volume keys
- scoring, reliability, eligibility
- container lifecycle
- cleanup and recovery
- signing image updates
- whether hardware is real

The agent's honest jobs are opening the door, staying reachable, and keeping a log nobody scores. Everything else it reports is a hint about where to look.

## The deep pass

### Order of a pass

1. **Reach the agent.** `GET /version` over the agent API. Unreachable is `SSH_TRANSPORT`; below the pool minimum is `VERSION_STALE`.
2. **Open a session.** A fresh SSH key, a fresh nonce, the hotkey signature over both. A refusal is `SSH_TRANSPORT`.
3. **Connect over SSH** and compare the presented host key with the one in the grant.
4. **Upload the checks** into the agent's root directory and run them with the python the grant names: host reality, the scrape, the challenge. A script that dies is `AGENT_CRASH`.
5. **Close the session**, which revokes the key.
6. **Classify** and post the result with every check's value and the verified specification.

### Proving the hardware is real

**A challenge only a real GPU can answer.** The validator sends a seed and a round count; the check computes the answer on the rig and returns it with its timing. The validator recomputes the expected answer itself. Wrong answer fails, slow answer fails. The cutoff is a pool setting, generous enough for a real card and far too tight for a CPU pretending.

**A throughput benchmark in the same process.** This is what catches a proxied GPU. A card in another datacentre computes the right answer, but the round trip shows in the measured throughput. A CPU pretending is not close on either. The benchmark reports the device and its measured TFLOPS alongside the answer.

**Hardware read through NVML, never by parsing text.** `nvidia-smi` output is a string any shell script can produce. NVML fails in specific ways when hardware is absent, and each is a distinct condition:

```
NVML_ERROR_DRIVER_NOT_LOADED
NVML_ERROR_GPU_IS_LOST
NVML_ERROR_GPU_NOT_FOUND
NVML_ERROR_MEMORY
```

The scrape reads the vGPU structures and the MIG mode too, which is how a virtualised slice presented as a whole card gets caught, and lists every compute process on every card.

**Keep separate miner and validator builds of the same library.** The validator verifies in process without paying the benchmark cost, and the compiled challenge cannot be read to precompute answers or stub the benchmark. The scrape is uploaded fresh per job and encrypted with a per job key, because a miner who can read the script can write something that produces the output it expects.

### Host reality

Run in the same session, because a container presented as a machine is the second fraud after a GPU that is not there.

- `/proc/1/root` must be reachable and be the host root, not a container's
- `/proc/1/cgroup` must not show a nested path
- `systemd-detect-virt --container` must find nothing
- DMI product and vendor strings, which a container cannot present truthfully

The agent runs in the host process namespace precisely so the validator can reach the true host filesystem through the init process. A machine that cannot show it is not a host.

### The inspector

A second compiled library with a session handshake: `session_new`, `session_handshake_reply`, `inspector_collect`. The handshake binds collection to a live session, so a rig that answered honestly once cannot cache that answer and replay it.

### Exclusivity

Every compute process the scrape found on a card is reconciled against the jobs the pool placed on the rig. A process matching neither a container nor a registered job means unauthorised work, and the rig fails. The pool's job ledger is the reference, never the agent's own list.

### What the miner sees

Failures are classified, never binary, and the miner sees which class and the seed, so they can reproduce it. A bare "validation failed" is unactionable and generates a support conversation every time. Each pass records its checks with values:

| Check | Value shown |
|---|---|
| agent reachable | The agent version |
| host reality | `host`, or what virtualisation detection found |
| GPU model and VRAM | Cards as enrolled, or which are missing, changed or virtualised |
| cryptographic challenge | The time taken, and whether the answer was wrong |
| throughput | The measured figure and device, or why the benchmark could not run |
| exclusivity | No foreign processes, or the process ids found |

See [Failure classes](miner_setup.md#failure-classes) for each class, what it means and what to do about it.

### From result to state

A pass with a specification refreshes the rig's verified profile: its tier, its isolation status and its eligibility per kind of work. A rig that was validating or failed moves to probation; an active rig stays active. A fail moves any rig to failed, where it takes no work until it passes again. The pass repeats hourly, so the same checks that admit a rig also keep it honest.
