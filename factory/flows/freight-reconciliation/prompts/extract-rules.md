# Extract a carrier contract into a rate spec

You turn one carrier's prose rate contract into a **rate spec**: a JSON document in a fixed format that
deterministic code evaluates against BlueFin Commerce's shipment records. Code does all pricing and
matching. Your job is only to transcribe what the contract says into that format, faithfully and
completely, and to record plainly where the contract does not determine something.

Another worker is extracting the same contract independently. Code compares the two specs by how they
price shipments; if they disagree, both are discarded and the contract is extracted again. Read the
contract slowly and literally.

## Your assignment

```json
{item}
```

- `carrier`: the carrier id to put in the spec.
- `contract`: the contract (markdown). Read it in full.
- `clause_index`: the same contract split into clauses by code: `file`, `agreement_ref`, `term`, and
  `clauses`, each with its `id`, `section`, `text` and the `numbers` it contains. Cite clauses by these ids.
- `vocabulary`: the values that occur in BlueFin's shipment records for `service_level` and
  `special_handling`. Conditions and service levels in the spec must use these exact values.
- `invoice_charge_codes`: the charge codes that occur on this carrier's invoices. Components map the
  contract's charges onto these codes (see *Components*).
- `copy`, `round`, `inputs`: bookkeeping; they do not change the task.

Read only the two files named by `contract` and `clause_index`. Do not open, list or search any other file
or directory: an automatic audit of your tool calls rejects the spec if you do.

Write the rate spec as one JSON file at exactly this path: {_run_artefact_dir}/rate-spec.json

Set its `"_session_id"` to your session id (stated at the end of this prompt).

{include:prompts/reference/rate-spec-guide.md}

## The JSON Schema the spec must satisfy

```json
{include:definitions/rate-spec.json}
```

## Before you finish, check

1. Every clause id in the clause index appears in at least one `clauses` list somewhere in the spec.
2. Every number you wrote (rate, amount, percentage, band limit, constant, threshold) appears in a clause
   that the same element cites. You derived or converted no numbers.
3. Band ends follow the wording table exactly; no band was stretched to close a gap; gaps are listed.
4. Every condition value and every allowed service level is copied from `vocabulary`; every condition on a
   fact the records do not hold is kept, as `unrecorded`.
5. Every component's `charge_codes` follow the combined, generic and specific rules, using `invoice_charge_codes`.
6. Anything the building blocks cannot express is in `unrepresentable`, not approximated.
7. The file is valid JSON at the path above and carries your `_session_id`.

When you finish, automated checks validate the schema, trace every number to the clauses cited, and audit
which files you read. If a check fails you are told exactly what failed; fix the file in place.
