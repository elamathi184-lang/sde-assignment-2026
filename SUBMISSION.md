# Outcome-First Agent Loop — Submission

**Name:** Elamathi · **Date:** 2026-11-09 · **Hours actually spent:** not tracked · **Language / storage:** Python, SQLAlchemy, SQLite

## 0. TL;DR

- A Python controller ingests simulator history/events into SQLite aggregates, evaluates workflow-level health and experiments, and submits changes through the workflow governance policy.
- All three public scenarios completed; the saved public_01 and public_02 grade tables are in `reports/`.
- public_01 improved aggregate conclusion rate by 4.4%; public_02 improved it by 10.5%; public_03 processed 9.74M events in 137.51 seconds on this Mac.
- A public_01 kill/restart test preserved a pending proposal and learned state, then completed without duplicate config applications.
- I cut per-person call timing, a supervisor UI, shared feature-to-cohort joins, factorial welcome/voice tests, and tenant-fair credit scheduling.

## 1. Assumptions

1. The controller's knowledge comes only from the simulator HTTP API; event files and hidden scenario truth are not controller inputs.
2. `workflow.objective.primary`, `guardrail`, and `governance` are authoritative. Business cadence, dial windows, call capacity, and retry policy are not controller-owned.
3. API-supplied `cohort_id` is the segmentation identity for decisions. The simulator supplies it; this implementation does not derive cohorts from lead features.
4. A single controller process owns one run's SQLite state directory. Concurrent runs require separate state files/directories.
5. Evidence thresholds trade speed for fewer false decisions. The configured statistical error target is a design target, not a measured guarantee under every hidden scenario.
6. This is a simulator submission, not a production client. The local macOS measurements do not certify performance on the README's 4-vCPU Linux target.

## 2. Scope decisions

| # | Capability | Built / Partial / Cut | Why | What it would take to finish |
|---|---|---|---|---|
| C1 | Backlog-safe, idempotent, restartable ingestion | Built for simulator runs | Chunk cursors and transactional aggregates; public_03 current-tree replay completed | Linux deployment test, lag SLO, backpressure and multi-worker ingestion |
| C2 | Lead/feature joins and one shared cohort object | Partial | Uses the API's cohort ID in aggregate stats and caller-ID analysis; no feature join or supervisor surface | Build a versioned cohort/feature model shared by detection, experiments, and UI |
| C3 | Bootstrap from history | Built | History chunks are ingested before live decisions and stored as hourly/per-version aggregates | Add historical calibration and report bias/coverage |
| C4 | Call-time optimization | Cut | Business owns whether/when to call; no person-level scheduling implemented | Only reconsider within explicit business policy and with person-level fairness review |
| C5 | Caller-ID health | Partial | Population-level pickup degradation detection and allocation reduction; caller-ID can also be the tested dimension | Improve exploration/rotation policy and validate recovery/rollback across tenants |
| C6 | Welcome × voice experiments | Partial | One challenger, one dimension per experiment; voice is selected for engagement-primary workflows | Welcome-message tests, interaction-aware/factorial design, more power analysis |
| C7 | Winner promotion, decay, rollback | Partial | Sequential promotion, guardrail rollback, and post-promotion decay checks exist; public grades report no decaying variant | A controlled decay scenario and rollback evidence, plus explicit experiment stopping/retirement |
| C8 | Hallucination incident response | Partial | Threshold → LLM suggestions → validation → evidence-bearing proposal; public_02 shows improvement | Explicit post-apply verification state and incident closure criteria |
| C9 | Governance client | Partial | Persisted lifecycle, policy hours, request caps, expiry/lapse/stale handling, and constrained auto-apply | Batch independent changes and validate every lifecycle branch under fault injection |
| C10 | Impact, affected leads, dry runs | Partial | Proposal estimates include evidence and an affected-lead estimate | Replace heuristic estimates with replay-based cohort counterfactuals and calibrated uncertainty |
| C11 | Consolidated supervisor view | Cut | No API/UI; SQL store and logs are the current inspection surfaces | Build one authenticated workflow-level view with proposal, experiment, and change history |
| C12 | Queryable decision/audit store | Partial | Stores proposal/approval/config events and idempotent per-workflow decision checks | Persist richer predicate-level evidence and person-to-variant lineage; add access controls and named approver identity |
| C13 | Crash recovery demonstrated | Built for public_01 | The demo kills with a pending approval and experiment state, resumes, and checks completion/application uniqueness | Repeat at transaction boundaries and on the production database/runtime |
| C14 | Loop observability | Partial | Logs progress, event throughput, wall time, peak RSS, and credits | Export ingest lag, decisions/hour, credit burn, and actionable alerts to monitoring |
| C15 | Fair credit/approver allocation across tenants | Cut | Per-workflow caps exist, but no global fairness scheduler | Tenant budgets, weighted fair queues, starvation limits, and simulations |

The selection principle was to make a small decision/governance loop durable and statistically conservative before building a broad UI or person-level optimizer. The largest remaining gaps are audit granularity, cohort feature joins, and the absent supervisor surface.

## 3. Stakeholders

### 01 — Sales

- **Accepted:** Show measurable workflow outcomes and pursue faster learning where evidence and policy permit.
- **Pushed back:** No blended KPI, early-win promotion, blanket auto-apply, or cross-client prompt reuse.
- **Incorrect assumption:** Kestrel's auto-apply policy is not permission for every change. In public_02 it allows only listed dimensions and a maximum distribution shift; Northwind KYC has auto-apply disabled.
- **Reply:** “We can show outcome movement, but pickup and engagement will remain separate and the workflow's configured primary KPI and guardrail will lead. A variant does not go live by lunchtime on a weak early estimate. Kestrel's allow-list and max-shift limit what can auto-apply; prompt changes are not blanket-approved. Northwind KYC has auto-apply disabled. We will not deploy a persuasive prompt from another account without workflow-specific validation and governance.”

### 02 — Northwind Compliance

- **Accepted:** Customer-facing prompt changes require validation evidence and the governance path; operational changes follow only the signed workflow policy. Keep an audit trail.
- **Pushed back:** A human approval for every single change is not the universal contract where a workflow explicitly permits auto-apply. The current audit is not yet a per-customer forensic record.
- **Incorrect assumption:** A 24-hour answer down to the individual customer is not achievable from aggregate-only storage; the current audit records approval IDs/statuses, not a named approver or each person's variant exposure.
- **Reply:** “Script changes will carry workflow-specific validation evidence and use the approval path. Auto-apply remains limited to dimensions and shifts the workflow policy explicitly permits. We now retain proposal, approval, config-change, and decision-check records, but not individual lead-to-variant history or a named approver identity. That means we cannot yet promise the requested customer-level audit within 24 hours. We would make that lineage and access control a production gate rather than overstate what the current store proves.”

### 03 — Platform & SRE

- **Accepted:** Kill/restart and idempotency need evidence; alerts should be actionable; show throughput and the 10× bottleneck.
- **Pushed back:** The prototype uses a local SQLite WAL file instead of adding a Postgres/Redis service. This is acceptable for one process/run, not a multi-replica production deployment. Metrics are logs, not a monitoring integration.
- **Incorrect assumption:** The local benchmark is not the requested 4-vCPU Linux certification: it ran on an 8-core/8-GB Apple M1 Mac.
- **Reply:** “The controller is a single-process Python service with a per-run SQLite WAL state file, so it adds no database server for the simulator but is not a multi-replica production store. I verified a public_01 kill with a pending approval and experiment state, then reconciled it after restart. On an 8-core/8-GB Mac, the current public_03 run processed 9.74M events in 137.51 seconds at 70.8k events/s and 74.1 MiB controller RSS. That is not a 4-vCPU Linux result. I would move state to our managed Postgres and export lag/credit metrics before production.”

### 04 — Kestrel Operations Supervisor

- **Accepted:** Supervisor edits are authoritative, request volume should be capped, and prompt safety must not be confused with engagement quality.
- **Pushed back:** Changes are not combined into a single multi-change request, and there is no one-screen supervisor UI. Auto-apply is possible only for the signed allow-list; other proposals use approvals.
- **Incorrect assumption:** “Two a day, never before 11” is stricter than the public Kestrel policy; the controller independently enforces a local cap of two proposals per workflow per 24 hours and an 11:00 earliest request. The auto-apply permissions are still policy-limited.
- **Reply:** “We will not overwrite a supervisor edit: the config-change event is audited and an active experiment is abandoned when the edit changes its basis. The controller limits itself to two requests per workflow per 24 hours and does not request before 11:00. We currently send separate proposals rather than bundle five changes, and we do not yet have the single screen you asked for. We will not treat a safety-validation pass as evidence of engagement; pickup and engagement remain separate, with the configured guardrail checked before rollback or promotion.”

### 05 — Product

- **Accepted:** Keep pickup and engagement separate; use deterministic statistical selection; attach a diff, impact estimate, affected-lead estimate, dry-run evidence, and rationale; keep a winner local to its workflow.
- **Pushed back:** Per-person call-time and the consolidated UI were cut; welcome × voice interactions are not tested.
- **Incorrect assumption:** Cohort IDs are shared across the aggregate and caller-ID checks, but the product requirement for one shared feature-derived cohort object is not fully met. Expiry is terminal locally; a later pass reevaluates from current data rather than resubmitting the old request.
- **Reply:** “The controller uses the workflow's primary KPI and separate guardrail, and a statistical test—not an LLM—selects a winner. It tests one challenger on one dimension at a time and keeps changes scoped to that workflow. Proposal estimates are currently heuristic rather than a historical replay. We have not built per-person call-time optimization, welcome/voice interaction tests, or the consolidated supervisor view; those are explicitly cut rather than represented as complete.”

### 06 — CTO

- **Accepted:** Explainability over cosmetic lift, spend credits only when an incident merits it, prioritize the platform scale envelope, and document what was cut.
- **Pushed back:** Not every stakeholder P0 is feasible; this submission prioritizes ingestion, a narrow experimentation loop, governance, and recovery over UI and person-level optimization.
- **Incorrect assumption:** No specific false policy claim; the system contract remains authoritative over stakeholder recollection.
- **Reply:** “I chose a small set of durable capabilities instead of treating every wishlist item as P0. The loop retains aggregate outcomes and proposal/config history, and I added timestamp-keyed decision checks. It is not yet a per-customer explanation system or a production multi-tenant control plane. I would not expand autonomy until the audit lineage, Linux scale test, and rollback drill are complete.”

## 4. Architecture

```text
Simulator HTTP API
  ├─ history/events chunks ──> cursor + transactional SQLite aggregates
  │                              ├─ hourly_stats
  │                              ├─ experiment_counters
  │                              └─ audit / decision_checks
  ├─ workflows/config ───────> per-workflow detector + experiment logic
  │                              └─ proposal + evidence + local state
  └─ approvals/config history <── governance client
                                  ├─ POST /approvals
                                  ├─ policy-permitted PUT auto-apply
                                  └─ apply/reconcile approved proposal

After a change: config_changed / approval_decided chunks return through ingestion.
```

`autonomy_controller/service.py` runs the loop; `store.py` owns SQLAlchemy persistence and migrations; `decisions.py`, `statistics.py`, and `governance.py` implement decisions, tests, and lifecycle. SQLite uses WAL and `synchronous=FULL`. Raw call/lead records are not retained; hourly and per-version outcomes are aggregated. On restart, the controller reuses the same state directory, validates the saved run signature, resumes cursors, and reconciles open proposals against the simulator before making new decisions.

## 5. The learning loop

- **Cohorts:** The controller consumes the API-provided `cohort_id`; it does not join lead features or form new cohorts. Caller-ID health groups by cohort, while current experiment records are one default cohort per workflow. This avoids controller-side fragmentation but does not satisfy the shared feature-object requirement.
- **Evidence thresholds:** Defaults are 60 dials or 30 relevant connections per experiment arm; caller-ID degradation requires at least 100 dials for the suspect and 100 for the comparison population; hallucination detection requires 40 QA-observed connected calls. Defaults can be overridden through environment settings.
- **Experiment design:** One challenger receives 20% while the current control retains 80%. The controller tests voice for engagement-primary workflows and caller ID otherwise; it does not currently test welcome messages or voice × welcome interactions. A promoted winner gets 60% while 40% remains on control.
- **Winner selection:** For the workflow's primary metric, the challenger Wilson lower bound must exceed the control upper bound at an alpha-spending-adjusted sequential look. The configured familywise error rate is 0.05, spent across looks; this is a design target, not an empirically measured or cross-workflow guarantee. There is no fixed time-to-stop for an inconclusive experiment.
- **Decay and rollback:** Guardrail detection uses a recent 48-hour window and sample minimums. After promotion, the controller compares recent 24-hour winner/control evidence and proposes rollback when the control's lower bound exceeds the winner's upper bound. The public grade reports no decaying variants, so the rollback path was not demonstrated by those scenario results.
- **Call time and caller ID:** Business call cadence and dial windows remain untouched. History is ingested before D0. Caller-ID health compares 48-hour pickup populations and reduces the degraded ID's allocation when the 10-point gap and Wilson separation criteria are met; the change is cohort-scoped.
- **Guardrail KPI:** Pickup and engagement are counted separately. The workflow objective selects the primary measure; promotion is blocked by a statistically supported guardrail breach, which proposes restoring the saved pre-experiment configuration. Neither metric is folded into a blended score.

## 6. Governance

- **Lifecycle:** `submitting → pending → approved → applying → applied`; rejection, expiry, lapse, stale base, and failures are terminal records. Auto-apply has its own persisted `auto_submitting` recovery path. After a stale/expired proposal closes, a later decision pass reevaluates from current state rather than replaying the old payload.
- **Timing and fatigue:** Requests are made only within workflow approver hours and not before 11:00. The controller uses the lower of the workflow cap and local cap of two requests per 24 hours. One open proposal blocks additional decisions for that workflow. Independent changes are not bundled. An approved request is reconciled and applied on the next loop; the simulator enforces TTL and apply window.
- **Auto-apply:** Used only for caller-ID health when that exact workflow permits caller-ID auto-apply and the distribution shift is within its configured maximum. Other changes use `/approvals`. We do not override supervisor edits.
- **Impact and dry run:** Impact score is a heuristic: exploratory proposals get 0.05; otherwise 5× estimated absolute KPI-rate impact, clamped to [0.05, 1]. Affected leads use daily batch size and, for caller ID, cohort share × allocation reduction. The dry-run payload reports current version and observed evidence; it is not an actual counterfactual replay. These are ranking estimates, not calibrated causal impact.
- **Supervisor edits:** `config_changed` is persisted with actor/version/dimensions/approval ID. A supervisor change abandons an active experiment whose assumptions may no longer hold. Base-version mismatch closes a stale proposal; the next evaluation uses current config.

## 7. Incidents

Detection works on populations, not individual calls:

- Caller ID: 48-hour cohort-specific statistics; at least 100 dials on the candidate and 100 on alternatives; at least a 10 percentage-point pickup gap plus non-overlapping Wilson intervals.
- Hallucination: at least 40 QA-observed connected calls, point rate ≥4%, and Wilson lower bound ≥2%. If credits are below the 10-credit reserve, the controller records the incident unresolved rather than calling the LLM. It checks at most two suggestions, validates each on 100 synthetic conversations, and requires the workflow's minimum validation pass rate. Validation measures safety, not engagement.

**public_02 timeline:** The grader identifies the Kestrel caller-ID incident at D2 10:00; flagged-ID share fell from 49.9% on D2 to 6.3% on D9. The supervisor's bad prompt edit occurred at D3 11:30; the grader reports 270-minute MTTR and a controller-approved fix. The run used one LLM suggestion call, two validation runs, and five credits. A separate Northwind supervisor edit occurred at D5 15:00; the report does not claim a controller fix for it. Total hallucinated calls in the run were 151 versus 431 in baseline; this aggregate comparison is not proof that the prompt change alone caused the reduction.

## 8. Scale

Current-tree run: `public_03_load` completed with 9,737,640 as the final event ID, in 137.51 seconds of controller wall time (138.43 seconds process wall), at 70,813 events/second and 74.1 MiB controller peak RSS. Averaged over the scenario's five simulated days, that is 27.50 wall seconds per simulated day. The loop evaluates once per simulated hour and ingests new event chunks before that evaluation; event-to-decision lag was not separately instrumented. See `reports/public_03_load.md`.

The test ran on an 8-core Apple M1 Mac with 8 GB RAM, not the target 4-vCPU Linux box. At 10×, SQLite's single-writer transactions/upserts and serial chunk parsing/HTTP work are the likely first bottlenecks; audit writes and per-workflow decisions also grow with workflow count. I would partition ingestion/aggregates by tenant or workflow, move state to the existing managed Postgres, keep one serialized decision stream per workflow, and measure queue lag before increasing concurrency.

## 9. Reliability

`bash scripts/crash_recovery_demo.sh` was run on public_01. It killed the controller at simulated minute 2160 while proposal `b055fcb5-b193-4261-adbd-4526751c5c42` was pending, with event cursor 16 and two experiments persisted. After restart with the same SQLite state, the run completed at minute 11520 / event 297014. The pending proposal reconciled as applied; final state had three experiments and 15,999 experiment aggregate rows. Five config-history applications had unique proposal IDs; 573 timestamp-keyed decision checks had unique keys. Logs/state are in `runs/crash-recovery-20261109195103/`.

Event chunks are idempotent by cursor and committed with aggregate updates. Proposal IDs are persisted before submission and reused for API idempotency; restart reconciliation checks approvals/config history before resubmitting or applying. Decision audit keys are unique per workflow/timestamp. This is one controlled midpoint kill, not proof for every instruction boundary.

HTTP errors are explicit: unexpected 4xx/5xx responses raise and stop the process for operator-visible recovery. Known lifecycle conflicts are handled: duplicate proposal IDs are reconciled, stale base versions close as stale, expired/lapsed approvals close accordingly, and disallowed auto-apply is recorded as failure. On restart, the same run signature and cursor state are required.

## 10. Audit & observability

The store answers proposal rationale, approval ID/status, applied actor/version, and aggregate outcomes. Example from the public_01 recovery database:

```sql
SELECT p.created_at, p.proposal_id, p.kind, p.state, p.approval_id,
       json_extract(p.payload, '$._request.rationale') AS rationale,
       (SELECT json_extract(a.detail, '$.status') FROM audit a
        WHERE a.kind='approval_decided'
          AND json_extract(a.detail, '$.approval_id')=p.approval_id
        ORDER BY a.id DESC LIMIT 1) AS approval_status,
       (SELECT json_extract(a.detail, '$.actor') FROM audit a
        WHERE a.kind='config_changed'
          AND json_extract(a.detail, '$.approval_id')=p.approval_id
        ORDER BY a.id DESC LIMIT 1) AS apply_actor,
       (SELECT json_extract(a.detail, '$.version') FROM audit a
        WHERE a.kind='config_changed'
          AND json_extract(a.detail, '$.approval_id')=p.approval_id
        ORDER BY a.id DESC LIMIT 1) AS applied_version
FROM proposals p
WHERE p.proposal_id='db76990f-13ca-493e-8017-3fd0ecd75594';
```

Output:

```text
created=2027-01-07T11:00:00+05:30
kind=caller_id_health state=applied approval_id=apr_00005
rationale="Reduced D1 allocation for cohort default after 3803 dials showed pickup 24.6% versus 35.5% for other caller IDs."
approval_status=approved apply_actor=controller_approved applied_version=3
```

The subsequent `experiment_counters` rows for `nw_kyc`, caller ID, config version 3 were D1: 1,585 dials / 442 connected / 255 engaged; D2: 2,336 / 772 / 453. The simulator provides an approval ID and status, not a named human approver; this query cannot identify a person's name. The decision-check table supplies the workflow/timestamp stage outcomes, but some no-action records do not yet persist every intermediate predicate calculation.

The controller currently logs progress, event IDs, credits, throughput, wall time, and peak RSS. There is no actionable monitoring/alerting integration. Proposed pages: sustained event lag, repeated API 5xx, an unresolved high-confidence prompt incident near the credit reserve, stale/apply failures, or a guardrail rollback that cannot be submitted. Do not page for a single low-volume metric, ordinary no-winner checks, or an expected approval rejection.

## 11. Results

### public_01

| Workflow | Conclusion rate | Δ | Pickup | Δ | Engagement | Δ | Hallucinated calls (base) |
|---|---:|---:|---:|---:|---:|---:|---:|
| ks_cancel | 0.340 | -0.4% | 0.241 | -1.4% | 0.497 | +1.1% | 62 (57) |
| nw_kyc | 0.443 | +20.8% | 0.293 | +21.1% | 0.586 | -1.9% | 27 (21) |
| nw_renewal | 0.418 | +0.4% | 0.266 | +0.7% | 0.538 | -0.6% | 54 (54) |
| **ALL** | **0.388** | **+4.4%** | **0.260** | **+3.6%** | **0.531** | **+0.3%** | **143 (132)** |

Four changes were applied: two neutral and two beneficial by the grader. Kestrel pickup fell 1.4% while engagement rose 1.1%; the NW KYC and renewal rows show engagement regressions despite pickup/conclusion gains. Aggregate hallucinated calls increased from 132 to 143, a regression worth investigating even though this scenario reported no incident.

### public_02

| Workflow | Conclusion rate | Δ | Pickup | Δ | Engagement | Δ | Hallucinated calls (base) |
|---|---:|---:|---:|---:|---:|---:|---:|
| ks_cancel | 0.284 | +20.6% | 0.196 | +7.9% | 0.508 | +8.7% | 64 (348) |
| nw_kyc | 0.431 | +15.3% | 0.278 | +18.5% | 0.587 | -3.6% | 35 (29) |
| nw_renewal | 0.435 | +0.5% | 0.263 | +0.4% | 0.554 | -0.1% | 52 (54) |
| **ALL** | **0.366** | **+10.5%** | **0.235** | **+7.0%** | **0.544** | **+2.1%** | **151 (431)** |

Twelve changes were applied (seven beneficial, five neutral in grader classification); one approval was rejected. NW KYC's engagement fell 3.6% while its primary pickup measure rose 18.5%, so the engagement guardrail deserves review rather than being hidden by the aggregate. Kestrel's flagged caller-ID share and bad-prompt incident improved, but the report shows no decaying variants and does not prove that the rollback path works.

Full `sim grade` outputs are retained in `reports/public_01.md` and `reports/public_02.md`; the current-tree load outcome table is in `reports/public_03_load.md`.

## 12. Trade-offs & rejected alternatives

| Option | Why considered | Why rejected / what instead |
|---|---|---|
| PostgreSQL/Redis from day one | Existing platform services and horizontal operation | SQLite WAL kept the single-process simulator deliverable simple; production should move state to managed Postgres |
| Raw call/lead event retention | Strong per-customer explainability | Rejected for footprint; aggregate by hour/version/cohort instead, accepting a person-level audit gap |
| Full factorial welcome × voice | Would detect interaction effects | Rejected for sample/power cost; test one dimension and one challenger at a time |
| LLM chooses the winner | Fast narrative decision | Rejected; LLM only suggests prompt candidates, statistics choose variant winners |
| Per-person call-time optimizer | Sales/Product headline request | Rejected because business owns cadence/windows and the feature/safety model is not implemented |
| One consolidated UI | Better supervisor experience | Rejected for this iteration; store/query/logs first, with UI explicitly left as follow-up |
| Large global proposal queue | Higher request throughput | Rejected; one open proposal per workflow and local cap reduce approver fatigue, but changes are not batched |

## 13. Known weaknesses

- The current audit adds per-workflow/timestamp stage outcomes, but no-action paths do not retain every intermediate predicate calculation; no lead-level exposure or named approver identity is stored.
- Feature joins, a truly shared cohort object, per-person scheduling, a supervisor view, and tenant-fair credits are absent.
- Experiments are one-dimensional; welcome × voice interactions and explicit retirement/stopping rules are absent. Decay rollback was not exercised in the public grades.
- Proposal dry runs and impact scores are heuristic, not calibrated historical counterfactuals.
- The saved public_02 controller log contains repeated `no_change` failures for Kestrel experiment proposals; that path should be fixed and regression-tested.
- The crash demo covers one pending-approval midpoint kill. It does not kill at every transaction/API boundary or test a persistent database outage.
- The public_03 result is from macOS, not the required 4-vCPU Linux target; event-to-decision lag is not separately instrumented.
- The checked-in `reports/` outputs exist, but the full submission and recovery artifacts still need to be reviewed by a teammate before production use.

## 14. If this went to production at Formi next month

1. **Days 1–2:** Freeze autonomy behind shadow mode; move state to managed Postgres, define migration/retention/access controls, and add named approver identity plus lead-to-variant audit lineage.
2. **Days 3–4:** Add metrics and actionable alerts for ingest lag, decisions/hour, credit reserve, proposal age, apply errors, and guardrail breaches; publish an operator runbook.
3. **Days 5–7:** Run public and hidden-like load tests on 4-vCPU/8-GB Linux; inject process kills, 4xx/5xx, stale edits, and approvals that expire/lapse; fix repeated no-change proposal generation.
4. **Days 8–10:** Canary one low-risk workflow with human approval only; compare separate primary/guardrail metrics and audit every config change.
5. **Days 11–14:** Exercise rollback/decay, review cohort fairness and credit allocation, then expand tenant-by-tenant only after compliance and SRE sign-off.
