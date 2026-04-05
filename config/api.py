from ninja import NinjaAPI
from apps.users.api import router as users_router
from apps.patients.api import router as patients_router

api = NinjaAPI(
    title="Unified Health Record API",
    version="1.0.0",
)

api.add_router("/users/", users_router),
api.add_router("/patients/", patients_router)