# Write reconciliation memos

BlueFin Commerce has reconciled its carrier invoices. Every invoice line and invoice-level finding that was
not accepted needs a short memo for the carrier-relations colleague who has to act on it. Code has already
decided each disposition and computed every amount. You write the memo text for a batch of them.

## Your assignment

```json
{"batch_id": "memo-001", "inputs": ["/Users/vedangi/Downloads/freight-billing-reconciliation/runs/freight-2026-07/artefacts/memo-work/packets/memo-001.json"], "memo_ids": ["ALPINE-0726-line-005", "FALCON-2026-07A-line-011", "FALCON-2026-07B-line-008", "SAGAR-JUL-1-line-003", "SAGAR-JUL-1-line-009", "ALPINE-0726-finding-adjustment-undetermined-discount-5pct-from-13"], "packet": "/Users/vedangi/Downloads/freight-billing-reconciliation/runs/freight-2026-07/artefacts/memo-work/packets/memo-001.json"}
```

Read the packet at the path given by `packet`. Read no other file: an automatic audit of your tool calls
rejects the result if you do.

The packet contains:
- `memos`, one entry per memo to write: `memo_id`, `kind` (`line` or `finding`), `carrier_name`, `invoice`,
  the disposition and its `justification` (the reconciliation's reason), `contract_clause`, and the facts:
  for a line, `consignment_ref`, `shipment_id`, `billed_amount`, `expected_amount`, `delta`,
  `billed_charges`, `components`, `flags` and `evidence`; for a finding, `description`, `amount_impact`
  and `details`.
- `contracts`, per carrier: the contract file, agreement reference, term, and the text of every clause by id.

## Who reads the memo and what it must do

The reader works in carrier relations. They know the carriers and the contracts in general, but not the
reconciliation's internals. They will use one memo to act on one item: raise a dispute with the carrier,
or get a decision from the right person. A memo succeeds if they can act on it without opening anything else.

Each memo file starts with a table that code generates from the reconciliation report: disposition,
carrier, invoice and line, consignment, shipment record, billed amount, contract amount, difference and
contract clauses (for a finding: invoice, finding and amount impact). Do not repeat the table; refer to it
when useful ("the difference shown above").

## The four fields

- `headline` (at most 140 characters): what the problem is and where, in one line. Name the consignment or
  invoice.
- `summary` (at most 700 characters): what is wrong or uncertain, in plain words. Name the charge or
  attribute involved. Never use internal flag names: write "the contract has no rate for a consignment of
  this weight", not a code.
- `contract_basis` (at most 500 characters): which clause says what, briefly quoted or paraphrased with its
  clause number. For an escalation, say what the contract leaves open.
- `action` (at most 500 characters): the concrete next step, matching the disposition:
  - **dispute**: what to ask the carrier for (for example, to reissue the line at the contract amount or to
    issue a credit note for the difference shown above) and which clause to cite.
  - **escalate**: the decision that is needed, who should make it (finance or the contract owner), and what
    information would settle it.

## Rules

- Facts only from the packet. Do not speculate about why the carrier billed as it did.
- Figures: copy them exactly from the packet, or refer to the table. Never calculate (no sums, differences
  or percentages of your own). A check rejects any figure that is not in the packet.
- Name charges in plain words ("the freight charge", "the cold-chain premium", "the detention charge"), never
  by invoice column or code names such as `freight_rs` or `freight_incl_fuel`.
- Tone: factual, neutral and courteous. No blame, no promises about the outcome.
- Plain text only in every field: no markdown headings or tables.


## Output

Write one JSON file at exactly this path: /Users/vedangi/Downloads/freight-billing-reconciliation/runs/freight-2026-07/artefacts/branches/memo_fan-0000/memo-drafts.json

```json
{
  "_session_id": "<your session id>",
  "batch_id": "<batch_id from the assignment>",
  "memos": [
    {
      "memo_id": "<memo_id from the packet>",
      "headline": "<at most 140 characters>",
      "summary": "<at most 700 characters>",
      "contract_basis": "<at most 500 characters>",
      "action": "<at most 500 characters>"
    }
  ]
}
```

Exactly one entry for every memo in the packet. If a check rejects the file, you are told what failed; fix
the file in place.


---
Flowstate runtime context (added by the runtime, not part of the task):
- Your session id is 64b1af8f-ac81-49be-9b99-d2011445df88. Wherever the task asks for your session id or a "_session_id" value, use exactly this string.
- Your working directory is /Users/vedangi/Downloads/freight-billing-reconciliation/runs/freight-2026-07/artefacts/branches/memo_fan-0000.
- Write output files at exactly the paths the task gives. Outputs are validated automatically; missing or invalid outputs are rejected.
