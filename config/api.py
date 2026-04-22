from ninja import NinjaAPI
from apps.users.api import router as users_router
from apps.patients.api import router as patients_router
from apps.share.api import authenticated_router as share_router, public_router as share_public_router
from apps.visits.api import patient_router as visits_patient_router, org_router as visits_org_router
from apps.organisations.api import router as org_router
from apps.practitioners.api import router as prac_router
from apps.medical_events.api import router as medical_router


api = NinjaAPI(
    title="Unified Health Record API",
    version="1.0.0",
)

api.add_router("/users", users_router),
api.add_router("/patients", patients_router)


# Share links — authenticated (patient manages)
api.add_router("/patients/{patient_id}/share-links", share_router)

# Share links — public (anonymous doctor access)
api.add_router("/share", share_public_router)

# Visits — patient-facing
api.add_router("/patients{patient_id}/visits", visits_patient_router)

# Visits — org-facing (org QR generation)
api.add_router("/organisations", visits_org_router)

api.add_router("/organisations", org_router)
api.add_router("/practitioners", prac_router)



api.add_router("", medical_router)