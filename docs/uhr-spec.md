# Unified Health Record (UHR)

## Purpose of this Document

This document defines the **Unified Health Record (UHR)** — an open-source, patient-owned healthcare record system.

It is a **frozen execution blueprint**, written _before implementation_, to ensure real-world usability, safety, and long-term correctness for patients and doctors.

---

## 1. Problem Statement

Patients often visit multiple doctors across private and government hospitals. Their medical data becomes fragmented across:

- Paper files
- PDFs and scanned reports
- Messaging apps
- Personal memory

Doctors rarely see a complete medical history — including past diagnoses, medications, surgeries, or test trends.

This leads to:

- Incomplete diagnosis
- Repeated tests
- Unsafe prescriptions
- Poor continuity of care

**The core issue is information loss, not lack of medical expertise.**

---

## 2. Product Goal (North Star)

Create a **patient-owned digital medical timeline** that preserves complete health history and enables doctors to make informed decisions using **explicit, consent-based access**.

---

## 3. Core Principles (Non-Negotiable)

- Patient owns the data
- Timeline over tables
- Consent-first sharing
- Low friction for doctors
- Manual digitisation supported
- Open source, community-driven
- Privacy by default

---

## 4. User Roles

### Patient (Primary Owner)

- Creates and owns the UHR account
- Adds and uploads medical records
- Controls sharing and consent
- Can revoke access at any time

### Caregiver & Dependent Support

UHR separates **User Accounts** from **Patient Profiles**.

- A Patient Profile represents a single medical identity.
- A User Account may:
  - Own a Patient Profile (self)
  - Be granted caregiver access to another Patient Profile

Caregiver access must:

- Be explicitly granted
- Be scope-bound
- Be revocable
- Appear in audit logs

Consent rules apply at the **Patient Profile** level, not the User Account level.

### Doctor

Doctors interact with the system in two modes:

#### Registered Doctor

- Identified by name and optional affiliation
- Can add diagnoses, medications, and procedures
- Can provide second opinions

#### Anonymous / Remote Doctor

- Accesses records via secure, time-limited sharing link
- No mandatory login required
- Can submit **second-opinion notes only**
- Contributions remain **unverified and invisible** until patient approval
- **Validation Challenge Required**:
  - Access must include a secondary validator
  - Example: patient year of birth or a short PIN
  - Prevents unauthorized use if the link is intercepted

---

## 5. Scope Definition

### Medical Data Types

- Visits
- Test results
- Diagnoses
- Medications _(acute, chronic, PRN, discontinued)_
- Surgeries / procedures
- Documents _(reports, prescriptions)_
- Second opinions _(remote or in-person)_

### Explicit Exclusions (for now)

- Billing
- Insurance
- Hospital inventory
- AI diagnosis
- Emergency monitoring

---

## 6. High-Level System Flow

- Patient creates and maintains a Unified Health Record.
- Medical data is structured into immutable Medical Events.
- Events are ordered chronologically by clinical timestamp.
- Patient grants consent-based access to doctors.
- Doctors review timeline and may add attributed contributions.
- All access and modifications generate immutable audit entries.
- Patient retains control and may revoke access at any time.

---

## 7. Consent Model (Operational Rules)

Consent is **explicit, time-bound, and scope-bound**.

### Consent can be granted for:

- Entire timeline
- Specific date ranges
- Specific data types _(e.g., tests only)_

### Consent revocation:

- Takes effect immediately

- Does not erase audit history

- Emergency override access is **explicitly not supported** in the MVP

### Anonymous / Remote Doctor Access (Validation Challenge)

Anonymous doctor access requires **two conditions**:

1. A secure, time-limited sharing link
2. A secondary validator supplied by the patient:
   - Year of birth **or**
   - 4–6 digit PIN

This ensures:

- Intercepted or forwarded links cannot be misused
- The doctor accessing the record is actually present with the patient
- Legal and ethical protection for both parties

---

## 8. Health Stats & Data Freshness

### Purpose

Health Stats provide patients and doctors a quick snapshot of which medical tests exist and how recent they are.

### What Health Stats Show

For each test:

- Test name
- Last recorded value _(if available)_
- Date of last test
- Data age _(human-readable)_
- Source _(lab / hospital / patient upload)_
- Verification status

### Data Freshness Labels

- **Recent**: ≤ 3 months
- **Moderately Old**: 3–12 months
- **Old**: > 12 months

Health Stats are **informational only** and never replace clinical judgment.

---

## 9. Medical Events & Longitudinal Data Model (Critical Rule)

All entries in the UHR timeline are modeled as **Medical Events**.

### Base Medical Event Standard

Every timeline entry _(visit, test, diagnosis, medication, procedure, document, note)_ shares a common base structure:

- Event ID
- Patient ID
- Event type
- **Clinical timestamp** — when the event occurred in real life
- **System timestamp** — when the event was recorded in UHR
- Source type _(patient / doctor / lab / system)_
- Verification status _(verified / unverified)_
- Visibility status _(visible / hidden by patient)_
- Created by _(user or anonymous share)_

This separation ensures:

- Accurate clinical timelines
- Safe backdated uploads
- Reliable audits

### Longitudinal Rule

- Medical data is **time-series by default**
- Events are immutable
- Corrections create new amendment events
- Historical data is never overwritten

Health Stats and summary views compute from events; the **full event history is always preserved**.

### Medication Lifecycle Rule

Medications are modeled as **time-bound Medical Events**.

A medication lifecycle is represented through multiple immutable events:

- **Medication Started** — creates the initial medication event.
- **Medication Modified** — creates a new amendment event referencing the original.
- **Medication Discontinued** — creates a new event referencing the original medication event.

Existing medication events are never overwritten.

This ensures:

- Accurate longitudinal medication history
- Clear determination of active vs historical medications
- Medico-legal traceability

---

## 10. Edge Case Handling

UHR explicitly preserves real-world data messiness:

- Same test from different labs → stored separately
- Conflicting values → both preserved
- Wrong upload → corrected via amendment
- Missing units → allowed, marked incomplete
- Different units → stored as-is
- Backdated reports → ordered by **clinical date**, not upload date

The system preserves **information fidelity over forced normalization**.

---

## 11. Corrections, Disputes & Amendments

- Patients can flag records as disputed and add context
- Doctors can submit corrections as amendment events
- Original records are never deleted
- All changes are attributed and auditable

---

## 12. Audit & Transparency

Every read or write action generates an **audit entry**.

### Patients can view:

- Who accessed their data
- When

### Doctors can view:

- Attribution for every medical entry

Audit logs are **append-only and immutable**.

---

## 13. Data Hiding, Visibility & Clinical Disclosure

- Medical records are never deleted
- Patients may:
  - Hide records from default views
  - Exclude records from shared access

### Clinical Safety Disclosure Rule

When any record is hidden, all doctor views must display a persistent banner:

> _“Note: This patient timeline has been filtered by the patient. Some history may be hidden.”_

This ensures:

- Doctors are aware of incomplete context
- Legal and ethical protection for clinicians

Account deletion deactivates access but preserves data integrity and audit history.

---

## 14. Timeline Ordering, Dual Timestamping & Trust Signals

Timeline ordering controls **display only**, not medical correctness.

### Dual-Timestamp Requirement

Every Medical Event stores two distinct timestamps:

- **Clinical Date** — used for timeline ordering
- **System Date** — used for audits and traceability

### Ordering Rules

- Primary order: Clinical Date
- Secondary order: System Date

### Trust & Verification Signals

- Source type _(lab / doctor / patient)_
- Verification status _(see levels below)_
- Supporting evidence _(PDF / image / none)_

Conflicting events coexist. Doctors apply clinical judgment using these signals.

### Verification Levels

Verification status must clearly distinguish:

- **Self-Reported** — manually entered by patient
- **Patient-Confirmed** — OCR-extracted and confirmed by patient
- **Provider-Verified** — confirmed by a registered doctor
- **Digitally Verified** — received via structured digital integration (e.g., lab system)

Verification level must be clearly visible in all doctor views.

No verification level implies clinical accuracy.

---

## 15. Development Timeline

### Phase 0 — Product Definition (Week 1)

- Freeze scope
- Finalise document

### Phase 1 — System Design (Week 2)

- Define Medical Event base schema
- Define dual-timestamp requirement
- Define medication lifecycle model
- Define anonymous access validation rules
- API surface outline

### Phase 2 — Backend Foundation (Weeks 3–4)

- FastAPI setup
- Database & migrations
- OpenAPI docs

### Phase 3 — Data Ingestion & Core Medical Records (Weeks 5–6)

- Manual patient entry APIs
- Document upload APIs

**Document Ingestion Pipeline (Mandatory Rules):**

- Original source file _(PDF/Image)_ always preserved

- Extracted data points link back to source

- Doctors can view source documents

- OCR used only for structuring, never diagnosis

- Human confirmation before timeline insertion

- CRUD APIs for Medical Events

- Timeline API

### Phase 4 — Doctor Access & Second Opinions (Weeks 7–8)

- Secure sharing links with validation challenge
- Doctor contributions
- Attribution & audits

### Phase 5 — Security & Privacy (Weeks 9–10)

- Authentication
- RBAC
- Encryption guidelines

### Phase 6 — Open Source Readiness (Week 11)

- License
- Contribution guides
- Public repository

---

## 16. Success Criteria

- A patient can recreate 2–3 years of history
- A doctor understands patient context in under 2 minutes
- Sharing works without hospital integration

---

## 17. Final Guiding Statement

**Unified Health Record (UHR)** is a patient-owned medical timeline ensuring no medical decision is made based on incomplete history.

The system prioritizes **continuity, consent, and clarity** over automation or prediction.

---

## 18. Interoperability & Standards Alignment

Interoperability supports **data portability and external system integration**, not internal domain control.

### Standards Position

- UHR is designed to allow alignment with modern healthcare interoperability standards.
- **HL7 FHIR (R4)** is the primary reference standard.
- Full FHIR conformance is **not required** in the MVP.
- The internal **Medical Event** model remains the canonical domain structure.

### Mapping Requirement

Every Medical Event type must be mappable to an equivalent FHIR resource, including:

- Patient → `Patient`
- Visit → `Encounter`
- Diagnosis → `Condition`
- Test Result → `Observation`
- Medication → `MedicationRequest`
- Procedure → `Procedure`
- Document → `DocumentReference`
- Second Opinion → `Communication` or `Observation`

Mapping must preserve:

- **Clinical timestamp**
- **Source attribution**
- **Audit traceability**
- **Visibility and consent rules**

### Integration Layer Responsibility

External integration layers may handle:

- FHIR import
- FHIR export
- National health gateway integration
- EHR interoperability
- Structured terminology support _(e.g., SNOMED, LOINC)_

The core UHR model must not introduce structural decisions that prevent future standards alignment.
