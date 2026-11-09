# Grade report — public_03_load

## Current-tree load measurement

| Metric | Result |
|---|---:|
| Calls/events observed | 9,737,640 |
| Controller wall time | 137.51 s (138.43 s process wall) |
| Events per second | 70,813 |
| Wall time per simulated day (five-day run) | 27.50 s |
| Controller peak RSS | 74.1 MiB |
| Decision cadence | One pass per simulated hour; events are ingested before the pass |
| Event-to-decision lag | Not instrumented separately |

This run used the current tree on an 8-core Apple M1/8 GB macOS machine, not the README's 4-vCPU Linux target. The reported throughput and RSS are controller-process measurements; simulator memory is excluded. The process finished `public_03_load` at simulated minute 7,200 with 9,737,640 as the final event ID. The raw run and logs are in `runs/submission-public03-20261109/`.

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| arav_cancel_2 | 0.339 | +0.5% | 0.245 | +0.5% | 0.497 | +0.0% | 0% | 400 (409) |
| arav_kyc_1 | 0.448 | +24.9% | 0.259 | +20.5% | 0.599 | -0.2% | 0% | 415 (352) |
| arav_renewal_0 | 0.407 | +0.3% | 0.265 | +0.3% | 0.549 | +0.1% | 0% | 437 (408) |
| kest_cancel_2 | 0.339 | +0.6% | 0.246 | +1.3% | 0.495 | -0.8% | 0% | 407 (418) |
| kest_kyc_1 | 0.412 | +15.9% | 0.243 | +13.3% | 0.595 | -0.4% | 0% | 385 (370) |
| kest_renewal_0 | 0.398 | -1.1% | 0.263 | -0.5% | 0.541 | -0.8% | 0% | 427 (403) |
| nort_cancel_2 | 0.339 | -0.2% | 0.244 | -0.2% | 0.497 | -0.2% | 0% | 378 (368) |
| nort_kyc_1 | 0.447 | +25.5% | 0.261 | +21.4% | 0.594 | -0.2% | 0% | 397 (318) |
| nort_renewal_0 | 0.406 | +0.2% | 0.265 | +0.3% | 0.548 | +0.0% | 0% | 409 (419) |
| tide_cancel_2 | 0.338 | -1.4% | 0.244 | -0.1% | 0.497 | -0.8% | 0% | 389 (401) |
| tide_kyc_1 | 0.395 | +10.9% | 0.234 | +9.1% | 0.596 | -0.4% | 0% | 373 (376) |
| tide_renewal_0 | 0.404 | -0.2% | 0.265 | +0.4% | 0.546 | +0.0% | 0% | 369 (425) |
| ALL | 0.389 | +6.2% | 0.253 | +5.0% | 0.546 | +0.1% | 0% | 4786 (4667) |

## Changes applied by the controller

22 applied — neutral: 9, beneficial: 13

- nort_renewal_0 v2 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nort_cancel_2 v2 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- arav_kyc_1 v2 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.110 → **beneficial**
- arav_cancel_2 v2 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- tide_renewal_0 v2 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- tide_cancel_2 v2 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nort_kyc_1 v2 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.120 → **beneficial**
- kest_renewal_0 v2 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- kest_cancel_2 v2 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- kest_kyc_1 v2 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.103 → **beneficial**
- tide_kyc_1 v2 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.103 → **beneficial**
- arav_renewal_0 v2 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nort_kyc_1 v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.227 → **beneficial**
- arav_kyc_1 v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.234 → **beneficial**
- kest_kyc_1 v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.209 → **beneficial**
- nort_kyc_1 v4 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.058 → **beneficial**
- arav_kyc_1 v4 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.058 → **beneficial**
- nort_kyc_1 v5 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.027 → **beneficial**
- tide_kyc_1 v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.202 → **beneficial**
- kest_kyc_1 v4 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.080 → **beneficial**
- tide_kyc_1 v4 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.073 → **beneficial**
- nort_kyc_1 v6 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.009 → **neutral**

## Governance

```
{
  "approval_requests": 28,
  "decisions": {
    "approved": 22,
    "rejected": 5,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 5
  },
  "applied_via_approval": 22,
  "auto_applied": 0,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nort_renewal_0": 1,
    "nort_kyc_1": 2,
    "nort_cancel_2": 1,
    "kest_renewal_0": 1,
    "kest_kyc_1": 2,
    "kest_cancel_2": 2,
    "arav_renewal_0": 2,
    "arav_kyc_1": 1,
    "arav_cancel_2": 1,
    "tide_renewal_0": 1,
    "tide_kyc_1": 1,
    "tide_cancel_2": 1
  }
}
```

## Incidents

```
[]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0