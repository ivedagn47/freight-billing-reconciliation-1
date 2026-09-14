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
- Tone: factual, neutral and courteous. No blame, no promises about the outcome.
- Plain text only in every field: no markdown headings or tables.
