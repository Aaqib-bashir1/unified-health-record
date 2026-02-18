"""
audit/models.py
===============
System-wide, append-only audit layer.

Models:
  - AuditLog — immutable record of every read and write action across UHR

Dependency rule:
  audit/ depends on → patients/ (for patient_id reference)
  audit/ depends on → users/   (for user_id reference)
  audit/ must never be imported by → patients/, integrations/, claims/
  audit/ is written TO by all other apps via the audit service,
  never via direct model imports in business logic.

Design rules:
  - Every row is permanent. No updates. No deletes.
  - Written atomically with the action that triggered it.
    If the audit write fails, the triggering action must roll back.
  - Covers all actions defined in schema section 7:
    read | create | share | revoke | update | claim | transfer | delete (soft)
  - resource_type + resource_id form a generic reference to any model
    in any app without requiring FK constraints to every table.
    This keeps audit/ decoupled from domain models while retaining traceability.
"""

import uuid
from django.db import models
from django.conf import settings


# ===========================================================================
# CHOICES
# ===========================================================================

class AuditAction(models.TextChoices):
    """
    Every auditable action in UHR.

    Aligned with schema section 7 and extended to cover
    the claims and access management flows designed in this session.
    """
    # Core data actions
    READ   = "read",   "Read"
    CREATE = "create", "Create"
    UPDATE = "update", "Update"
    SHARE  = "share",  "Share (Consent Granted)"
    REVOKE = "revoke", "Revoke (Consent or Access Revoked)"

    # Soft delete / retraction
    RETRACT = "retract", "Retract (Soft Delete)"

    # Profile claim flow
    CLAIM_INITIATED  = "claim_initiated",  "Claim Initiated"
    CLAIM_SUCCESS    = "claim_success",    "Claim Succeeded"
    CLAIM_FAILED     = "claim_failed",     "Claim Failed"

    # Ownership lifecycle
    OWNERSHIP_TRANSFER = "ownership_transfer", "Ownership Transferred"
    ACCESS_GRANTED     = "access_granted",     "Access Granted"
    ACCESS_REVOKED     = "access_revoked",     "Access Revoked"

    # Support operations
    SUPPORT_CLAIM_APPROVED = "support_claim_approved", "Support Claim Approved"
    SUPPORT_CLAIM_REJECTED = "support_claim_rejected", "Support Claim Rejected"

    # External identity
    IDENTITY_LINKED   = "identity_linked",   "External Identity Linked"
    IDENTITY_UNLINKED = "identity_unlinked", "External Identity Unlinked"


# ===========================================================================
# MODEL: AUDIT LOG
# ===========================================================================

class AuditLog(models.Model):
    """
    Immutable, append-only audit record of every significant action in UHR.

    Schema alignment:
      Defined in UHR schema section 7. Fields extended to cover the full
      access management and claims lifecycle.

    resource_type + resource_id:
      A generic reference pattern. resource_type is the model name as a string
      (e.g. "Patient", "PatientUserAccess", "MedicalEvent").
      resource_id is the UUID of the specific record affected.
      This avoids FK constraints to every table while remaining queryable.

    patient_id:
      Always populated when the action relates to a patient's data.
      Allows the patient to see a complete log of all activity on their record
      without joining across resource types.

    Immutability:
      No updated_at field. No soft delete fields.
      Records in this table are permanent and unalterable.
      Any attempt to update or delete a row should be treated as a system error.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # The user who performed the action
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="audit_logs",
        null=True,
        blank=True,
        help_text="The user who performed the action. Null for system-generated actions.",
    )

    # The patient this action relates to (always populated for clinical actions)
    patient = models.ForeignKey(
        "patients.Patient",
        on_delete=models.PROTECT,
        related_name="audit_logs",
        null=True,
        blank=True,
        help_text="The patient whose data was accessed or affected.",
    )

    # ── Action ───────────────────────────────────────────────────────────────
    action = models.CharField(max_length=32, choices=AuditAction.choices)

    # ── Resource Reference (generic) ──────────────────────────────────────────
    # resource_type: model name as a string (e.g. "MedicalEvent", "PatientUserAccess")
    # resource_id:   UUID of the specific record affected
    resource_type = models.CharField(
        max_length=64,
        help_text="The model/resource type affected (e.g. 'MedicalEvent', 'PatientUserAccess').",
    )
    resource_id = models.UUIDField(
        null=True,
        blank=True,
        help_text="UUID of the specific record affected.",
    )

    # ── Network Metadata ──────────────────────────────────────────────────────
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(null=True, blank=True)

    # ── Additional Context ────────────────────────────────────────────────────
    # Machine-readable structured metadata about the action.
    # Examples:
    #   claim_method: "email_otp"
    #   revocation_reason: "patient_initiated"
    #   trust_level: "email_verified"
    #   previous_role: "full_delegate"
    #   new_role: "primary"
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Structured context about the action. Schema varies by action type.",
    )

    # Human-readable summary for display in patient-facing access logs
    description = models.TextField(
        null=True,
        blank=True,
        help_text="Human-readable description of the action. Shown in patient access history.",
    )

    # Immutable timestamp. This is the canonical record of when the action occurred.
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "audit"
        db_table  = "audit_logs"
        indexes   = [
            # Patient's own access history view (schema section 7)
            models.Index(
                fields=["patient", "timestamp"],
                name="idx_audit_patient_timestamp",
            ),
            # Per-user activity (admin review)
            models.Index(
                fields=["user", "timestamp"],
                name="idx_audit_user_timestamp",
            ),
            # Resource-specific audit trail (e.g. all actions on a MedicalEvent)
            models.Index(
                fields=["resource_type", "resource_id"],
                name="idx_audit_resource",
            ),
            # Action-type queries (e.g. all CLAIM_SUCCESS events)
            models.Index(
                fields=["action", "timestamp"],
                name="idx_audit_action_timestamp",
            ),
        ]
        ordering = ["-timestamp"]

    def __str__(self):
        return (
            f"[{self.action}] {self.resource_type} {self.resource_id} "
            f"by User {self.user_id} @ {self.timestamp}"
        )

    def save(self, *args, **kwargs):
        # Guard: audit records must never be updated after creation.
        if self.pk:
            raise RuntimeError(
                "AuditLog records are immutable. "
                "Attempted to update an existing audit record."
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        # Guard: audit records must never be deleted.
        raise RuntimeError(
            "AuditLog records are permanent. "
            "Attempted to delete an audit record."
        )