"""Serverless entry for Vercel / AWS Lambda.

Vercel calls `handler`, local dev still uses `api.app:app`.
Keeps the same FastAPI app, no rewrite. Only used when DATABASE_URL points to Postgres.
"""

from api.app import app

try:
    from mangum import Mangum  # type: ignore

    handler = Mangum(app)
except ImportError:
    # Mangum not installed locally — still works for `uvicorn api.app:app`
    handler = app  # type: ignore
