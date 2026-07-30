"""The write-only-secret sentinel shared by the models and MCP routers.

Stored secrets (model API keys; MCP bearer/env/headers values) are never sent
to the frontend: GET replaces them with this mask, and a PUT that sends the
mask back means "keep the stored value". The constant used to exist as two
identical literals (API_KEY_MASK, SECRET_MASK); the masking/unmasking logic
itself stays per-router because the shapes genuinely differ (one flat field
vs. dict-valued env/headers). The frontend hardcodes the same literal in
ui/js/models.js and the connectors server keeps its own copy (separate
process) — change one, change all.
"""

SECRET_MASK = "********"
