from ninja import NinjaAPI
from apps.users.api import router as users_router

api = NinjaAPI(
    title="Unified Health Record API",
    version="1.0.0",
)

api.add_router("/users/", users_router)