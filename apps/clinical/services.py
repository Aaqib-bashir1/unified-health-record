"""
clinical/services.py
====================
Service layer for clinical workflow — test orders.

Function index:
  place_order(user, data)                        → TestOrder
  get_order(user, order_id)                      → TestOrder
  list_patient_orders(user, patient_id, status)  → QuerySet[TestOrder]
  list_my_orders(user, status)                   → QuerySet[TestOrder]
  update_order_status(user, order_id, data)      → TestOrder
  link_result(user, order_id, resulting_event_id) → TestOrder
  cancel_order(user, order_id, reason)           → TestOrder
"""

import logging
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from patients.services import _get_active_access

from .exceptions import (
    InvalidOrderTransition,
    NoPractitionerProfile,
    OrderAccessDenied,
    OrderAlreadyCancelled,
    OrderAlreadyResulted,
    OrderNotFound,
)
from .models import OrderStatus, TestOrder

logger = logging.getLogger(__name__)

# Valid status transitions
_VALID_TRANSITIONS = {
    OrderStatus.DRAFT:              {OrderStatus.ACTIVE, OrderStatus.CANCELLED},
    OrderStatus.ACTIVE:             {OrderStatus.SPECIMEN_COLLECTED, OrderStatus.CANCELLED, OrderStatus.ON_HOLD},
    OrderStatus.SPECIMEN_COLLECTED: {OrderStatus.IN_LAB, OrderStatus.CANCELLED},
    OrderStatus.IN_LAB:             {OrderStatus.RESULTED, OrderStatus.CANCELLED},
    OrderStatus.ON_HOLD:            {OrderStatus.ACTIVE, OrderStatus.CANCELLED},
    OrderStatus.RESULTED:           set(),   # terminal
    OrderStatus.CANCELLED:          set(),   # terminal
}


def _get_practitioner(user):
    """Get verified practitioner for user or raise NoPractitionerProfile."""
    try:
        from practitioners.models import Practitioner
        return Practitioner.objects.get(user=user, is_verified=True, is_active=True)
    except Exception:
        raise NoPractitionerProfile()


def _assert_order_access(user, order: TestOrder) -> None:
    """
    Verify the user can access this order.
    Access is granted if:
      - User is the ordering practitioner
      - User has patient access (can_write)
      - User is UHR staff
    """
    if user.is_staff:
        return

    # Check if user is the ordering practitioner
    try:
        from practitioners.models import Practitioner
        prac = Practitioner.objects.get(user=user)
        if prac == order.ordering_practitioner:
            return
    except Exception:
        pass

    # Check patient access
    try:
        access = _get_active_access(user, order.patient_id)
        if access.can_read:
            return
    except Exception:
        pass

    raise OrderAccessDenied()


# ===========================================================================
# PLACE ORDER
# ===========================================================================

@transaction.atomic
def place_order(user, data) -> TestOrder:
    """
    Place a test order for a patient.
    Requires a verified practitioner profile.
    Requires the practitioner has access to the patient
    (via visit session, direct access grant, or patient consent).
    """
    practitioner = _get_practitioner(user)

    # Verify the practitioner has access to this patient
    try:
        access = _get_active_access(user, data.patient_id)
        if not access.can_write:
            raise OrderAccessDenied(
                "You need write access to this patient to place orders."
            )
    except OrderAccessDenied:
        raise
    except Exception:
        # Check visit-based access
        try:
            from visits.services import check_practitioner_visit_access
            check_practitioner_visit_access(practitioner, data.patient_id)
        except Exception:
            raise OrderAccessDenied(
                "You do not have access to this patient. "
                "Ensure the patient has an active visit at your organisation."
            )

    org = practitioner.primary_organisation

    order = TestOrder.objects.create(
        patient               = _get_active_access(user, data.patient_id).patient,
        ordering_practitioner = practitioner,
        ordering_organisation = org,
        created_by            = user,
        test_name             = data.test_name,
        category              = data.category,
        coding_system         = data.coding_system,
        coding_code           = data.coding_code,
        coding_display        = data.coding_display,
        priority              = data.priority,
        clinical_reason       = data.clinical_reason,
        special_instructions  = data.special_instructions,
        specimen_type         = data.specimen_type,
        due_date              = data.due_date,
        status                = OrderStatus.ACTIVE,
    )

    logger.info(
        "Test order placed. order_id=%s test=%s patient=%s practitioner=%s",
        order.id, order.test_name, data.patient_id, practitioner.id,
    )
    return order


# ===========================================================================
# GET / LIST
# ===========================================================================

def get_order(user, order_id: UUID) -> TestOrder:
    try:
        order = TestOrder.objects.select_related(
            "ordering_practitioner",
            "ordering_organisation",
            "patient",
        ).get(pk=order_id)
    except TestOrder.DoesNotExist:
        raise OrderNotFound()

    _assert_order_access(user, order)
    return order


def list_patient_orders(user, patient_id: UUID, status: str = None):
    """List all orders for a patient. Requires patient read access."""
    access = _get_active_access(user, patient_id)
    if not access.can_read:
        raise OrderAccessDenied()

    qs = TestOrder.objects.filter(
        patient_id=patient_id,
    ).select_related(
        "ordering_practitioner",
        "ordering_organisation",
    ).order_by("-ordered_at")

    if status:
        qs = qs.filter(status=status)

    return qs


def list_my_orders(user, status: str = None):
    """List all orders placed by this practitioner."""
    practitioner = _get_practitioner(user)

    qs = TestOrder.objects.filter(
        ordering_practitioner=practitioner,
    ).select_related("patient", "ordering_organisation").order_by("-ordered_at")

    if status:
        qs = qs.filter(status=status)

    return qs


# ===========================================================================
# UPDATE STATUS
# ===========================================================================

@transaction.atomic
def update_order_status(user, order_id: UUID, new_status: str, notes: str = None) -> TestOrder:
    """
    Transition a test order to a new status.
    Enforces valid transition rules.
    """
    try:
        order = TestOrder.objects.select_for_update().get(pk=order_id)
    except TestOrder.DoesNotExist:
        raise OrderNotFound()

    _assert_order_access(user, order)

    valid_next = _VALID_TRANSITIONS.get(order.status, set())
    if new_status not in valid_next:
        raise InvalidOrderTransition(
            f"Cannot transition from '{order.status}' to '{new_status}'. "
            f"Valid transitions: {', '.join(valid_next) or 'none (terminal state)'}."
        )

    now            = timezone.now()
    order.status   = new_status
    update_fields  = ["status", "updated_at"]

    if new_status == OrderStatus.SPECIMEN_COLLECTED:
        order.specimen_collected_at = now
        update_fields.append("specimen_collected_at")
    elif new_status == OrderStatus.CANCELLED:
        order.cancelled_at        = now
        order.cancellation_reason = notes
        update_fields += ["cancelled_at", "cancellation_reason"]

    order.save(update_fields=update_fields)

    logger.info(
        "Order status updated. order_id=%s %s→%s by user=%s",
        order_id, order.status, new_status, user.id,
    )
    return order


# ===========================================================================
# LINK RESULT
# ===========================================================================

@transaction.atomic
def link_result(user, order_id: UUID, resulting_event_id: UUID) -> TestOrder:
    """
    Link a resulted ObservationEvent to a TestOrder.
    Transitions the order to status=resulted.

    The ObservationEvent must already exist in medical_events/.
    It is created separately (by the patient uploading, or lab integration).
    This call simply connects the two.
    """
    try:
        order = TestOrder.objects.select_for_update().get(pk=order_id)
    except TestOrder.DoesNotExist:
        raise OrderNotFound()

    _assert_order_access(user, order)

    if order.is_resulted:
        raise OrderAlreadyResulted()
    if order.is_cancelled:
        raise OrderAlreadyCancelled()

    # Verify the event exists and belongs to the same patient
    try:
        from medical_events.models import MedicalEvent
        event = MedicalEvent.objects.get(
            pk=resulting_event_id,
            patient=order.patient,
            is_active=True,
        )
    except MedicalEvent.DoesNotExist:
        from django.core.exceptions import ValidationError
        raise ValidationError({
            "resulting_event_id": "ObservationEvent not found for this patient."
        })

    now                    = timezone.now()
    order.resulting_event  = event
    order.status           = OrderStatus.RESULTED
    order.resulted_at      = now
    order.save(update_fields=["resulting_event", "status", "resulted_at", "updated_at"])

    logger.info(
        "Order resulted. order_id=%s event_id=%s patient=%s",
        order_id, resulting_event_id, order.patient_id,
    )
    return order


# ===========================================================================
# CANCEL ORDER
# ===========================================================================

@transaction.atomic
def cancel_order(user, order_id: UUID, reason: str) -> TestOrder:
    """Cancel a test order. Cannot cancel an already-resulted order."""
    try:
        order = TestOrder.objects.select_for_update().get(pk=order_id)
    except TestOrder.DoesNotExist:
        raise OrderNotFound()

    _assert_order_access(user, order)

    if order.is_resulted:
        raise OrderAlreadyResulted("A resulted order cannot be cancelled.")
    if order.is_cancelled:
        raise OrderAlreadyCancelled()

    now                     = timezone.now()
    order.status            = OrderStatus.CANCELLED
    order.cancelled_at      = now
    order.cancellation_reason = reason
    order.save(update_fields=["status", "cancelled_at", "cancellation_reason", "updated_at"])

    return order