"""clinical/api.py — test order endpoints."""

import logging
from uuid import UUID

from django.core.exceptions import ValidationError
from ninja import Router

from core.auth import JWTBearer, get_current_user
from users.schemas import ErrorSchema

from . import services
from .exceptions import (
    InvalidOrderTransition,
    NoPractitionerProfile,
    OrderAccessDenied,
    OrderAlreadyCancelled,
    OrderAlreadyResulted,
    OrderNotFound,
)
from .schemas import (
    CreateTestOrderSchema,
    LinkResultSchema,
    TestOrderResponseSchema,
    TestOrderSummarySchema,
    UpdateOrderStatusSchema,
)

logger   = logging.getLogger(__name__)
jwt_auth = JWTBearer()
router   = Router(tags=["Clinical — Test Orders"])


def _build_order(order) -> dict:
    return {
        "id":                          order.id,
        "patient_id":                  order.patient_id,
        "ordering_practitioner_id":    order.ordering_practitioner_id,
        "ordering_practitioner_name":  order.ordering_practitioner.full_name,
        "ordering_organisation_id":    order.ordering_organisation_id,
        "test_name":                   order.test_name,
        "category":                    order.category,
        "coding_system":               order.coding_system,
        "coding_code":                 order.coding_code,
        "priority":                    order.priority,
        "clinical_reason":             order.clinical_reason,
        "special_instructions":        order.special_instructions,
        "specimen_type":               order.specimen_type,
        "status":                      order.status,
        "ordered_at":                  order.ordered_at,
        "due_date":                    order.due_date,
        "specimen_collected_at":       order.specimen_collected_at,
        "resulted_at":                 order.resulted_at,
        "cancelled_at":                order.cancelled_at,
        "cancellation_reason":         order.cancellation_reason,
        "resulting_event_id":          order.resulting_event_id,
        "cpoe_order_id":               order.cpoe_order_id,
        "order_set_id":                order.order_set_id,
        "billing_code":                order.billing_code,
        "requires_auth":               order.requires_auth,
        "created_at":                  order.created_at,
    }


@router.post(
    "/",
    auth=jwt_auth,
    response={201: dict, 400: ErrorSchema, 401: ErrorSchema, 403: ErrorSchema},
    summary="Place a test order",
    description=(
        "Place a test or investigation order for a patient. "
        "Requires a verified practitioner profile. "
        "Requires patient access (via visit session or direct access grant)."
    ),
)
def place_order(request, data: CreateTestOrderSchema):
    user = get_current_user(request)
    try:
        order = services.place_order(user, data)
        return 201, _build_order(order)
    except NoPractitionerProfile as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)
    except OrderAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)
    except ValidationError as e:
        return 400, ErrorSchema(detail=str(e), status_code=400)


@router.get(
    "/{order_id}/",
    auth=jwt_auth,
    response={200: dict, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="Get a test order",
)
def get_order(request, order_id: UUID):
    user = get_current_user(request)
    try:
        order = services.get_order(user, order_id)
        return 200, _build_order(order)
    except OrderNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except OrderAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


@router.get(
    "/patients/{patient_id}/",
    auth=jwt_auth,
    response={200: list, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema},
    summary="List orders for a patient",
)
def list_patient_orders(request, patient_id: UUID, status: str = None):
    user = get_current_user(request)
    try:
        orders = services.list_patient_orders(user, patient_id, status=status)
        return 200, [_build_order(o) for o in orders]
    except OrderAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


@router.get(
    "/my-orders/",
    auth=jwt_auth,
    response={200: list, 401: ErrorSchema, 403: ErrorSchema},
    summary="List orders placed by me (practitioner)",
)
def list_my_orders(request, status: str = None):
    user = get_current_user(request)
    try:
        orders = services.list_my_orders(user, status=status)
        return 200, [_build_order(o) for o in orders]
    except NoPractitionerProfile as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


@router.patch(
    "/{order_id}/status/",
    auth=jwt_auth,
    response={200: dict, 400: ErrorSchema, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema, 409: ErrorSchema},
    summary="Update order status",
    description=(
        "Transition an order through its lifecycle. "
        "Valid transitions: draft→active, active→specimen_collected, "
        "specimen_collected→in_lab, in_lab→resulted, any→cancelled."
    ),
)
def update_order_status(request, order_id: UUID, data: UpdateOrderStatusSchema):
    user = get_current_user(request)
    try:
        order = services.update_order_status(user, order_id, data.status, data.notes)
        return 200, _build_order(order)
    except OrderNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except InvalidOrderTransition as e:
        return 409, ErrorSchema(detail=e.message, status_code=409)
    except OrderAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)


@router.post(
    "/{order_id}/link-result/",
    auth=jwt_auth,
    response={200: dict, 400: ErrorSchema, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema, 409: ErrorSchema},
    summary="Link a result to an order",
    description=(
        "Connect an ObservationEvent (the permanent result record) to this order. "
        "The ObservationEvent must already exist in the patient's medical timeline. "
        "Transitions order status to 'resulted'."
    ),
)
def link_result(request, order_id: UUID, data: LinkResultSchema):
    user = get_current_user(request)
    try:
        order = services.link_result(user, order_id, data.resulting_event_id)
        return 200, _build_order(order)
    except OrderNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except OrderAlreadyResulted as e:
        return 409, ErrorSchema(detail=e.message, status_code=409)
    except OrderAlreadyCancelled as e:
        return 409, ErrorSchema(detail=e.message, status_code=409)
    except OrderAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)
    except ValidationError as e:
        return 400, ErrorSchema(detail=str(e), status_code=400)


@router.delete(
    "/{order_id}/",
    auth=jwt_auth,
    response={200: dict, 400: ErrorSchema, 401: ErrorSchema, 403: ErrorSchema, 404: ErrorSchema, 409: ErrorSchema},
    summary="Cancel a test order",
)
def cancel_order(request, order_id: UUID, reason: str = "No reason provided"):
    user = get_current_user(request)
    try:
        order = services.cancel_order(user, order_id, reason)
        return 200, _build_order(order)
    except OrderNotFound as e:
        return 404, ErrorSchema(detail=e.message, status_code=404)
    except (OrderAlreadyResulted, OrderAlreadyCancelled) as e:
        return 409, ErrorSchema(detail=e.message, status_code=409)
    except OrderAccessDenied as e:
        return 403, ErrorSchema(detail=e.message, status_code=403)