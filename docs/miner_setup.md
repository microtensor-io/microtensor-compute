# Rig owner guide

Put a GPU machine into the pool: requirements, the accepted hardware and its tiers, install, claim, the fee, the deep pass, and what each failure class means.

The same page on the site: https://www.microtensor.cloud/docs/compute/guides/rig-owner

---

### Before you start

Read the requirements once. A machine failing any of them is not enrolled, and a machine that later stops meeting one drops out of rotation at the next deep pass.

| Requirement | Value | Why |
|---|---|---|
| Operating system | Ubuntu 22.04 or 24.04 | Everything is tested against these |
| Kernel | 5.19 minimum, 6.x preferred | Container isolation over layered filesystems needs identity mapped mounts, added in 5.19 |
| Architecture | x86_64 | Required by the isolation runtime |
| GPU | On the [accepted list](miner_setup.md#accepted-hardware-and-tiers), 8 GB or more, identified by UUID | The pool only characterises cards it can verify |
| Driver | At or above the announced minimum | The driver runs privileged; a vulnerability there reaches every tenant |
| Storage | 180 GB solid state minimum, more for rental and shaping | Below this the model cache, images and logs contend |
| Storage quotas | Enforceable by the docker storage driver | A tenant that can fill the host disk takes down every other tenant |
| Runtime | docker-ce, containerd, the NVIDIA container toolkit, sysbox-ce 0.6.6 for rental | Sysbox is what makes a stranger's code safe on your machine |
| Network | Public IPv4 with no carrier grade NAT, SSH reachable from validators, a reserved port range for tenants | The validator must reach the machine directly |
| Exclusivity | Nothing else running on the cards | Anything the pool did not place is unauthorised |

The GPU is what the pool rents and serves with. Small models are memory bandwidth bound, so an older card with good bandwidth serves them well and cheaply; you do not need a training card to qualify for inference.

### 1. Sign in and verify

Open [Compute Pool](https://www.microtensor.cloud/compute-pool) on the site. Connect a Bittensor wallet extension, pick the hotkey, and sign the nonce. Then connect Discord once. The pool binds that Discord account to the hotkey permanently and checks that you are in the microtensor server. Claims are refused until both are done.

> **Note** The hotkey you claim with is the hotkey that owns the rig and receives its earnings. Use the one you intend to keep.

### 2. Install the agent

On the rig, as root:

```bash
curl -fsSL https://raw.githubusercontent.com/microtensor-io/microtensor-compute/main/agent/deploy/install.sh | sudo bash
```

The installer sets nothing up for you. It checks the machine against every requirement above (operating system, kernel, driver, docker with the compose plugin, the NVIDIA runtime, sysbox 0.6.6 with ID mapped mounts, the storage quota probe) and refuses with the fix for each missing item. When the host passes, it pins the agent image to the digest the validators signed, writes `/opt/rig-agent/docker-compose.yml` and `.env`, and starts three containers:

| Container | What it does |
|---|---|
| `agent` | The agent itself, privileged with the host pid namespace, the docker socket, the agent API on port 8800 and SSH on port 2200 |
| `monitor` | The same image with a different command, reading `/dev/kmsg` for kernel level GPU faults so a wedged agent cannot take monitoring down with it |
| `autoheal` | Restarts a container marked unhealthy |

The agent enrols with the pool and the installer prints the registration code:

```
==============================================================
  REGISTRATION CODE   k7m2p9qx
  Enter it on the portal under Compute Pool > Add rig.
==============================================================
```

Read it again at any time with `sudo docker compose exec agent rig-agent status` in `/opt/rig-agent` (the install directory belongs to root).

### 3. Claim the rig

On the portal open Add rig, paste the code and press Add rig. The pool tells the agent which hotkey is claiming the machine, and the agent asks at the rig's terminal. Approve from the rig:

```bash
cd /opt/rig-agent && sudo docker compose exec agent rig-agent approve <your hotkey>
```

Deny with `--deny`. The prompt expires after ten minutes; enter the code again if it does. Only the person at the keyboard can approve, which is what makes a leaked code useless.

### 4. Pay the commitment

The portal quotes the fee from the cards the agent enrolled:

```
fee = base per GPU × tier multiplier × number of GPUs
```

| Tier | Memory | Multiplier |
|---|---|---|
| Entry | 8 to 16 GB | 1× |
| Standard | 20 to 24 GB | 2× |
| Professional | 32 to 48 GB | 3× |
| Flagship | 80 GB and above | 4× |

Send the exact amount to the pool coldkey with the memo shown, paste the transfer reference, and report it. The operator confirms the payment and the rig enters validation. The fee is non refundable and is forfeited if the rig is removed for misbehaviour.

### 5. The first deep pass

Within the hour a compute validator opens a session on the rig and runs its checks. You will see the result on the Validation page, with each check and its value:

| Check | What passes |
|---|---|
| Host reality | The agent runs on a real host, not inside a container |
| GPU model and VRAM | Every enrolled card is present, reported memory sits inside the model's window, no virtualised or MIG slice |
| Cryptographic challenge | The right answer, fast enough |
| Throughput | The measured figure is consistent with the declared card |
| Exclusivity | No GPU process the pool did not place |

A pass reads your rig's profile: its tier and, per kind of work, whether it qualifies and why not. A fail names one of the [failure classes](miner_setup.md#failure-classes) and the seed, so you can reproduce it.

### 6. Probation, work and pay

After the first pass the rig takes low value work while reliability builds, and reaches full standing after fourteen days of uptime with its checks passing. Work arrives by priority: rental, inference, model shaping, then mining. The Rigs page shows jobs running and earnings per rig; earnings are eighty percent of what each job paid.

### Declining rental

Rental means a stranger's code runs on your hardware, inside a sysbox container with its own user namespace. If you would rather not, turn rental off for the rig on the portal. It costs you the highest value work in the ladder and a fraction of the emission floor, and nothing else changes.

### Keeping the rig healthy

- Keep it online. The websocket ping every twenty seconds is the liveness signal; a drop longer than a minute marks the rig offline and resets its uptime ramp.
- Keep the driver above the announced minimum. A cutoff is announced with a grace period, and a rig mid rental is exempt until the tenant leaves.
- Let the agent update itself. It applies only image digests signed by a compute validator, fetched every five minutes.
- Read `sudo docker compose logs agent` and the event log at `/var/lib/rig-agent-logs/events.jsonl` when something looks wrong. The log is append only and keeps its freshest half when it fills.

### Removing a rig

A removed rig drains first, so a customer is never interrupted, and its commitment is forfeited. Enrolling the same card again later means paying the fee again at the current rate.

## Accepted hardware and tiers

### The four tiers

Tiers are set by memory rather than by generation, because memory is what determines whether a workload fits at all. Generation shows up separately in throughput, and therefore in demand weight and in what a machine actually earns.

| Tier | Memory | Fee multiplier | Representative cards |
|---|---|---|---|
| Flagship | 80 GB and above | 4× | B200, H200, H100, A100 80 GB, A800, RTX PRO 6000 |
| Professional | 32 to 48 GB | 3× | L40S, L40, RTX 6000 Ada, A6000, RTX PRO 5000, Quadro RTX 8000, RTX 5090 |
| Standard | 20 to 24 GB | 2× | RTX 4090, 3090, A5000, L4, A10, RTX 4500 Ada, RTX PRO 4000, A4500 |
| Entry | 8 to 16 GB | 1× | RTX 4080, 4070, 3080, A4000, T4, V100, RTX 5080 |

A rig's tier is the lowest tier among its accepted cards, and a card under 8 GB is not accepted for work.

### Memory windows

Windows are per variant, not per model. A card sold in two memory configurations gets two entries. Without that, a machine could claim to be the larger variant of a card it does not have, or a smaller data centre part could claim to be a much larger one.

Windows are tolerant at the edges, because reported memory sits below the marketing figure and drifts between driver versions. A card advertised at 24 GB commonly reports around 23, and a 48 GB card reports either 46 or 49 depending on the batch. A window of roughly 95 to 105 percent absorbs that without opening a gap wide enough to admit the next size up. Eligibility minimums are checked against the nominal figure of the matched variant, so a 4090 reporting 23.99 GB still counts as a 24 GB card.

### Accepted models

**Data centre, current generation.** B200, H200 and H200 NVL, H100 in its 80 GB, NVL and PCIe forms.

**Data centre, previous generations.** A100 in 80 GB PCIe and SXM4 forms, A800 80 GB, A10, L40S, L40, L4, V100 in 16 and 32 GB, T4.

**Professional and workstation.** RTX PRO 6000 Blackwell in server and workstation editions, RTX PRO 5000, 4500 and 4000 Blackwell, RTX PRO 2000 Blackwell, RTX 6000 Ada, RTX 5880 Ada, RTX 5000 Ada, RTX 4500 Ada, RTX A6000, A5000, A4500, A4000, A2000, Quadro RTX 8000, 6000 and 5000, TITAN V and TITAN RTX.

**Consumer, current.** RTX 5090, 5080, 5070 Ti, 5070, 5060 Ti, 5060.

**Consumer, previous.** RTX 4090 and 4090 D, 4080 SUPER, 4080, 4070 Ti SUPER, 4070 Ti, 4070 SUPER, 4070, 4060 Ti, 4060. RTX 3090 Ti, 3090, 3080 Ti, 3080, 3070 Ti, 3070, 3060 Ti, 3060, 3050. RTX 2080 Ti, 2080 SUPER, 2070 SUPER, 2060 SUPER, 2060. GTX 1660 Ti and 1660 SUPER.

The list is additive. A model absent from it is not refused on principle, it is simply not yet characterised, and adding one requires observing its real reported memory across enough machines to set the window honestly. The current table with its windows is served at `GET /v1/pool/hardware`.

### System minimums, regardless of tier

| Requirement | Value | Why |
|---|---|---|
| Operating system | Ubuntu 22.04 or 24.04 | Everything is tested against these |
| Kernel | 5.19 minimum, 6.x preferred | Container isolation over layered filesystems needs identity mapped mounts, added in 5.19; without them the GPU hook fails outright |
| Architecture | x86_64 | Required by the isolation runtime |
| Storage | 180 GB minimum, solid state | Below this the model cache, container images and logs contend |
| Storage quotas | Enforceable by the storage driver | A tenant that can fill the host disk takes down every other tenant |
| Network | Public IPv4, no carrier grade translation | The validator must reach the machine directly |
| Driver | At or above the announced minimum | The driver runs privileged; a vulnerability reaches every tenant on the host |
| Exclusivity | No other workload on the card | Anything not placed by the pool is unauthorised |

A machine failing any of these is not enrolled. A machine that later stops meeting one drops out of rotation at the next deep pass.

### What a contributor sees

The specification check runs before any work is offered, and its result is readable rather than a verdict. A machine that qualifies for inference and not for rental is told so, with the reason, so the contributor can decide whether upgrading is worth it:

```
rig a4f2c1 · 2 × RTX 4090 · standard tier
  inference        eligible
  model shaping    eligible
  rental           not eligible
                   container isolation not active
                   storage driver cannot enforce per container quotas
  reliability      0.42 building, 6 days of 14
  commitment       paid, 2 × standard
```

That is the whole contract with a contributor: here is what your hardware qualifies for, here is why it does not qualify for the rest, and here is what you would need to change.

## Failure classes

### The classes

| Class | What the validator saw | What to do |
|---|---|---|
| `SSH_TRANSPORT` | The agent could not be reached, refused the session, or the SSH connection failed | Check the public address, that ports 8800 and 2200 reach the agent, and that the rig is online in the portal |
| `AGENT_CRASH` | The agent died during the check, or an uploaded check exited without a result | Read `sudo docker compose logs agent` and the event log; restart the agent |
| `CHALLENGE_REJECT` | The challenge answer was wrong, or took longer than the cutoff | The card is not what it claims, is proxied, or is heavily contended. Free the card and let the next pass run |
| `SPEC_MISMATCH` | The hardware is not what was declared: a missing card, a changed model, a virtualised or MIG slice, or NVML failing | Enrol the rig again with the cards it really has; do not slice cards; make sure the driver loads |
| `UNAUTHORISED_WORK` | A GPU process that matches neither a container nor a job the pool placed | Stop whatever else runs on the cards. Anything the pool did not place is unauthorised |
| `NESTED_CONTAINER` | The agent is not on a real host: a nested cgroup, a container view of init, or virtualisation detection found something | Run the agent on the host as the installer sets it up, with the host pid namespace |
| `DUPLICATE_UUID` | The card is already enrolled elsewhere in the pool, under this account or another | A card enrols once anywhere. Remove it from the other rig first |
| `VERSION_STALE` | The agent is below the pool's minimum version | Let the signed updater run, or pull the current image |

### Where each is raised

- At **enrolment**, before any work: `DUPLICATE_UUID` is refused with a 409, and `VERSION_STALE` with a 426, both from the pool server when the agent registers.
- At **every deep pass**, hourly: all eight. A rig can be honest at enrolment and moved into a container the next day, which is why the pass repeats all of it.

### What a failure does

A failed pass moves the rig to the failed state. It stops receiving work, its emission base is zero for the epoch, and it stays there until a later pass succeeds, at which point it returns to probation with its profile refreshed. Nothing is forfeited by a failed pass on its own. Removal, which forfeits the commitment, is an operator decision reserved for misbehaviour.

### Reading a failure on the portal

The Validation page lists every rig with its stage, its current result and the failure reason, and the details sheet shows the validator that ran the pass, when it ran, the failure class, the notes, and every check with its value. The seed is recorded with the pass, and the reason line quotes what the validator saw, for example which process ids were found on a card or which cards were missing.

### Honest failures on the pool's side

A job that failed because of the pool pays the rig in full, because the machine held capacity it was asked to hold. A deep pass that could not run because a validator was down does not fail the rig; it is simply late, and the rig stays in its current state until a validator reaches it.
