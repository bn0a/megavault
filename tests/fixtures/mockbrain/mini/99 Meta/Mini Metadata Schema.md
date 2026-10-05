---
title: Mini Metadata Schema
type: schema
status: active
evidence_level: official
published: 2026-02-01
last_verified: 2026-02-01
review_due: 2026-08-01
tags:
  - meta
---

# Mini Metadata Schema

Every note carries these keys. `validate` fails the publish if one is missing.

| Key | Values |
|---|---|
| `type` | `home`, `moc`, `concept`, `register`, `ledger`, `map`, `backlog`, `schema`, `log`, `dataset` |
| `status` | `active`, `draft`, `needs_review`, `deprecated` |
| `evidence_level` | `official`, `peer_reviewed`, `empirical`, `mixed`, `hypothesis`, `unsupported` |
| `published` / `last_verified` / `review_due` | ISO dates |

---

Up: [[00 Mini Home]]
