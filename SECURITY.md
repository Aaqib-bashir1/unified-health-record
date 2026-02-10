# Security Policy

Unified Health Record (UHR) handles **sensitive healthcare data**.
Security issues must be handled responsibly to protect patients, doctors, and contributors.

---

## Reporting a Security Vulnerability

⚠️ **Do NOT open public GitHub issues for security vulnerabilities.**

If you discover a security issue, vulnerability, or potential data exposure, please report it **privately**.

### How to Report

Send an email to:

**aaqib.untoo15@gmail.com**

Include:

- A clear description of the issue
- Steps to reproduce (if applicable)
- Potential impact
- Any proof-of-concept code (if safe to share)

---

## What Counts as a Security Issue

Please report issues related to:

- Unauthorized access to medical records
- Consent or access control bypass
- Authentication or authorization flaws
- Audit log tampering or evasion
- Data leakage (structured data or documents)
- Injection vulnerabilities
- Insecure defaults or misconfigurations

---

## What Is NOT a Security Issue

The following should be discussed via normal GitHub issues:

- Feature requests
- Performance concerns
- Code style or refactoring
- Documentation improvements
- Design disagreements

---

## Disclosure Process

Upon receiving a security report:

1. Maintainers will acknowledge receipt
2. The issue will be investigated privately
3. A fix will be developed and reviewed
4. A disclosure timeline will be coordinated if appropriate

We aim to respond responsibly, but timelines may vary due to the sensitive nature of healthcare systems.

---

## Data Safety Rules for Contributors

To protect patient privacy:

- ❌ Never include real patient data in:
  - Issues
  - Pull requests
  - Test fixtures
  - Logs
  - Screenshots
- ✅ Use only:
  - Synthetic data
  - Fully anonymized examples

Violations may result in removal of content or contributor access.

---

## Deployment Responsibility

UHR is open-source software.
Operators who deploy UHR are responsible for:

- Secure configuration
- Proper access controls
- Compliance with applicable laws and regulations
- Protecting patient data in their environment

The UHR project provides **software**, not hosted services.

---

## License & Security

UHR is licensed under **GNU Affero General Public License v3.0 (AGPL-3.0)**.

Security disclosure obligations apply **only to code**, not to patient data.
Patient data must never be shared as part of security reports.

---

## Final Note

Security in healthcare systems is not optional.

If you are unsure whether something is a security issue, **err on the side of caution and report it privately**.

Thank you for helping keep UHR safe and trustworthy.
