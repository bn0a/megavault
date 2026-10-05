---
title: Mini Claim Ledger
type: ledger
status: active
evidence_level: mixed
published: 2026-02-01
last_verified: 2026-02-01
review_due: 2026-03-03
tags:
  - claims
  - evidence
---

# Mini Claim Ledger

Status is computed, not asserted: `supported` needs accepted evidence; central, numerical, causal, comparative and predictive claims additionally need two independent origin groups, otherwise they are `qualified`. Accepted evidence both ways makes a claim `disputed`; accepted refuting evidence with no accepted support makes it `refuted`.

| ID     | Claim                                                                                            | Status          | Evidence     | Scope, action, caveat                                                              | Groups |
| ------ | ------------------------------------------------------------------------------------------------ | --------------- | ------------ | ---------------------------------------------------------------------------------- | ------ |
| CL-001 | Watering intervals on the reference planter are set in days, not weeks.                  | **supported**   | S-001, S-003 | Reference planter only. Read interval settings as day values.                      | 2      |
| CL-002 | Setting the grow light two steps brighter increases leaf growth by roughly a third.     | **qualified**   | S-002        | One lab study, 42 cuttings. Do not quote the effect size as settled.               | 1      |
| CL-003 | The planter stores a separate watering interval per plant profile rather than one global setting. | **supported**   | S-001        | Planter documentation. Set the interval per plant profile, not globally.           | 1      |
| CL-004 | Light above 400 PPFD scorches the leaves of most pothos cuttings.                        | **disputed**    | S-003, S-004 | Observation claim. Practitioner report and moisture-sensor vendor disagree outright.      | 1      |
| CL-005 | Smart planter apps log soil moisture at watering-event level.                            | **supported**   | S-004        | Vendor product surface. Treat as a capability statement, not an outcome.            | 1      |
| CL-006 | Pot choice measurably changes leaf growth.                                               | **unsupported** | —            | Nobody has measured this. Do not assert it in a review.                             | 0      |

## Claim index

One heading per claim, so a wikilink to `Claim Ledger#CL-###` lands on the claim. Status, evidence and caveats live in the table above; the quoted passages are in [[Mini Evidence Map]].

### CL-001

Watering intervals on the reference planter are set in days, not weeks. — evidence: [[Mini Evidence Map]]

### CL-002

Setting the grow light two steps brighter increases leaf growth by roughly a third. — evidence: [[Mini Evidence Map]]

### CL-003

The planter stores a separate watering interval per plant profile rather than one global setting. — evidence: [[Mini Evidence Map]]

### CL-004

Light above 400 PPFD scorches the leaves of most pothos cuttings. — evidence: [[Mini Evidence Map]]

### CL-005

Smart planter apps log soil moisture at watering-event level. — evidence: [[Mini Evidence Map]]

### CL-006

Pot choice measurably changes leaf growth. — evidence: [[Mini Evidence Map]]

---

Up: [[00 Mini Home]] · [[Mini Evidence Map]] · [[Mini Source Register]]
