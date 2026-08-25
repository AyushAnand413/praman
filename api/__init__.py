"""HTTP surface — FastAPI app factory and routers.

Routes: GET /.well-known/agent-commerce.json, GET /audit/{id},
GET /audit/verify.

Every product payload leaving this package passes through the single
`to_public` serializer in `store`. There is no other DB → HTTP path.
"""
