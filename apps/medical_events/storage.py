"""
medical_events/storage.py
=========================
S3-compatible file storage utilities for document uploads.

Supports:
  - AWS S3
  - Cloudflare R2 (S3-compatible)
  - MinIO (S3-compatible, self-hosted)

Configuration (settings.py):
  DOCUMENT_STORAGE_BACKEND = "s3"  # s3 | r2 | minio | local
  AWS_ACCESS_KEY_ID        = "..."
  AWS_SECRET_ACCESS_KEY    = "..."
  AWS_STORAGE_BUCKET_NAME  = "uhr-documents"
  AWS_S3_ENDPOINT_URL      = None   # None for AWS S3
                                    # "https://<account>.r2.cloudflarestorage.com" for R2
                                    # "http://localhost:9000" for MinIO
  AWS_S3_REGION_NAME       = "ap-south-1"
  DOCUMENT_PRESIGNED_URL_EXPIRY = 3600  # seconds (1 hour default)

S3 key structure:
  documents/{patient_id}/{event_id}/{original_filename}

Security:
  - Presigned URLs are time-limited (default 1 hour)
  - Files are private by default (no public ACL)
  - Checksum verified on every retrieval (see DocumentEvent.verify_checksum)
"""

import hashlib
import logging
import uuid
from typing import Optional

from django.conf import settings

logger = logging.getLogger(__name__)

_PRESIGNED_URL_EXPIRY = getattr(settings, "DOCUMENT_PRESIGNED_URL_EXPIRY", 3600)
_BUCKET               = getattr(settings, "AWS_STORAGE_BUCKET_NAME", "uhr-documents")
_BACKEND              = getattr(settings, "DOCUMENT_STORAGE_BACKEND", "s3")


def _get_s3_client():
    """
    Return a boto3 S3 client configured for the active storage backend.
    Works for AWS S3, Cloudflare R2, and MinIO.
    """
    try:
        import boto3
        from botocore.config import Config

        kwargs = {
            "aws_access_key_id":     settings.AWS_ACCESS_KEY_ID,
            "aws_secret_access_key": settings.AWS_SECRET_ACCESS_KEY,
            "config":                Config(signature_version="s3v4"),
        }

        endpoint_url = getattr(settings, "AWS_S3_ENDPOINT_URL", None)
        region       = getattr(settings, "AWS_S3_REGION_NAME", "us-east-1")

        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        else:
            kwargs["region_name"] = region

        return boto3.client("s3", **kwargs)

    except ImportError:
        raise ImportError(
            "boto3 is required for S3 storage. "
            "Install it with: pip install boto3"
        )


def build_s3_key(patient_id: uuid.UUID, event_id: uuid.UUID, filename: str) -> str:
    """
    Build the S3 object key for a document.
    Structure: documents/{patient_id}/{event_id}/{filename}
    """
    safe_filename = filename.replace(" ", "_")
    return f"documents/{patient_id}/{event_id}/{safe_filename}"


def upload_document(
    file_bytes: bytes,
    patient_id: uuid.UUID,
    event_id: uuid.UUID,
    original_filename: str,
    content_type: str,
) -> dict:
    """
    Upload a document to S3-compatible storage.

    Returns a dict with:
      s3_key:   the S3 object key
      s3_bucket: the bucket name
      file_url: the permanent S3 URL (not presigned — for internal reference)
      checksum: SHA-256 hex digest computed from file_bytes

    Raises DocumentUploadFailed on any storage error.
    """
    from .exceptions import DocumentUploadFailed

    checksum = hashlib.sha256(file_bytes).hexdigest()
    s3_key   = build_s3_key(patient_id, event_id, original_filename)

    if _BACKEND == "local":
        # Local filesystem fallback for development
        return _upload_local(file_bytes, s3_key, checksum)

    try:
        client = _get_s3_client()
        client.put_object(
            Bucket      = _BUCKET,
            Key         = s3_key,
            Body        = file_bytes,
            ContentType = content_type,
            Metadata    = {
                "patient-id":  str(patient_id),
                "event-id":    str(event_id),
                "sha256":      checksum,
            },
        )

        endpoint_url = getattr(settings, "AWS_S3_ENDPOINT_URL", None)
        if endpoint_url:
            file_url = f"{endpoint_url}/{_BUCKET}/{s3_key}"
        else:
            region   = getattr(settings, "AWS_S3_REGION_NAME", "us-east-1")
            file_url = f"https://{_BUCKET}.s3.{region}.amazonaws.com/{s3_key}"

        logger.info(
            "Document uploaded. patient_id=%s event_id=%s s3_key=%s",
            patient_id, event_id, s3_key,
        )

        return {
            "s3_key":   s3_key,
            "s3_bucket": _BUCKET,
            "file_url": file_url,
            "checksum": checksum,
        }

    except Exception as e:
        logger.error(
            "Document upload failed. patient_id=%s event_id=%s error=%s",
            patient_id, event_id, str(e),
        )
        raise DocumentUploadFailed(f"Upload failed: {str(e)}")


def generate_presigned_url(s3_key: str, expiry_seconds: int = None) -> str:
    """
    Generate a presigned URL for downloading a document.
    URL is time-limited (default: DOCUMENT_PRESIGNED_URL_EXPIRY seconds).

    Called on every document retrieval — never store the presigned URL itself.
    """
    from .exceptions import DocumentUploadFailed

    if _BACKEND == "local":
        return f"/media/{s3_key}"

    expiry = expiry_seconds or _PRESIGNED_URL_EXPIRY

    try:
        client = _get_s3_client()
        url    = client.generate_presigned_url(
            "get_object",
            Params     = {"Bucket": _BUCKET, "Key": s3_key},
            ExpiresIn  = expiry,
        )
        return url
    except Exception as e:
        logger.error("Presigned URL generation failed. s3_key=%s error=%s", s3_key, e)
        raise DocumentUploadFailed(f"Could not generate download URL: {str(e)}")


def _upload_local(file_bytes: bytes, s3_key: str, checksum: str) -> dict:
    """
    Local filesystem fallback for development.
    Stores files in MEDIA_ROOT/documents/.
    Do NOT use in production.
    """
    import os

    media_root = getattr(settings, "MEDIA_ROOT", "/tmp/uhr_media")
    full_path  = os.path.join(media_root, s3_key)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)

    with open(full_path, "wb") as f:
        f.write(file_bytes)

    return {
        "s3_key":    s3_key,
        "s3_bucket": "local",
        "file_url":  f"/media/{s3_key}",
        "checksum":  checksum,
    }