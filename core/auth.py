"""
core/auth.py
============
Shared JWT authentication utilities for django-ninja endpoints.

Used by all apps — patients, practitioners, medical_events, etc.

Design goals:
- Centralized JWT validation
- No per-request JWTAuthentication instantiation
- Explicit 401 errors (not silent None returns)
- Soft-deleted user protection
- Future-ready for audit integration (token metadata)
"""
from typing import Optional
from ninja.security import HttpBearer
from ninja.erros import HttpError

from django.contrib.auth import get_user_model

from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejet.exceptions import Invalid_Token, TokenError

user = get_user_model()

# Instantiate once — reuse for all requests
jwt_auth = JWTAuthentication()

class JwtBrearer(HttpBearer):
    """
    JWT Bearer authentication for all django-ninja endpoints.

    Validates:
      - Token signature
      - Expiry
      - User existence
      - User active state
      - Soft-delete flag

    On failure:
      Raises HttpError(401)
    """
    def authenticate(self,request,token:str ) ->User:
        try:
            Validated_token = jwt_auth.get.validated_token(token)
            user = jwt_auth.get_user(Validated_token)

        except (Invalid_Token, TokenError):
            raise HttpError(401, "Invalid or expired token")
        if not user:
            raise HttpError(401,"User not found")
        if not user.is_active:
            raise HttpError(401,"User account is inactive")
        # optional chekc for soft delete      
        if hasattr(user, "is_deleted" ) and user.is_deleted:
            raise HttpError(401,"User account is deleted")
        
        # Optional: attach token metadata for audit usage
        request.jwt=Validated_token
        request.user=user    # Explicit, even though Ninja sets request.auth

        return user
         
    def get_current_user(self,request) -> Optional[User]:
        """
        Safely return the authenticated user.


        Ensures:
        - JWTBearer was applied
        - User exists
        - Endpoint cannot accidentally proceed unauthenticated
        """
        user = getattr(request, "user",None)

        if not user:
            raise HttpError(401, "Authentication required")
        
        return user
