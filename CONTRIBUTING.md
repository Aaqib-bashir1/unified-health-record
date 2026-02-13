# Contributing to Unified Health Record (UHR)

Thank you for your interest in contributing to **Unified Health Record (UHR)**.

UHR is healthcare infrastructure, not a general-purpose application.  
Contributions are welcome, but they must respect the project’s ethical, clinical, and architectural boundaries.

---

## Project Philosophy

Before contributing, please understand the core principles of UHR:

- **Patients own their data**
- **Medical history is longitudinal and append-only**
- **Consent is explicit, revocable, and scoped**
- **Transparency over automation**
- **Clarity over prediction**
- **Correctness over speed**

If a contribution conflicts with these principles, it will not be accepted.

---

## Scope Boundaries (Non-Negotiable)

### UHR Accepts Contributions That:

- Improve data modeling or correctness
- Strengthen consent, privacy, or auditability
- Improve ingestion of real-world medical documents
- Improve developer experience or documentation
- Improve performance or reliability **without altering medical semantics**

### UHR Does NOT Accept Contributions That:

- Add AI diagnosis or treatment recommendations
- Automate clinical decision-making
- Introduce surveillance, tracking, or behavioral analytics
- Add billing, insurance, or hospital ERP features
- Override or mutate historical medical data
- Remove or weaken consent or audit mechanisms

These boundaries exist to preserve clinical safety and trust.

---

## Contribution Process

### 1. Issues First

All non-trivial changes **must be discussed in a GitHub issue first**.

- Feature pull requests without prior discussion may be closed.
- This prevents scope creep and architectural drift.

---

### 2. Pull Requests

Pull requests must:

- Be focused and minimal
- Reference a related issue
- Preserve backward compatibility unless explicitly discussed
- Include clear rationale for any data model changes
- Avoid mixing unrelated changes

Maintainers may request changes or close PRs that conflict with project goals.

---

## Data Model Changes (Strict Rules)

Because UHR handles medical data:

- Data must be **append-only**
- Existing medical events must never be overwritten or deleted
- Corrections must be modeled as amendments
- Timeline semantics must be preserved
- Dual timestamps (clinical vs system) must remain intact
- Audit trails must never be bypassed

Any pull request violating these rules will be rejected.

---

## Security & Privacy

- Do **not** report security vulnerabilities via public issues.
- Follow the instructions in `SECURITY.md`.
- Never include real patient data in:
  - Issues
  - Pull requests
  - Test fixtures
  - Logs
  - Screenshots

Synthetic or anonymized data only.

---

## Licensing & Contributor License Agreement (CLA)

UHR is licensed under the **GNU Affero General Public License v3.0 (AGPL-3.0)**.

Before a pull request can be merged, contributors must sign the project's **Contributor License Agreement (CLA)**.

The CLA:

- Allows the project to remain open-source under AGPL-3.0
- Grants the Project Lead the right to sublicense or relicense contributions if necessary in the future
- Guarantees permanent authorship credit

You can review the full CLA here:
👉 _[Link to your CLA Gist]_

By submitting a contribution, you agree to both:

- The AGPL-3.0 license
- The terms of the Contributor License Agreement

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

- Reject or close issues or pull requests
- Request architectural changes
- Enforce project scope and principles

This is not personal — it is necessary for healthcare infrastructure integrity.

---

## Final Note

If you are unsure whether a contribution fits UHR, open an issue first.

Good contributions improve trust, safety, and continuity of care.

Thank you for helping build responsible healthcare infrastructure.
