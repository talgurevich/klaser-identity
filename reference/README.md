# Reference: product-side identity client

`identity_client.py` is the SDK a product backend uses to authenticate
against this service. It's kept here as a **reference copy** — the
authoritative version lives in each product's own repo (currently
`elrom-platform/backend/app/services/identity.py`).

## Copy-paste to a new product

Until we publish this as a real Python package, the workflow is:

1. Copy `reference/identity_client.py` into the product backend as
   `app/services/identity.py`.
2. In the product's settings, add two env vars:
   - `IDENTITY_URL` — e.g. `https://auth.klaser.co.il`
   - `IDENTITY_SERVICE_TOKEN` — a value listed in this service's
     `SERVICE_TOKENS` env var. Different token per product.
3. Wire the product's route deps to use `current_user` and
   `require_entitlement("meetings")` (or `"takanon"`) from this module.

## When to extract as a package

When two products need to change the SDK in lock-step, or when a third
product enters the picture. Until then, copy-paste keeps the change
surface small.

## API contract stability

Anything under `/api/introspect` and `/api/service/*` is a **stable
contract** — the SDK relies on the response shape. Changing those
response shapes is a coordinated deploy: update identity, update every
product SDK copy, deploy together.
