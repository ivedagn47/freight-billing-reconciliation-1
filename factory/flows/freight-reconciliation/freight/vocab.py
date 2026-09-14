"""Shared vocabularies: invoice charge codes and the shipment fields a rate spec may use.

Parsers map each carrier's charge labels onto CHARGE_CODES. Rate specs name the charge codes
each component accounts for. Neither side invents codes, so billed charges can be matched to
contract components without free-text comparison.
"""

CHARGE_CODES = {
    "freight": "Transport charge for the consignment (any surcharges the invoice does not itemise are inside it)",
    "freight_incl_fuel": "Transport charge the invoice states includes the fuel surcharge",
    "fuel_surcharge": "Fuel surcharge itemised separately",
    "express_premium": "Express service premium itemised separately",
    "cold_chain_premium": "Refrigerated or cold-chain premium",
    "residential_delivery": "Delivery to a residential address",
    "fragile_handling": "Protected handling of fragile goods",
    "handling": "A handling fee whose reason the invoice does not state",
    "detention": "Detention or waiting time at pickup or delivery",
    "credit": "Credit issued on a credit note",
    "other": "A charge label the parser does not recognise (the raw label is kept)",
}

TRANSPORT_CODES = ("freight", "freight_incl_fuel", "fuel_surcharge", "express_premium")

# Shipment record fields a rate spec may reference, and their types.
SHIPMENT_FIELDS = {
    "billed_weight_kg": "number",
    "distance_km": "number",
    "declared_value_inr": "number",
    "service_level": "string",
    "special_handling": "list",
    "delivery_status": "string",
    "ship_date": "date",
}
