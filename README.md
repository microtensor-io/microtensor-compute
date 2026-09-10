<div align="center">

# Microtensor Compute

### A Verified GPU Pool for Bittensor

Bittensor subnet 92, compute layer

[Miner](docs/miner_setup.md) •
[Validator](docs/validator_setup.md) •
[Incentive mechanism](docs/incentive_mechanism.md) •
[How it works](https://www.microtensor.cloud/docs/compute/reference/how-it-works) •
[API](https://www.microtensor.cloud/docs/compute/reference/api) •
[Dashboard](https://www.microtensor.cloud/compute-pool)

</div>

---

## Introduction

The arena layer of Microtensor certifies inference systems: small composed
models measured on reference hardware, with the figures produced by the party
doing the measuring rather than the party being measured. A certificate is only
worth something if the system can be run. The compute layer is where it runs.

Contributors add GPU machines, called rigs, to a pool that serves four kinds of
demand from one fleet: inference on certified systems, model shaping (fine
tuning and evaluation), rental of raw capacity, and mining on other subnets for
whatever is left over. A rig moves between these states rather than being
assigned to one. Supply is built ahead of demand, because a network with no
capacity has nothing to sell, and emissions pay the floor that makes a machine
worth running on a quiet day.

Every rig is owned by someone with an incentive to cheat. The whole design
follows from taking that seriously.

---

## How it works

```
  ┌────────────────────┐        websocket         ┌────────────────────┐
  │      rig agent     │◀────────────────────────▶│     pool server    │
  │  (miner hardware)  │   commands, state, log   │  accounts, ledger, │
  │                    │                          │  claims, scores    │
  │  opens the door,   │                          └─────────┬──────────┘
  │  stays reachable,  │                                    │ signed HTTP
  │  keeps a log       │        SSH session                 ▼
  │                    │◀────────────────────────┌────────────────────┐
  └────────────────────┘   code the validator    │  compute validator │
                           uploaded and ran      │  decides, verifies,│
                                                 │  drives, recovers  │
                                                 └────────────────────┘
```

Three planes, and the split between them is the security model.

1. **The rig agent** runs on the contributor's machine. It verifies nothing,
   chooses no work and judges nothing. It grants an SSH session per request,
   holds a websocket to the pool server, reports cheap liveness, prefetches
   images, watches the kernel log for GPU faults and appends to a size capped
   event log. Anything it says about itself is a claim, not evidence.

2. **The compute validator** runs on our side and holds every decision, key
   and piece of state that matters. It posts a public key, a nonce and a
   signature over both; the agent installs the key for one session. The
   validator then uploads its own code over that session, runs it, and reads
   the result directly. The agent is not in the path and cannot alter the
   outcome.

3. **The pool server** is the third plane. It holds what neither of the others
   can: accounts and Discord bindings, the rig ledger, claims, the assignment
   record, verification history, reliability accumulated over weeks, scores,
   and the signed agent releases. Agents and validators never talk to each
   other except through a session the validator opened.

### Verification

Every deep pass runs the same checks from code the validator uploaded:

| Check | What it proves |
|---|---|
| Compiled challenge | A real GPU solved a fresh seed and cipher, and its measured throughput matches the declared card |
| NVML scrape | The hardware is what was enrolled: model, memory window, UUIDs, no MIG or vGPU slice presented as a card |
| Host reality | The agent is on a real host: init's root, the control group path, virtualisation detection, DMI strings |
| Duplicates | No card is enrolled anywhere else in the pool |
| Exclusivity | Every process on the cards matches a container the validator created or a job the server recorded |
| Version | The agent is at or above the pool minimum |

Failures are classified, never binary: `SSH_TRANSPORT`, `AGENT_CRASH`,
`CHALLENGE_REJECT`, `SPEC_MISMATCH`, `UNAUTHORISED_WORK`, `NESTED_CONTAINER`,
`DUPLICATE_UUID`, `VERSION_STALE`. The contributor sees which one, and the seed,
so they can reproduce it.

### Monitoring

Three tiers that differ in what they trust as much as in what they cost. The
websocket is continuous and trusts nothing; a drop is known immediately. The
express tick, every thirty seconds, reads what the agent says about itself and
treats it as a signal about where to look. The deep pass, hourly, opens a
session and runs the checks above. Only the deep pass decides anything.

### Scheduling

When hardware is contended: rental, then inference, then model shaping, then
mining. Eligibility is capability rather than preference: each kind of work has
minimums and a rig gets what it qualifies for. Rental is the one opt out, since
it means a stranger's code on the contributor's hardware, and declining it costs
something in the blended rate. Sensitivities are never mixed on one card, and a
rig is drained before it is removed.

---

## Incentive mechanism

Two streams. Emissions pay the floor; revenue share is the upside, paid on work
actually done. Per rig, per epoch:

```
score     =  base · w_d · s_c · m_iso · m_up · m_drv · m_opt
emission  =  pool · score / Σ score
```

| Term | Meaning |
|---|---|
| `base` | The deep pass verdict. A machine that failed verification scores zero and earns nothing from either stream |
| `w_d` | Demand weight: an exponential moving average of revenue per GPU type, so cards the market pays for earn more |
| `s_c` | Capacity share: the rig's GPUs over the fleet's, so twenty cards earn twenty times one |
| `m_iso` | Isolation: a machine without working container isolation is still useful, so this is a cost rather than a gate |
| `m_up` | Uptime: ramps linearly to one over fourteen days, probation expressed as money |
| `m_drv` | Driver: a hard gate, phased, with an announced grace period; a rig mid rental is exempt |
| `m_opt` | Rental opt out: pricing the choice to decline the most valuable work |

`pool` is sixty percent of compute emission. The remaining forty percent is held
before anything is paid, funding burn, the validator share and a reserve.

Revenue splits eighty percent to the machine and twenty to the network.
Inference pays per request, reserved work pays per hour held, mining passes
through what the target subnet paid. A job that failed on the rig's side pays
nothing; one that failed on ours pays in full.

Joining costs a one off commitment per GPU, tiered by memory and staked to the
subnet, so fabricated identities cost more than they earn:

| Tier | Memory | Fee |
|---|---|---|
| Flagship | 80 GB and above | 4× |
| Professional | 32 to 48 GB | 3× |
| Standard | 20 to 24 GB | 2× |
| Entry | 8 to 16 GB | 1× |

Every input to the score is a validator observation or a number from the
assignment ledger. Nothing a rig reports about itself is a term.

---

## Why verification is the product

**The validator observes; it is never told.** If a rig could gain by lying about
something, the validator measures it in a session it opened, running code it
uploaded. The agent's honest jobs are opening the door, staying reachable and
keeping a log nobody scores.

**A proxied GPU shows in the numbers.** The compiled challenge solves an integer
problem only a real card answers quickly, then benchmarks throughput in the same
process with a round trip per iteration, so a card in another datacentre
computes the right answer and fails on time.

**Hardware is read, not parsed.** The scrape reads NVML directly, never
`nvidia-smi` text, reports the distinct error a missing card produces, and is
encrypted per job with a key only the validator can reconstruct.

**Updates are signed.** The agent runs privileged with the docker socket
mounted, so its updater accepts only image digests signed by a validator hotkey
within a ten minute window, and refuses anything else.

---

## Repository

```text
protocol/    types both sides speak, no logic
agent/       the rig agent: api, control, access, hardware, monitor, cache, store, updater, deploy
validator/   the compute validator: session, verify (one file per check), work, storage, recovery, scoring, schedule, data, deploy
challenge/   the compiled GPU challenge, a miner build (nvcc) and a validator build (C)
scripts/     the import scan and the challenge reference vectors CI checks
```

Every push to `main` compiles the challenge with nvcc and publishes two images
with it inside: `ghcr.io/microtensor-io/rig-agent` and
`ghcr.io/microtensor-io/compute-validator`. Validators sign the agent digest they
authorise; rigs update to nothing else. CI lints, compiles and imports every
module, and proves the C and Python challenge references agree.

---

## Status

Subnet 92 on testnet. The agent, the validator, the challenge and the pool
server run end to end against the deployed API; the first real rigs and the
first activated compute validator are the next step. Live rigs, validations and
scores are on the [dashboard](https://www.microtensor.cloud/compute-pool).

---

## License

MIT
