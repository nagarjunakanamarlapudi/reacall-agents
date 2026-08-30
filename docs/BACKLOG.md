# Backlog and Explicit Non-Goals

## Future work after the academic demo

| Area | Candidate expansion | Boundary to preserve |
|---|---|---|
| Connectors | Authorized ERP/WMS/POS/supplier integrations | Keep per-system authorization, provenance, and human approval. |
| Identity | Production authentication and role administration | Do not treat a UI role selection as production access control. |
| Operations | Real notifications and inventory actions | Require organization policy, integration testing, and accountable approval. |
| Regulatory sources | Hardened FSIS adapter and broader notices | Keep FSIS outside the current flagship dependency. |
| Scale | Multi-retailer/nationwide coordination | Preserve case isolation, auditability, and explicit ownership. |
| Monitoring | Scheduled follow-up and service reliability | Do not claim 24/7 operations from the demonstration. |

## Deliberately excluded from this submission

RecallOps does not connect to real operational systems, write real inventory holds, contact consumers, notify regulators, process real customer PII, make autonomous public-health/compliance decisions, offer production authentication, deploy a service, or use A2A. These are exclusions by design. The synthetic Northstar twin exists to make the safety controls repeatable without representing a real retailer.
