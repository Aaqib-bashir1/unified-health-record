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
- **Open-source governance**

---

## What UHR Is

- A **patient-controlled medical record system**
- A **chronological medical timeline**, not a static file store
- A system that preserves **historical context and trends**
- Designed for real-world healthcare messiness
- Built with auditability and medico-legal traceability in mind

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

- Patients control who can access their records
- Access is:
  - Explicit
  - Time-bound
  - Scope-bound
- Patients can revoke access at any time
- Anonymous / remote doctors require:
  - Secure, time-limited links
  - Secondary validation (e.g., DOB or PIN)

If any history is hidden, doctors are clearly informed that the timeline is filtered.

---

## Data Philosophy

- Medical data is **append-only**
- Nothing is overwritten or silently deleted
- Corrections are handled via amendments
- Conflicting data is preserved with attribution
- Source documents are always retained for verification

This preserves trust, longitudinal context, and medico-legal traceability.

---

## Project Status

UHR is currently in the **design and foundation phase**.

- Scope is frozen
- Core models are being defined
- No production deployment exists yet

See the full system specification for details.

---

## Documentation

- 📄 **Full System Specification**: [`docs/uhr-spec.md`](docs/uhr-spec.md)
- 📘 **Security Policy**: [`SECURITY.md`](SECURITY.md)

---

## License

This project is licensed under the **GNU Affero General Public License v3.0 (AGPL-3.0)**.

- Any deployed network service using UHR must provide access to the corresponding source code.
- Patient data is **not** affected by the license — only source code.

See the `LICENSE` file for details.

---

## Contributor License Agreement (CLA)

To ensure long-term sustainability and allow future commercial support or alternative licensing models, all contributors are required to sign a **Contributor License Agreement (CLA)** before their pull requests can be merged.

The CLA:

- Allows the project to remain open-source under AGPL-3.0
- Grants the Project Lead the right to sublicense or relicense contributions if needed in the future
- Guarantees permanent authorship credit for all contributors

You can review the full CLA here:  
👉 https://gist.github.com/Aaqib-bashir1/60afa4d39a14a394e87e1a701d9e1942

---

## Contributing

UHR is healthcare infrastructure. Contributions are welcome, but must respect project scope and principles.

Please read `CONTRIBUTING.md` before opening issues or pull requests.

---

## 👥 Contributors

This project follows the [all-contributors](https://github.com/all-contributors/all-contributors) specification.

---

## Final Note

Unified Health Record (UHR) exists to ensure that **no medical decision is made based on incomplete history**.

Clarity over automation.  
Consent over convenience.  
Continuity over speed.
