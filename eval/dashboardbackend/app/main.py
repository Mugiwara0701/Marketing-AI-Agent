"""Dashboard backend. Supabase handles sign-in; this API verifies the JWT on every protected call."""

import os
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.auth import current_user

load_dotenv()  # reads eval/dashboardbackend/.env when started from that folder

app = FastAPI(title="Dashboard backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        o.strip()
        for o in os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(",")
        if o.strip()
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/auth/me")
def me(user: Annotated[dict, Depends(current_user)]) -> dict:
    return {"id": user["sub"], "email": user.get("email"), "role": user.get("role")}
