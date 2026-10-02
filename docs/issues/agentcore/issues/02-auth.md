# 02: Auth

**What to build:** A user can register with email and password and immediately receive a 30-minute access token, log in later to get a fresh one, and use it to call protected endpoints. Missing, malformed or expired tokens are rejected with a 401 in the standard error envelope.

**Blocked by:** 01 (Walking skeleton)

**Status:** done

- [x] Users table (uuid string id, unique indexed email, hashed password, created at) with an Alembic migration
- [x] `POST /api/v1/auth/register` → 201 `{access_token, token_type, expires_in}`; duplicate email → 409 envelope
- [x] `POST /api/v1/auth/token` (OAuth2 password form) → 200 same shape; wrong credentials → 401 envelope
- [x] Access tokens only, 30-minute expiry; passwords hashed with passlib; JWTs via python-jose
- [x] `get_current_user` dependency (Bearer header) injects the User; missing/invalid/expired token → 401 envelope
- [x] User factory fixture that yields a user and a valid token for later tests
- [x] Routes declare response model, status code, error responses and summary
- [x] Tests (HTTP seam): register happy path, duplicate email, login happy path, wrong password, expired token, missing token, malformed token
