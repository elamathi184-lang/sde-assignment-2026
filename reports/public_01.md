# Grade report — public_01_dev

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.341 | -0.2% | 0.240 | -1.9% | 0.500 | +1.7% | 0% | 60 (57) |
| nw_kyc | 0.443 | +20.8% | 0.288 | +19.2% | 0.595 | -0.4% | 0% | 39 (21) |
| nw_renewal | 0.413 | -0.9% | 0.260 | -1.7% | 0.546 | +0.7% | 0% | 42 (54) |
| ALL | 0.386 | +3.9% | 0.256 | +2.1% | 0.537 | +1.4% | 0% | 141 (132) |

## Changes applied by the controller

5 applied — neutral: 2, beneficial: 3

- ks_cancel v2 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v2 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_kyc v2 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.106 → **beneficial**
- nw_kyc v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.152 → **beneficial**
- nw_kyc v4 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.034 → **beneficial**

## Governance

```
{
  "approval_requests": 7,
  "decisions": {
    "approved": 5,
    "rejected": 2,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 2
  },
  "applied_via_approval": 5,
  "auto_applied": 0,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 1,
    "nw_kyc": 2,
    "ks_cancel": 1
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