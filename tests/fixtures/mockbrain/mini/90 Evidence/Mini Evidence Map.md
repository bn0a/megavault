---
title: Mini Evidence Map
type: map
status: active
evidence_level: mixed
published: 2026-02-01
last_verified: 2026-02-01
review_due: 2026-04-02
tags:
  - evidence
---

# Mini Evidence Map

Every claim-to-passage link, with the mechanical quote check. `exact` means the quote was found verbatim in the cached fetch; `fuzzy` means it matched above threshold; `mismatch` and `no_cache` mean the link is **not** accepted evidence.

### CL-001 — Watering intervals on the reference planter are set in days, not weeks.

Status: **supported** — 2 accepted edge(s) across 2 origin group(s)

- `EV-001` **supports** (accepted) — Example Planter Docs, [Planter settings reference](https://docs.planter.example/planter/settings) @ section `Watering` · quote check: **exact** (1.0)
  > The watering interval is expressed in days and is checked once per moisture cycle.
- `EV-002` **supports** (accepted) — Windowsill Notes, [Growing pothos at home](https://blog.windowsill-notes.example/pothos-at-home) @ paragraph `Days, not weeks` · quote check: **exact** (1.0)
  > Every self-watering planter I have owned sets the interval in days, which is why care guides written in weeks need converting first.

### CL-002 — Setting the grow light two steps brighter increases leaf growth by roughly a third.

Status: **qualified** — needs two independent origin groups; has 1 (leaf.example)

- `EV-003` **supports** (accepted) — Example Leaf Journal, [A controlled study of light level and leaf growth](https://journals.leaf.example/2024/light-study) @ section `Results` · quote check: **exact** (1.0)
  > Leaf growth rose by 34 percent when the light was set two steps brighter.

### CL-003 — The planter stores a separate watering interval per plant profile rather than one global setting.

Status: **supported** — 1 accepted edge(s) across 1 origin group(s)

- `EV-004` **supports** (accepted) — Example Planter Docs, [Planter settings reference](https://docs.planter.example/planter/settings) @ section `Per-profile settings` · quote check: **exact** (1.0)
  > Each plant profile defines its own interval_days value; there is no planter-wide override.

### CL-004 — Light above 400 PPFD scorches the leaves of most pothos cuttings.

Status: **disputed** — 1 accepted refuting edge(s) against 1 supporting

- `EV-005` **supports** (accepted) — Windowsill Notes, [Growing pothos at home](https://blog.windowsill-notes.example/pothos-at-home) @ paragraph `Where it breaks` · quote check: **exact** (1.0)
  > Above about 400 PPFD our cuttings stopped looking lush and started showing scorched leaf edges.
- `EV-006` **refutes** (accepted) — PlantLog, [Home growing telemetry report](https://analytics.plantlog.example/reports/growing) @ section `Light level` · quote check: **exact** (1.0)
  > Across the plants in this sample, light levels between 400 and 500 PPFD show no measurable change in leaf-scorch tags.

### CL-005 — Smart planter apps log soil moisture at watering-event level.

Status: **supported** — 1 accepted edge(s) across 1 origin group(s)

- `EV-007` **supports** (accepted) — PlantLog, [Home growing telemetry report](https://analytics.plantlog.example/reports/growing) @ section `Events` · quote check: **exact** (1.0)
  > Soil moisture is logged per watering event and is not joined to the leaf-growth table.

---

Up: [[00 Mini Home]] · [[Mini Claim Ledger]]
