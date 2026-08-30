# Data Sources and Provenance

**Status:** implemented, deterministic data/provenance contract; end-to-end runtime verification remains in Task 11.

![Data provenance boundary](images/01_data_provenance.svg)

## Source register

| Source | Role | Provenance label | Critical-path status |
|---|---|---|---|
| openFDA Food Enforcement API | Recall notice lookup | `OFFICIAL — openFDA` | Live lookup plus frozen fallback |
| Frozen `H-1230-2026` snapshot | Reproducible flagship case | `OFFICIAL — openFDA snapshot` | Required offline fallback |
| FDA traceability guidance | Policy context | `OFFICIAL — FDA guidance` | Reference resource |
| GS1 EPCIS 2.0 | Event semantics | `OFFICIAL — GS1 reference` | Reference resource |
| USDA FoodData Central | Optional product enrichment | `OFFICIAL — USDA` | Optional only |
| Northstar Grocers data | Product, lot, event, inventory, task, receipt fixtures | `SYNTHETIC_RETAILER_DIGITAL_TWIN`; UI: `SYNTHETIC — ACADEMIC DEMO` | Required demo data |
| USDA FSIS recall API | Possible future adapter | `FUTURE — excluded from flagship` | Not a dependency |

## Public/synthetic separation

`H-1230-2026` is an official public recall used to extract a product/UPC/plant/Julian-date/geography/hazard predicate. Northstar Grocers is fictional. The synthetic data may show exact, probable, ambiguous, and rejected matches, but it does not establish any connection between Northstar and the real recall. This distinction follows each record into citations, tool responses, UI badges, and audit traces.

## Business data represented

A food-recall coordinator starts with a regulator's recall predicate, but containment depends on private operational records that a public API cannot provide. The Northstar twin therefore represents the business chain from product master and supplier receipt through distribution-center/store movement, sale, return, quarantine, disposal, inventory count, and facility acknowledgement. EPCIS-like parent-event links make both forward tracing ("where did this lot go?") and backward tracing ("which receipt did this store event descend from?") inspectable.

The committed portfolio is intentionally larger than the six-row teaching fixture while remaining easy to run locally:

| Record set | Count | Business purpose |
|---|---:|---|
| Products | 48 | Catalog filtering, near-UPC controls, and table pagination |
| Lots | 144 | Exact, probable, ambiguous, rejected, clean, and gapped outcomes |
| Facilities | 18 | Two distribution centers and sixteen stores |
| EPCIS-like events | 577 | Receiving, shipping/transfer, sale, return, quarantine, and disposal lineage |
| Inventory positions | 216 | Multi-facility stock and quantity reconciliation |
| Supplier shipments | 144 | One traceable inbound shipment per lot |
| Facility acknowledgements | 18 | Complete facility coverage, with `STORE-08` intentionally unresolved |

Six anchor lots remain hand-auditable for the flagship demo. In particular, `LOT-PROBABLE-160` has a zero-unit gap and can exercise safe closure after acknowledgement, while `LOT-EXACT-170` retains a 50-unit gap and must remain blocked. The other 138 lots provide realistic background volume without changing those anchor facts.

## Reproducibility

The public snapshot remains byte-for-byte frozen with SHA-256 `086c80b789959dc0612f4d94ca4f199da621158416784a3e1ed0eeeecc260aa9`. Synthetic schema `recallops.synthetic-retailer-digital-twin` version `1.1.0` is regenerated from pinned seed `20260830`; its current `dataset.json` SHA-256 is `6f60ce4a3119aae2d68b3ea3c5105d79cc0df9fd335c2c2132218a886f5c61d9`.

The manifest declares the schema, version, seed, source label, exact collection counts, file list, and raw-byte SHA-256. Loading fails closed on checksum or count drift, anchor mutation, unlabelled origin, malformed timezone, duplicate identifiers, missing foreign keys, lineage cycles, facility-continuity breaks, quantity-aggregate mismatches, or incomplete facility acknowledgements. Generating twice produces byte-identical dataset and manifest files.

## Data minimization

The academic twin excludes real customer PII. Customer-like fields are masked before model context and traces. Real ERP, WMS, POS, supplier, or store systems are not accessed.
