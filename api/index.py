"""Serverless entry for Vercel / AWS Lambda.

Vercel calls `handler`, local dev still uses `api.app:app`.
Keeps the same FastAPI app, no rewrite. Only used when DATABASE_URL points to Postgres.
"""

from api.app import app
import os

# Vercel Python runtime speaks ASGI directly (no Mangum wrapping),
# Mangum is only for AWS Lambda. Using Mangum on Vercel breaks
# streamable HTTP content-length (UND_ERR_REQ_CONTENT_LENGTH_MISMATCH).
try:
    from mangum import Mangum  # type: ignore

    if os.getenv("VERCEL"):
        handler = app
    else:
        handler = Mangum(app)
except ImportError:
    handler = app  # type: ignore

# also expose app for Vercel's auto-detection
__all__ = ["app", "handler"]
