# Write reconciliation memos

BlueFin Commerce has reconciled its carrier invoices. Every invoice line and invoice-level finding that was
not accepted needs a short memo for the carrier-relations colleague who has to act on it. Code has already
decided each disposition and computed every amount. You write the memo text for a batch of them.

## Your assignment

```json
{item}
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

{include:prompts/reference/memo-style.md}

## Output

Write one JSON file at exactly this path: {_run_artefact_dir}/memo-drafts.json

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
