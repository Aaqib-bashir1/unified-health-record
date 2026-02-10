# Contributing to Unified Health Record (UHR)

Thank you for your interest in contributing to **Unified Health Record (UHR)**.

UHR is **healthcare infrastructure**, not a general-purpose app.  
Contributions are welcome, but they must respect the project’s **ethical, clinical, and architectural boundaries**.

---

## Project Philosophy

Before contributing, please understand the core principles of UHR:

- **Patients own their data**
- **Medical history is longitudinal and append-only**
- **Consent is explicit, revocable, and scoped**
- **Transparency over automation**
- **Clarity over prediction**

If a contribution conflicts with these principles, it will not be accepted.

---

## Scope Boundaries (Very Important)

### UHR Accepts Contributions That:

- Improve data modeling or correctness
- Strengthen consent, privacy, or auditability
- Improve ingestion of real-world medical documents
- Improve developer experience or documentation
- Improve performance or reliability **without altering semantics**

### UHR Does NOT Accept Contributions That:

- Add AI diagnosis or treatment recommendations
- Automate clinical decision-making
- Introduce surveillance, tracking, or behavioral analytics
- Add billing, insurance, or hospital inventory features
- Override or mutate historical medical data
- Remove or weaken consent or audit mechanisms

These boundaries are **non-negotiable**.

---

## Contribution Process

### 1. Issues First

- All non-trivial changes **must be discussed in an issue first**
- Feature PRs without an associated issue may be closed
- This prevents scope creep and misalignment

### 2. Pull Requests

Pull requests must:

- Be focused and minimal
- Reference a related issue
- Preserve backward compatibility unless explicitly discussed
- Include rationale for any data model changes
- Avoid mixing unrelated changes

Maintainers may request changes or close PRs that conflict with project goals.

---

## Data Model Changes (Special Rules)

Because UHR handles medical data:

- Data must be **append-only**
- Existing medical events must never be overwritten or deleted
- Corrections must be modeled as **amendments**
- Timeline semantics must be preserved
- Dual timestamps (clinical vs system) must remain intact

Any PR violating these rules will be rejected.

---

## Security & Privacy

- Do **not** report security vulnerabilities via public issues
- Follow the instructions in `SECURITY.md`
- Never include real patient data in:
  - Issues
  - Pull requests
  - Test fixtures
  - Logs or screenshots

Synthetic or anonymized data only.

---

## Licensing

UHR is licensed under **GNU Affero General Public License v3.0 (AGPL-3.0)**.

By contributing:

- You agree that your contributions will be licensed under AGPL-3.0
- You confirm you have the right to submit the code
- You understand that deployed network use requires source availability

No Contributor License Agreement (CLA) is required at this time.

---

## Code Style & Quality

- Follow existing code patterns and conventions
- Prefer clarity over cleverness
- Avoid premature optimization
- Write code that can be reasoned about years later

Healthcare systems must be **boring, predictable, and correct**.

---

## Maintainer Discretion

Maintainers reserve the right to:

- Reject or close issues or PRs
- Request design changes
- Enforce project scope and principles

This is not personal — it is necessary for healthcare infrastructure.

---

## Final Note

If you are unsure whether a contribution fits UHR, **ask first**.

Good contributions improve trust, safety, and continuity of care.

Thank you for helping build responsible healthcare infrastructure.
