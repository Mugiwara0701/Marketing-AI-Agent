# Dashboard backend

Python (FastAPI) API for the dashboard. Sign-in, sign-out and password reset stay with Supabase Auth
(the browser talks to it directly); this service verifies the Supabase access token on every protected call.

| Route          | Auth   | Purpose                                   |
|----------------|--------|-------------------------------------------|
| `GET /health`  | none   | liveness                                  |
| `GET /auth/me` | Bearer | returns `id`, `email`, `role` of the user |

Protect a new route with `user: dict = Depends(current_user)` (`app/auth.py`).

## Run

```bash
cd eval/dashboardbackend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in SUPABASE_URL, then export the vars (or use your process manager)
uvicorn app.main:app --reload --port 8000
pytest
```

Tokens are verified against the project's JWKS (`/auth/v1/.well-known/jwks.json`); set `SUPABASE_JWT_SECRET`
only for legacy HS256 projects. The dashboard sends `Authorization: Bearer <session.access_token>`.
