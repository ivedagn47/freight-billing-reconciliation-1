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
{"carrier": "falcon", "clause_index": "/Users/vedangi/Downloads/freight-billing-reconciliation/runs/freight-2026-07/artefacts/rules/clauses/falcon.json", "contract": "/Users/vedangi/Downloads/freight-billing-reconciliation/data/contracts/falcon-freight.md", "copy": "b", "inputs": ["/Users/vedangi/Downloads/freight-billing-reconciliation/data/contracts/falcon-freight.md", "/Users/vedangi/Downloads/freight-billing-reconciliation/runs/freight-2026-07/artefacts/rules/clauses/falcon.json"], "invoice_charge_codes": ["detention", "express_premium", "freight", "freight_incl_fuel", "fuel_surcharge", "residential_delivery"], "round": 1, "vocabulary": {"service_level": ["express", "standard"], "special_handling": ["cold_chain", "fragile", "residential"]}}
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

Write the rate spec as one JSON file at exactly this path: /Users/vedangi/Downloads/freight-billing-reconciliation/runs/freight-2026-07/artefacts/branches/extract_fan-0003/rate-spec.json

Set its `"_session_id"` to your session id (stated at the end of this prompt).

## How the rate spec is used

For every invoice line, code finds BlueFin's shipment record and evaluates the spec against it:

1. `quantities` are computed in order (for example a chargeable weight).
2. `components` are evaluated in order. A component with a `when` condition applies only when the
   shipment meets it. The contract amount for the consignment is the sum of the applied components,
   rounded once to the paisa.
3. If a banded rate has no band containing the shipment's value, the amount is **undetermined** and a
   person decides. Nothing is guessed.
4. Each charge billed on the invoice carries a charge code. A code that no component lists is treated as
   a charge the contract does not provide for. A code whose components all do not apply to the shipment is
   treated as a charge the shipment does not qualify for. If whether they apply depends on a fact the
   shipment records do not hold (see *Conditions*), a person decides.
5. A shipment whose `service_level` is not in `service_levels.allowed` is reported as a service the
   agreement does not offer. A ship date outside `term` is reported as outside the agreement.
6. `invoice_adjustments` apply to a whole invoice, based on how many of the carrier's consignments shipped
   in the invoice's billing month, as a percentage of the invoice's contract total.

## Principles

1. **Transcribe; do not calculate.** Every number in the spec must appear in a clause that the element
   cites. Write percentages as the percentage (a 7% surcharge is `"percent": 7`), rates as the rate per
   unit, and amounts as stated. Never combine, convert or derive numbers.
2. **Never fill a gap.** If the contract does not say what happens (a value between two bands, a value
   above the last band, a case it calls "quoted separately"), do not stretch a band or invent a rate. Leave
   it uncovered and describe it in `gaps`.
3. **Account for every clause.** Each clause id must be cited at least once: by the element it prices, or
   in `gaps`, `non_pricing` (with the reason it does not affect a price) or `unrepresentable`.
4. **Do not approximate what you cannot express.** If a pricing term needs something these building
   blocks lack (an amount that depends on an unrecorded quantity such as waiting hours, a time-based rule, a
   minimum invoice charge, ...), describe it in `unrepresentable` and cite it. A *condition* on a fact the
   shipment records do not hold is expressible: use `unrecorded` (see *Conditions*), never drop it. A person then extends the format. An empty list means you are sure
   the spec prices everything the contract prices.
5. **Use the contract's own words for boundaries** (see *Bands*), not what seems commercially intended.

## Top-level fields

| Field | Value |
|---|---|
| `spec_version` | `1` |
| `carrier` | the assignment's `carrier` |
| `contract_file` | the clause index's `file` |
| `agreement_ref` | the clause index's `agreement_ref` |
| `term` | `start` and `end` (YYYY-MM-DD) as the header states them; `clauses: ["header"]` |
| `quantities`, `components`, `service_levels`, `invoice_adjustments` | see below |
| `gaps`, `non_pricing`, `unrepresentable` | see below (use `[]` when there are none) |

Citations: `"header"` is the agreement header (reference, parties, service, term). Every other id is a
clause number from the clause index, as a string (`"3"`).

## Shipment fields

| Field | Type | Meaning |
|---|---|---|
| `billed_weight_kg` | number | The consignment's weight in kg in BlueFin's booking record. It is the only weight recorded; use it wherever the contract prices by the consignment's weight. |
| `distance_km` | number | The booked lane distance in km. |
| `declared_value_inr` | number | The declared value of the goods in INR. |
| `service_level` | one value | The booked service; values are listed in `vocabulary.service_level`. |
| `special_handling` | list of flags | Booking flags; values are listed in `vocabulary.special_handling`. A consignment "booked as X" or "flagged X" has the flag with that meaning. |

## Quantities

A named value derived from fields and constants, usable by later quantities and by components.

```json
{"name": "chargeable_kg", "op": "max", "args": [{"field": "billed_weight_kg"}, {"const": 40}], "clauses": ["2"]}
```

`op` is `max` ("the higher of", "whichever is greater", "a minimum of") or `min` ("the lower of", "capped
at"). `args` are two or more of `{"field": ...}`, `{"quantity": <earlier quantity>}`, `{"const": number}`.

## Components

```json
{"name": "base_freight", "kind": "freight", "charge_codes": ["freight", "freight_incl_fuel"],
 "calc": {"op": "per_unit", "basis": {"quantity": "chargeable_kg"}, "rate": 3.4}, "clauses": ["2"]}
```

- `name`: lower_snake_case, unique in the spec.
- `kind`: `freight` (the basic carriage charge), `surcharge` (fuel and similar), `premium` (a service
  premium such as a faster or refrigerated service), `accessorial` (a per-consignment extra service).
- `calc` is one of:

| `op` | Fields | Amount |
|---|---|---|
| `flat` | `amount` | the amount |
| `per_unit` | `basis`, `rate` | basis × rate |
| `banded_rate` | `basis`, `select_by`, `bands` | basis × the rate of the one band containing `select_by` |
| `percent_of` | `percent`, `of` (names of earlier components) | percent % of the sum of those components (a component that does not apply counts as 0) |

  `basis` and `select_by` are `{"field": ...}`, `{"quantity": ...}` or `{"const": ...}`. They may differ:
  a rate per km chosen by the weight band has `basis` = distance and `select_by` = weight.
- `when` (optional): the condition under which the component applies (see *Conditions*). Omit it for
  components that always apply.
- `charge_codes`: the invoice charge codes under which this component's money can be billed. The
  assignment's `invoice_charge_codes` are the codes this carrier's invoices actually use; only those affect
  pricing, so take the codes from that list:
  - A **combined** code belongs to every component it combines: transport billed "including fuel"
    (`freight_incl_fuel`) belongs to both the freight component and the fuel surcharge component.
  - A **generic** code names a kind of charge without saying which contract term it is for. List it on every
    component of that kind: `freight` on the basic carriage component(s), and `handling` on every
    handling-type accessorial (fragile or protected handling, loading and the like), so that a billed
    handling fee is checked against what the shipment qualifies for.
  - A **specific** code (`residential_delivery`, `cold_chain_premium`, ...) goes on the component it names.
  - A component whose charge has no code of its own in `invoice_charge_codes` is billed inside the transport
    charge: list the transport code(s) from the list on it.
- Order matters only for `percent_of`, which may name earlier components only.

Charge codes:

| Code | Meaning |
|---|---|
| `freight` | Transport charge for the consignment (any surcharges the invoice does not itemise are inside it) |
| `freight_incl_fuel` | Transport charge the invoice states includes the fuel surcharge |
| `fuel_surcharge` | Fuel surcharge itemised separately |
| `express_premium` | Express service premium itemised separately |
| `cold_chain_premium` | Refrigerated or cold-chain premium |
| `residential_delivery` | Delivery to a residential address |
| `fragile_handling` | Protected handling of fragile goods |
| `handling` | A handling fee whose reason the invoice does not state |
| `detention` | Detention or waiting time at pickup or delivery |

Do not list a code for a charge the contract does not provide for; code treats it as uncontracted.

## Conditions

```json
{"service_level": "<value>"}
{"special_handling_includes": "<flag>"}
{"unrecorded": "<the fact, in the contract's words>"}
{"all": [<condition>, ...]}   {"any": [<condition>, ...]}   {"not": <condition>}
```

`service_level` and `special_handling_includes` values must be copied from `vocabulary`.

Use `unrecorded` for a fact the contract conditions on that BlueFin's shipment records do not hold (the
shipment fields above are everything that is recorded). Keep the whole condition: a charge due "where the
booking is flagged X, or where the goods are Y", with Y not recorded, is
`{"any": [{"special_handling_includes": "X"}, {"unrecorded": "the goods are Y"}]}`. Code never treats an
unrecorded fact as true or false: it prices what the records establish, and a billed charge that only the
unrecorded fact could justify goes to a person. Dropping the unrecorded part would make code dispute charges
the contract may allow; turning the whole term into `unrepresentable` would stop every run for this carrier.

## Bands

Each band has `min`, `min_inclusive`, `max`, `max_inclusive` and `rate`. Encode each end exactly as worded:

| Contract wording | Encoding |
|---|---|
| "under X", "below X", "less than X" | `max` X, `max_inclusive` false |
| "up to X", "up to and including X", "not exceeding X", "X or less", "at most X" | `max` X, `max_inclusive` true |
| "over X", "above X", "more than X", "exceeding X" | `min` X, `min_inclusive` false |
| "X or more", "at least X", "from X", "X and above" | `min` X, `min_inclusive` true |
| "X to Y", "X–Y", "between X and Y" | `min` X inclusive, `max` Y inclusive |
| no lower (or upper) limit stated | `null`, with `_inclusive` false |

Bands must not overlap. Never add a band the contract does not state, never move an end to meet the next
band, and never leave an end open (`null`) unless the contract states no limit on that side. If the
wording leaves values uncovered (between two bands, or beyond the last), leave them uncovered and add a
`gaps` entry describing the uncovered range.

## Service levels

`service_levels.allowed` lists the services the agreement offers, as `vocabulary` values, with the
clauses that establish them. If the contract says a service is not offered, leave it out. If a clause
prices a service, include it. If the contract never limits services, include every `vocabulary` value and
cite the clauses (or `"header"`) describing the service.

## Invoice adjustments

```json
{"name": "volume_discount", "kind": "discount",
 "when": {"consignments_in_billing_month": {"gte": 30}},
 "calc": {"op": "percent_of_invoice_expected", "percent": 2.5}, "clauses": ["7"]}
```

The count is the carrier's consignments shipped in the invoice's billing month. Wording: "more than N" →
`gt`, "N or more" / "at least N" → `gte`, "fewer than N" → `lt`, "N or fewer" / "up to N" → `lte`.
A discount described any other way (a fixed amount, tiers, a different base) is `unrepresentable`.

## Gaps, non-pricing clauses, unrepresentable terms

```json
"gaps": [{"description": "consignments over 3,000 kg are not rated", "clauses": ["3"]}],
"non_pricing": [{"clauses": ["8"], "reason": "payment terms"}],
"unrepresentable": [{"description": "waiting time charged per hour after two hours", "clauses": ["6"]}]
```

`gaps` are informational: they record what the contract leaves undetermined. `non_pricing` accounts for
clauses that do not change what a consignment costs (invoicing cycle, payment terms, dispute procedure,
notice periods, general statements). A clause can be both priced and noted as a gap.

## Worked example (an invented contract, not the one you are extracting)

> **Agreement ref:** EX/77 · **Term:** 1 April 2030 – 31 March 2032
> 1. Freight is charged per km of lane distance at a rate set by the consignment's weight:
>    - up to 800 kg: ₹11.00 per km
>    - over 800 kg and up to 4,000 kg: ₹14.50 per km
>    Heavier consignments are rated by separate agreement.
> 2. A fuel surcharge of 9% applies to freight.
> 3. Consignments flagged fragile incur a handling charge of ₹400.00.
> 4. From the 40th consignment in a calendar month, the month's invoice is discounted by 3%.
> 5. Invoices are payable within 45 days.

```json
{
  "_session_id": "<your session id>",
  "spec_version": 1, "carrier": "example", "contract_file": "example.md", "agreement_ref": "EX/77",
  "term": {"start": "2030-04-01", "end": "2032-03-31", "clauses": ["header"]},
  "quantities": [],
  "components": [
    {"name": "freight", "kind": "freight", "charge_codes": ["freight", "freight_incl_fuel"], "clauses": ["1"],
     "calc": {"op": "banded_rate", "basis": {"field": "distance_km"}, "select_by": {"field": "billed_weight_kg"},
              "bands": [{"min": null, "min_inclusive": false, "max": 800, "max_inclusive": true, "rate": 11},
                        {"min": 800, "min_inclusive": false, "max": 4000, "max_inclusive": true, "rate": 14.5}]}},
    {"name": "fuel", "kind": "surcharge", "charge_codes": ["fuel_surcharge", "freight_incl_fuel"], "clauses": ["2"],
     "calc": {"op": "percent_of", "percent": 9, "of": ["freight"]}},
    {"name": "fragile_handling", "kind": "accessorial", "charge_codes": ["fragile_handling"], "clauses": ["3"],
     "when": {"special_handling_includes": "fragile"}, "calc": {"op": "flat", "amount": 400}}
  ],
  "service_levels": {"allowed": ["standard"], "clauses": ["header"]},
  "invoice_adjustments": [
    {"name": "volume_discount", "kind": "discount", "clauses": ["4"],
     "when": {"consignments_in_billing_month": {"gte": 40}},
     "calc": {"op": "percent_of_invoice_expected", "percent": 3}}
  ],
  "gaps": [{"description": "consignments over 4,000 kg are rated by separate agreement", "clauses": ["1"]}],
  "non_pricing": [{"clauses": ["5"], "reason": "payment terms"}],
  "unrepresentable": []
}
```

(In this invented example the shipment vocabulary is assumed to contain only `standard` service and a
`fragile` flag, and the header is assumed to state a standard service.)


## The JSON Schema the spec must satisfy

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "Rate Spec",
  "description": "A contract's pricing rules in a fixed, machine-checkable form. Every element cites the clause(s) it comes from. The pricing engine evaluates it against shipment records only; nothing here is free text that affects a price.",
  "type": "object",
  "additionalProperties": false,
  "required": ["spec_version", "carrier", "contract_file", "agreement_ref", "term", "quantities", "components",
               "service_levels", "invoice_adjustments", "gaps", "non_pricing", "unrepresentable"],
  "properties": {
    "_session_id": { "type": "string", "minLength": 1 },
    "spec_version": { "const": 1 },
    "carrier": { "type": "string", "pattern": "^[a-z][a-z0-9_-]*$" },
    "contract_file": { "type": "string", "minLength": 1 },
    "agreement_ref": { "type": ["string", "null"] },
    "term": {
      "type": "object",
      "additionalProperties": false,
      "required": ["start", "end", "clauses"],
      "properties": {
        "start": { "type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$" },
        "end": { "type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$" },
        "clauses": { "$ref": "#/$defs/clauses" }
      }
    },
    "quantities": { "type": "array", "items": { "$ref": "#/$defs/quantity" } },
    "components": { "type": "array", "minItems": 1, "items": { "$ref": "#/$defs/component" } },
    "service_levels": {
      "type": "object",
      "additionalProperties": false,
      "required": ["allowed", "clauses"],
      "properties": {
        "allowed": { "type": "array", "minItems": 1, "uniqueItems": true, "items": { "type": "string", "minLength": 1 } },
        "clauses": { "$ref": "#/$defs/clauses" }
      }
    },
    "invoice_adjustments": { "type": "array", "items": { "$ref": "#/$defs/adjustment" } },
    "gaps": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["description", "clauses"],
        "properties": {
          "description": { "type": "string", "minLength": 1 },
          "clauses": { "$ref": "#/$defs/clauses" }
        }
      }
    },
    "non_pricing": {
      "description": "Clauses that do not affect what a consignment costs (payment terms, notice periods, ...), each with the reason. Together with every other citation, every clause of the contract is accounted for.",
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["clauses", "reason"],
        "properties": {
          "clauses": { "$ref": "#/$defs/clauses" },
          "reason": { "type": "string", "minLength": 1 }
        }
      }
    },
    "unrepresentable": {
      "description": "Pricing terms these building blocks cannot express. Any entry stops the run for a human: the spec would otherwise under-state the contract.",
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["description", "clauses"],
        "properties": {
          "description": { "type": "string", "minLength": 1 },
          "clauses": { "$ref": "#/$defs/clauses" }
        }
      }
    }
  },
  "$defs": {
    "name": { "type": "string", "pattern": "^[a-z][a-z0-9_]*$" },
    "clauses": { "type": "array", "minItems": 1, "uniqueItems": true, "items": { "type": "string", "pattern": "^(header|[0-9]+)$" } },
    "amount": { "type": "number", "minimum": 0 },
    "value_ref": {
      "oneOf": [
        { "type": "object", "additionalProperties": false, "required": ["field"],
          "properties": { "field": { "enum": ["billed_weight_kg", "declared_value_inr", "distance_km"] } } },
        { "type": "object", "additionalProperties": false, "required": ["quantity"],
          "properties": { "quantity": { "$ref": "#/$defs/name" } } },
        { "type": "object", "additionalProperties": false, "required": ["const"],
          "properties": { "const": { "$ref": "#/$defs/amount" } } }
      ]
    },
    "quantity": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name", "op", "args", "clauses"],
      "properties": {
        "name": { "$ref": "#/$defs/name" },
        "op": { "enum": ["max", "min"] },
        "args": { "type": "array", "minItems": 2, "items": { "$ref": "#/$defs/value_ref" } },
        "description": { "type": "string" },
        "clauses": { "$ref": "#/$defs/clauses" }
      }
    },
    "band": {
      "type": "object",
      "additionalProperties": false,
      "required": ["min", "min_inclusive", "max", "max_inclusive", "rate"],
      "properties": {
        "min": { "type": ["number", "null"], "minimum": 0 },
        "min_inclusive": { "type": "boolean" },
        "max": { "type": ["number", "null"], "minimum": 0 },
        "max_inclusive": { "type": "boolean" },
        "rate": { "$ref": "#/$defs/amount" }
      }
    },
    "calc": {
      "oneOf": [
        { "type": "object", "additionalProperties": false, "required": ["op", "amount"],
          "properties": { "op": { "const": "flat" }, "amount": { "$ref": "#/$defs/amount" } } },
        { "type": "object", "additionalProperties": false, "required": ["op", "basis", "rate"],
          "properties": { "op": { "const": "per_unit" }, "basis": { "$ref": "#/$defs/value_ref" },
                          "rate": { "$ref": "#/$defs/amount" } } },
        { "type": "object", "additionalProperties": false, "required": ["op", "basis", "select_by", "bands"],
          "properties": { "op": { "const": "banded_rate" }, "basis": { "$ref": "#/$defs/value_ref" },
                          "select_by": { "$ref": "#/$defs/value_ref" },
                          "bands": { "type": "array", "minItems": 1, "items": { "$ref": "#/$defs/band" } } } },
        { "type": "object", "additionalProperties": false, "required": ["op", "percent", "of"],
          "properties": { "op": { "const": "percent_of" }, "percent": { "type": "number", "minimum": 0, "maximum": 1000 },
                          "of": { "type": "array", "minItems": 1, "uniqueItems": true, "items": { "$ref": "#/$defs/name" } } } }
      ]
    },
    "condition": {
      "oneOf": [
        { "type": "object", "additionalProperties": false, "required": ["service_level"],
          "properties": { "service_level": { "type": "string", "minLength": 1 } } },
        { "type": "object", "additionalProperties": false, "required": ["special_handling_includes"],
          "properties": { "special_handling_includes": { "type": "string", "minLength": 1 } } },
        { "type": "object", "additionalProperties": false, "required": ["unrecorded"],
          "description": "A fact the contract conditions on that BlueFin's shipment records do not hold. It is neither true nor false: pricing excludes what depends on it, and a billed charge that only it could justify goes to a person.",
          "properties": { "unrecorded": { "type": "string", "minLength": 1 } } },
        { "type": "object", "additionalProperties": false, "required": ["all"],
          "properties": { "all": { "type": "array", "minItems": 1, "items": { "$ref": "#/$defs/condition" } } } },
        { "type": "object", "additionalProperties": false, "required": ["any"],
          "properties": { "any": { "type": "array", "minItems": 1, "items": { "$ref": "#/$defs/condition" } } } },
        { "type": "object", "additionalProperties": false, "required": ["not"],
          "properties": { "not": { "$ref": "#/$defs/condition" } } }
      ]
    },
    "component": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name", "kind", "charge_codes", "calc", "clauses"],
      "properties": {
        "name": { "$ref": "#/$defs/name" },
        "kind": { "enum": ["freight", "surcharge", "premium", "accessorial"] },
        "charge_codes": {
          "type": "array", "minItems": 1, "uniqueItems": true,
          "items": { "enum": ["freight", "freight_incl_fuel", "fuel_surcharge", "express_premium", "cold_chain_premium",
                              "residential_delivery", "fragile_handling", "handling", "detention"] }
        },
        "when": { "$ref": "#/$defs/condition" },
        "calc": { "$ref": "#/$defs/calc" },
        "description": { "type": "string" },
        "clauses": { "$ref": "#/$defs/clauses" }
      }
    },
    "adjustment": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name", "kind", "when", "calc", "clauses"],
      "properties": {
        "name": { "$ref": "#/$defs/name" },
        "kind": { "enum": ["discount"] },
        "when": {
          "type": "object", "additionalProperties": false, "required": ["consignments_in_billing_month"],
          "properties": {
            "consignments_in_billing_month": {
              "type": "object", "additionalProperties": false, "minProperties": 1, "maxProperties": 1,
              "properties": { "gt": { "type": "integer", "minimum": 0 }, "gte": { "type": "integer", "minimum": 0 },
                              "lt": { "type": "integer", "minimum": 0 }, "lte": { "type": "integer", "minimum": 0 } }
            }
          }
        },
        "calc": {
          "type": "object", "additionalProperties": false, "required": ["op", "percent"],
          "properties": { "op": { "const": "percent_of_invoice_expected" },
                          "percent": { "type": "number", "exclusiveMinimum": 0, "maximum": 100 } }
        },
        "description": { "type": "string" },
        "clauses": { "$ref": "#/$defs/clauses" }
      }
    }
  }
}

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


---
Flowstate runtime context (added by the runtime, not part of the task):
- Your session id is d424c9e2-9a24-4eca-9fe1-eecfd63f7c96. Wherever the task asks for your session id or a "_session_id" value, use exactly this string.
- Your working directory is /Users/vedangi/Downloads/freight-billing-reconciliation/runs/freight-2026-07/artefacts/branches/extract_fan-0003.
- Write output files at exactly the paths the task gives. Outputs are validated automatically; missing or invalid outputs are rejected.
