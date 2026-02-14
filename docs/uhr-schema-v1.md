# Unified Health Record (UHR)

# Schema Architecture Overview (v1)

---

## Status

- Version: 1.0
- Scope: Neutral Longitudinal Record Layer (Option B)
- FHIR Position: Compatible by Design, Not FHIR-Native
- Last Updated: 2026-02-14

---

# 1. Architectural Philosophy

UHR uses a strict relational, patient-centric schema with an immutable event model.

The relational database is the canonical domain model.

FHIR is implemented as:

- An import/export translation layer
- Not as the internal storage format

This ensures:

- Data integrity
- Long-term maintainability
- Strong auditability
- Enterprise compatibility
- Future FHIR-server capability without schema rewrite

---

# 2. Schema Overview

The schema is divided into the following logical layers:

1. Identity Layer
2. Consent & Access Layer
3. Medical Event Engine (Core)
4. Typed Medical Event Extensions
5. Audit & Transparency Layer
6. Interoperability Hooks (Future-Ready)
7. FHIR Server Extensions (Optional Future)

---

# 3. Identity Layer (Required in MVP)

## 3.1 Users (Authentication Layer)

Purpose: System login & access control.

```
users
- id (UUID, PK)
- email (unique)
- phone (unique)
- password_hash
- role (patient | doctor | admin)
- is_active
- deleted_at (nullable timestamp)
- retraction_reason (nullable text)
- created_at
```

Note: A User Account is not a medical identity.

---

## 3.2 Patients (Medical Identity)

FHIR Mapping: Patient

```
patients
- id (UUID, PK)
- user_id (nullable FK → users.id)
- mrn
- national_health_id
- first_name
- last_name
- gender
- birth_date
- created_at
```

Design Rules:

- A Patient Profile represents a single medical identity.
- A User may own multiple Patient Profiles (caregiver model).
- Medical data attaches to patient_id, not user_id.

---

## 3.3 Practitioners

FHIR Mapping: Practitioner

```
practitioners
- id (UUID, PK)
- user_id (nullable FK → users.id)
- full_name
- license_number
- specialization
- is_verified
- verification_source (manual | registry | national_gateway)
- created_at
```

Supports:

- Self-registered doctors
- Registry-imported doctors
- Future national registry linking

---

## 3.4 Organizations

FHIR Mapping: Organization

```
organizations
- id (UUID, PK)
- name
- type (hospital | clinic | lab)
- registration_number
- verified
```

---

## 3.5 Practitioner Roles

FHIR Mapping: PractitionerRole

```
practitioner_roles
- id (UUID, PK)
- practitioner_id (FK → practitioners.id)
- organization_id (FK → organizations.id)
- role_title
- start_date
- end_date
- is_active
```

Allows time-bound affiliations.

---

# 4. Consent & Access Layer (Required in MVP)

## 4.1 Consents

```
consents
- id (UUID, PK)
- patient_id (FK)
- granted_to_practitioner_id (nullable FK)
- granted_to_email (nullable)
- scope_type (all | date_range | data_type)
- data_type (nullable)
- start_date
- end_date
- is_active
- created_at
- revoked_at
```

Supports:

- Scope-bound consent
- Time-bound consent
- Immediate revocation

---

## 4.2 Share Links (Anonymous Doctor Access)

```
share_links
- id (UUID, PK)
- patient_id (FK)
- token (unique)
- validator_type (year_of_birth | pin)
- validator_hash
- expires_at
- is_used
- created_at
```

Implements validation challenge rule.

Staging Rule:

- Any medical_event created via share_link must default to visibility_status = pending_approval.
- pending_approval events are excluded from all standard API queries.
- Only the Patient-Owner may approve and transition them to visible or hidden.

---

# 5. Medical Event Engine (Core Domain Model)

This is the canonical model of UHR.

All medical data is stored as immutable events.

## 5.1 Base Table: medical_events

```
medical_events
- id (UUID, PK)
- patient_id (FK)

- event_type
  (visit | observation | condition |
   medication | procedure | document | second_opinion)

- clinical_timestamp
- system_timestamp

- source_type
  (patient | doctor | lab | system)

- source_practitioner_id (nullable FK)
- source_organization_id (nullable FK)

- verification_level
  (self_reported |
   patient_confirmed |
   provider_verified |
   digitally_verified)

Authority Matrix Rule:
- Only authenticated Practitioner accounts may generate provider_verified events.
- Patient-entered records describing professional actions remain self_reported.
- The system must never infer verification level from content alone.

- visibility_status (visible | hidden | pending_approval)

- created_by_user_id (FK)
- amends_event_id (nullable FK → medical_events.id)
- amendment_reason (nullable text)
- parent_event_id (nullable FK → medical_events.id)
- relationship_type (lifecycle | amendment | related | none)

- is_active
- created_at
```

Core Rules:

- Events are immutable.
- Corrections create amendment events (relationship_type = amendment).
- Lifecycle transitions (e.g., medication dose change or discontinuation) create new linked events (relationship_type = lifecycle) referencing parent_event_id.
- No hard deletes.
- clinical_timestamp must be stored as an offset-aware datetime (timezone-aware). Internal ordering uses absolute UTC sequence, while UI must display local event time for clinical context.
- Timeline ordered by clinical_timestamp (UTC sequence).

---

# 6. Typed Event Extensions (MVP Required)

Each extension references medical_event_id (1:1).

---

## 6.1 visit_events

FHIR: Encounter

```
visit_events
- medical_event_id (PK, FK)
- reason
- visit_type
- notes
```

---

## 6.2 observation_events

FHIR: Observation

```
observation_events
- medical_event_id (PK, FK)
- coding_system
- coding_code
- coding_display
- value_type
- value_quantity
- value_unit
- value_string
- reference_range
```

---

## 6.3 condition_events

FHIR: Condition

```
condition_events
- medical_event_id
- coding_system
- coding_code
- coding_display
- clinical_status
- onset_date
- abatement_date
```

---

## 6.4 medication_events

FHIR: MedicationRequest

```
medication_events
- medical_event_id
- medication_name
- dosage
- frequency
- route
- start_date
- end_date
- status
```

---

## 6.5 procedure_events

FHIR: Procedure

```
procedure_events
- medical_event_id
- coding_system
- coding_code
- coding_display
- performed_date
- notes
```

---

## 6.6 document_events

FHIR: DocumentReference

```
document_events
- medical_event_id (PK, FK)
- file_url
- file_type
- document_type
- checksum (required, SHA-256 or stronger)
- file_size_bytes
- storage_provider
```

Integrity Rule:

- The checksum must be computed at upload time.
- On every file retrieval, the stored checksum must be validated against the file contents.
- Any mismatch must trigger an integrity alert and block access until resolved.

```

---

## 6.7 second_opinion_events

FHIR: Communication

```

second_opinion_events

- medical_event_id
- doctor_name
- doctor_registration_number
- opinion_text
- approved_by_patient

```

Unapproved opinions are not visible in main timeline.

---

# 7. Audit Layer (Required in MVP)

```

audit_logs

- id (UUID, PK)
- user_id
- patient_id
- action (read | create | share | revoke | update)
- resource_type
- resource_id
- ip_address
- timestamp

```

Append-only.

---

# 8. Interoperability Hooks (Add Early, Use Later)

These fields allow future enterprise integration.

Add to medical_events:

```

external_system
external_resource_id
original_payload_hash

fhir_resource_type
fhir_logical_id
fhir_version_number

```

Purpose:

- Idempotent imports
- External syncing
- Future FHIR server support
- Version tracking

---

# 9. Enterprise Integration (Future Phase)

When integrating with hospitals:

- Implement FHIR import layer
- Implement FHIR export Bundle generator
- Implement terminology normalization
- Implement provenance tracking

No schema rewrite required.

---

# 10. Full FHIR Server (Optional Future)

Only required if UHR becomes a full FHIR server:

- CapabilityStatement endpoint
- FHIR search compliance
- Resource version history endpoints
- SMART-on-FHIR support
- Terminology services
- Subscription support

This layer is additive.

---

# 11. Architectural Position

UHR is:

A patient-owned longitudinal health record
with strict relational integrity
FHIR-compatible by design
FHIR-server-capable by extension

The relational core remains canonical.
Interoperability layers remain modular.

---

# 12. Architectural Invariants (Non-Negotiable System Rules)

The following invariants must never be violated, regardless of future features or integrations.

## 12.1 Immutability Invariant
Medical events must never be modified or hard-deleted after creation. Corrections must create new amendment events.

Soft Delete Rule:
Records are never physically deleted. If a record must be retracted, deleted_at must be set and retraction_reason must be recorded. Retractions must remain visible in audit logs.

## 12.2 Provenance Invariant
Every medical_event must retain:
- Original actor (created_by_user_id)
- source_type
- verification_level
- System timestamp

Amendment Transparency Rule:
If an event amends a prior event, amendment_reason must be recorded explaining why the change occurred (e.g., typographical correction, updated clinical evidence).

Provenance metadata must never be removed or overwritten.

## 12.3 Patient Anchor Invariant
All clinical resources must reference a valid patient_id. No orphan medical data is permitted.

## 12.4 Authority Invariant
Only authenticated Practitioner accounts may generate provider_verified events.
The system must never infer verification level from content alone.

## 12.5 Timeline Integrity Invariant
clinical_timestamp must be stored as an offset-aware datetime.
Timeline ordering must use absolute UTC sequence.
UI must display local event time for clinical context.

## 12.6 Consent Enforcement Invariant
All read and write operations must be evaluated against active consent rules.
Revoked consent must take effect immediately without altering audit history.

## 12.7 Staging Safety Invariant
Events created via share_link must default to visibility_status = pending_approval and must not appear in standard queries until approved by the Patient-Owner.

---

# 13. Indexing & Performance Considerations

To ensure scalability and enterprise-grade performance, the following indexing strategy must be implemented.

## 13.1 medical_events
Primary timeline query:
WHERE patient_id = ? ORDER BY clinical_timestamp DESC

Required index:
- INDEX (patient_id, clinical_timestamp DESC)

Additional recommended indexes:
- INDEX (event_type)
- INDEX (source_practitioner_id)
- INDEX (external_resource_id)

## 13.2 observation_events
Common queries:
WHERE coding_code = ?
WHERE patient_id = ? AND coding_code = ?

Required index:
- INDEX (coding_code)

## 13.3 medication_events
Common queries:
WHERE patient_id = ? AND status = 'active'

Required index:
- INDEX (status)
- Composite INDEX via join on medical_events(patient_id)

## 13.4 audit_logs
Common queries:
WHERE patient_id = ? ORDER BY timestamp DESC

Required index:
- INDEX (patient_id, timestamp DESC)

---

Performance Principles:

- Timeline queries must remain sub-second for typical patient history.
- No clinical query should require full-table scans.
- Index strategy must evolve with real query patterns.
- Referential integrity must not be sacrificed for performance.

---

```
