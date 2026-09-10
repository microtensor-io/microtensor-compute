# Incentive mechanism

Two streams, the emission score term by term, the revenue share, and the commitment fee that makes a fabricated identity cost more than it earns.

The same page on the site: https://www.microtensor.cloud/docs/compute/getting-started/incentives

---

### Two streams

Emissions alone pay for idle hardware. Revenue alone leaves a new network with no supply before it has demand. The pool pays both: an emission floor computed from what the validators observed, and a revenue share on every job the rig completed.

### The emission score

Per rig, per epoch:

```
score    = base × w_d × s_c × m_iso × m_up × m_drv × m_opt
emission = pool × score / Σ score
```

`pool` is sixty percent of compute emission. The remaining forty percent is held before anything is distributed, funding burn, the validator share and a reserve. A pool paying out everything has nothing to adjust with when demand shifts.

| Term | What it is | Where it comes from |
|---|---|---|
| `base` | 1 if the last deep pass passed, otherwise 0 | The validator's session, nothing else |
| `w_d` | Demand weight, an exponential moving average of revenue per GPU type | The job ledger, smoothed so one busy afternoon does not swing the fleet |
| `s_c` | Capacity share, the rig's accepted GPUs over the fleet's | The enrolment record |
| `m_iso` | Isolation multiplier, 1 with verified container isolation, lower without | The deep pass |
| `m_up` | Uptime, ramping linearly to 1 over fourteen days | The connection record |
| `m_drv` | Driver gate, 1 or 0 once an announced cutoff passes, exempt mid rental | The scrape and the job ledger |
| `m_opt` | Rental opt out, below 1 for a rig that declines rental | The rig's own setting |

A rig that failed verification scores zero and earns nothing from either stream. See [The emission score](incentive_mechanism.md#the-emission-score) for each term with its current value.

### Revenue share

Eighty percent to the rig, twenty to the network. Inference pays per request served. Reserved work, meaning rental and shaping, pays per hour held whether or not the customer keeps the card busy, because reserved capacity is paid for availability by definition. Mining pays whatever the target subnet paid, less the network share.

A job that failed on the rig's side pays nothing. A job that failed on the pool's side pays in full, because the rig held capacity it was asked to hold.

### The entry fee

One off per GPU, tiered by card, non refundable, staked to the subnet. A twenty GPU host pays twenty fees, which is what makes fabricated identity unprofitable: the cost scales with the capacity being claimed rather than with the number of accounts someone can create. Removal for misbehaviour forfeits the fee, and returning means paying again at the current rate. See [Revenue share and fees](incentive_mechanism.md#revenue-share-and-fees).

### What is deliberately absent

- **Presence.** A machine online and doing nothing scores a base of zero. Uptime multiplies work done and is never a term of its own.
- **Self reported performance.** Every input is a validator observation or a number from the assignment ledger.
- **Hardware class as a bonus.** A better card earns more because demand weight says the market pays more for it. If demand for a tier collapses, its emission follows without anyone editing a constant.

## The emission score

### The formula

Per rig, per epoch, computed hourly by the pool:

```
score    = base × w_d × s_c × m_iso × m_up × m_drv × m_opt
share    = score / Σ score over the fleet
emission = pool_share × share
```

The share per hotkey, summing the shares of every rig a hotkey owns, is what compute validators set as weights. The epoch table is public to validators at `GET /v1/compute/validators/scores` and to the operator at `GET /v1/operator/compute/scores`, with every term for every rig.

### The terms

#### base

1 if the rig's last deep pass passed and the rig is in probation or active, otherwise 0. It comes only from the validator's session. A machine that failed verification scores zero and earns nothing from either stream, and a machine that has never been verified is not in the fleet at all.

#### w_d, demand weight

An exponential moving average of revenue per GPU type:

```
w_t = w_(t-1) + α (r_t − w_(t-1))
```

`r_t` is the revenue earned per GPU of that type in the epoch, and `α` is 0.1 by default. GPU types earning more get a larger share of emission, smoothed so one busy afternoon does not swing the fleet. Without it, a card nobody rents earns the same floor as one booked solid, and the pool fills with hardware the market has not asked for. Before any revenue exists every type carries the same weight, so the floor still stands.

#### s_c, capacity share

The rig's accepted GPUs over the fleet's verified GPUs. Twenty cards earn twenty times one card, which is what makes the per GPU entry fee proportionate.

#### m_iso, isolation

A cost rather than a gate. A machine without verified container isolation is still useful for inference and mining; it simply cannot be trusted with a stranger's code, so it earns less. Verified isolation is 1, unverified is 0.7 by default.

#### m_up, uptime

Ramps linearly from 0 to 1 over fourteen days of continuous connection. This is probation expressed as money rather than as a flag: it makes churn unprofitable and means a proven machine is worth more than an identical unproven one. A drop longer than a minute resets it.

#### m_drv, driver

A hard gate, phased. The driver runs privileged, and a vulnerability there reaches every tenant on the host, so an out of date driver eventually earns nothing. But a cutoff is announced with a grace period, because a contributor who has not read the announcement is not an attacker, and a machine mid rental is exempt entirely: a customer must never be interrupted because their host has not upgraded. With no cutoff announced the term is 1.

#### m_opt, rental opt out

Prices the choice. Rental sits first in the priority ladder, so a machine declining it is unavailable for the most valuable work. If the floor were identical either way, everyone would decline and rental would have no supply. Declining rental sets the term to 0.8 by default.

### The pool share

Sixty percent of compute emission is distributed by score. The remaining forty percent is held before anything is distributed, funding burn, the validator share and a reserve. A pool paying out everything has nothing to adjust with when demand shifts.

### Current values

| Parameter | Default |
|---|---|
| Pool share | 0.6 |
| Demand smoothing α | 0.1 |
| Isolation multiplier without isolation | 0.7 |
| Rental opt out multiplier | 0.8 |
| Uptime ramp | 14 days |
| Epoch | 3600 seconds |
| Driver minimum and cutoff | none announced |

The operator can read the effective values at `GET /v1/operator/compute/config`. Changes take effect at the next epoch and are announced before they do.

### What is deliberately absent

Presence: a machine online and doing nothing scores a base of zero, and uptime multiplies work done rather than standing on its own. Self reported performance: every input is a validator observation or a number from the assignment ledger. Hardware class as a bonus: a better card earns more because demand weight says the market pays more for it, and if demand for a tier collapses, its emission follows without anyone editing a constant.

## Revenue share and fees

### Revenue share

Eighty percent to the machine, twenty to the network, which is the going marketplace rate.

| Work | Pays |
|---|---|
| Inference | Per request served |
| Rental and model shaping | Per hour held, whether or not the customer keeps the card busy, because reserved capacity is paid for availability by definition |
| Subnet mining | Whatever the target subnet paid, less the network share |

A job that failed on the machine's side pays nothing. A job that failed on the pool's side pays in full, because the machine held capacity it was asked to hold. The pool records every job with its kind, the card it ran on, the revenue and the rig's share, and the Rigs page shows the running total per rig.

### The commitment fee

A one off fee per GPU, tiered, non refundable, staked to the subnet.

| Tier | Fee, relative | Rationale |
|---|---|---|
| Flagship | 4× | Earns the most, so entry should cost the most |
| Professional | 3× | |
| Standard | 2× | |
| Entry | 1× | The base unit |

The fee is per card, not per machine. A twenty GPU host pays twenty fees. That is what makes fabricated identity unprofitable: a contributor who wants twenty registrations pays for twenty, and the cost scales with the capacity they are claiming rather than with the number of accounts they can create.

Two properties this gives. Entry is priced by what the hardware can earn, so a flagship card and an entry card are not equally cheap to register. And removal is a real loss: a machine removed for misbehaviour forfeits the fee, and returning means paying again at the current rate.

### Paying it

1. Claim the rig, so the pool knows which cards it is quoting for.
2. The portal quotes `base × multiplier × GPUs` from the enrolled cards and the rig's tier, and shows the pool coldkey and a memo of the form `rig:<id>`.
3. Send the exact amount with the memo, then report the transfer reference on the portal. Transfers without the memo cannot be matched to the rig.
4. The operator confirms the payment. The rig moves from claimed to validating and a validator reaches it within the hour.

The quote is versioned as a contract. Terms can change between rounds without stranding rigs enrolled under the old ones, and a rig's eligibility is read against the contract it paid under.

### Setting the base unit

It should sit somewhere above a few weeks of what an entry tier card earns in the pool and below what it earns in a quarter. Too low and fabricated identity is cheap; too high and honest contributors with one card never join. This is a number to revisit as the fleet grows rather than to fix once. The current base is served with the hardware table at `GET /v1/pool/hardware`.
