# Unified Health Record (UHR)

Unified Health Record (UHR) is an **open-source, patient-owned medical timeline** designed to prevent clinical decisions from being made on incomplete or fragmented health history.

UHR focuses on **continuity, consent, and clarity**, not automation or prediction.

---

## Why UHR Exists

Patients often receive care from multiple doctors across hospitals, clinics, and labs. Their medical history becomes fragmented across:

- Paper files
- PDFs and scanned reports
- Messaging apps
- Personal memory

Doctors rarely see the full picture — leading to repeated tests, unsafe prescriptions, and poor continuity of care.

**The core problem is information loss, not lack of medical expertise.**

UHR addresses this by preserving a complete, longitudinal health timeline that patients own and control.

---

## Core Principles

- **Patient owns the data**
- **Timeline over tables** (longitudinal history matters)
- **Consent-first access** (explicit, revocable, scoped)
- **Low friction for doctors**
- **Manual digitisation supported**
- **Privacy by default**
- **Open source, community-driven**

---

## What UHR Is

- A **patient-controlled medical record system**
- A **chronological medical timeline**, not a static file store
- A system that preserves **historical context and trends**
- Designed for **real-world healthcare messiness**
- Built with **auditability and medico-legal traceability** in mind

---

## What UHR Is NOT

UHR explicitly does **not** include:

- ❌ AI diagnosis or treatment recommendations
- ❌ Emergency monitoring or real-time alerts
- ❌ Billing, insurance, or hospital inventory
- ❌ Predictive analytics or surveillance
- ❌ Automated clinical decision-making

UHR provides **context and clarity**, not medical judgment.

---

## Consent & Access Model (High Level)

- Patients control **who can access** their records
- Access is:
  - Explicit
  - Time-bound
  - Scope-bound
- Patients can revoke access at any time
- Anonymous / remote doctors may access records only via:
  - Secure, time-limited links
  - A secondary validation challenge (e.g., DOB or PIN)

If any history is hidden, doctors are clearly informed that the timeline is filtered.

---

## Data Philosophy

- Medical data is **append-only**
- Nothing is overwritten or silently deleted
- Corrections are handled via **amendments**
- Conflicting data is preserved with attribution
- Source documents are always retained for verification

This preserves trust, trends, and legal traceability.

---

## Project Status

UHR is currently in the **design and foundation phase**.

- Scope is frozen
- Core models are being defined
- No production deployment exists yet

See the full specification for details.

---

## Documentation

- 📄 **Full System Specification**: [`docs/uhr-spec.md`](docs/uhr-spec.md)
- 📘 Swagger / OpenAPI: _(added during backend implementation)_

---

## License

This project is licensed under the **GNU Affero General Public License v3.0 (AGPL-3.0)**.

- Any deployed network service using UHR must provide access to the corresponding source code
- Patient data is **not** affected by the license — only source code

See the `LICENSE` file for details.

---

## Contributing

UHR is healthcare infrastructure. Contributions are welcome, but with strict boundaries.

Please read `CONTRIBUTING.md` before opening issues or pull requests.

---

## Final Note

Unified Health Record (UHR) exists to ensure that **no medical decision is made based on incomplete history**.

Clarity over automation.  
Consent over convenience.  
Continuity over speed.
