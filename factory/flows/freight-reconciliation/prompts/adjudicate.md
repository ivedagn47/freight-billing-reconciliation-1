# Adjudicate open reconciliation items

BlueFin Commerce reconciles carrier invoices against its shipment records and rate contracts. Code has
already matched every invoice line to a shipment, computed the amount each contract allows, and settled
every line its disposition policy could decide mechanically. The items in your batch are the ones policy
left open: for each, it offers exactly two dispositions, and you choose one.

You make a judgement, not a calculation. Every amount has been computed by code and is final; you do not
change, recompute or add amounts.

## Your assignment

```json
{item}
```

Read the packet at the path given by `packet`. Read no other file: an automatic audit of your tool calls
rejects the result if you do.

The packet contains:
- `items`, one per open line: `item_id`, `invoice`, `consignment_ref`, the matched `shipment_id` and the
  shipment record (`evidence.shipment`), `billed_amount`, `expected_amount` (what the contract allows,
  computed by code), `delta` (billed minus expected), `billed_charges`, the computed `components`,
  `flags` (what code found that needs attention, with details), `policy_basis` (why policy did not decide
  alone), and `options` (the two dispositions you may choose from).
- `contracts`, per carrier: the contract file, agreement reference, term, and the text of every clause by id.

## What the dispositions mean

- `accept`: BlueFin pays the line as billed.
- `dispute`: BlueFin withholds the difference and asks the carrier to correct the line.
- `escalate`: a person in finance decides; BlueFin neither pays nor disputes the line yet.

## How to decide

1. Choose one of the item's `options` and nothing else.
2. Decide from the packet only: the flags and their details, the shipment record, the billed charges, and
   the contract clauses.
3. Choose `dispute` only when a clause, read as written, shows that the carrier is not entitled to what the
   flags call into question. Cite that clause.
4. Choose `escalate` when the contract does not settle the question, or when the facts in the packet are
   incomplete or point in different directions, so that a person has to decide.
5. Choose `accept` only if it is offered and nothing in the flags affects what BlueFin owes.
6. Decide every item on its own facts; similar-looking items can differ.

## Output

Write one JSON file at exactly this path: {_run_artefact_dir}/adjudications.json

```json
{
  "_session_id": "<your session id>",
  "batch_id": "<batch_id from the assignment>",
  "decisions": [
    {
      "item_id": "<item_id from the packet>",
      "disposition": "<one of that item's options>",
      "clauses": ["<clause id>"],
      "justification": "<one to three sentences>"
    }
  ]
}
```

- Exactly one decision for every item in the packet.
- `clauses`: ids of the clauses in that item's carrier contract that support the decision (`[]` only if no
  clause bears on it). Code appends the clause references to your justification, so you need not repeat them.
- `justification`: at most 600 characters, in plain language for the carrier-relations team: what the
  issue is and why this disposition. Do not use internal flag names. If you mention a figure (an amount, a
  weight, a distance, a percentage), copy it exactly from the packet; a check rejects any figure that is not
  in the packet. Do no arithmetic.

If a check rejects the file, you are told what failed; fix the file in place.
