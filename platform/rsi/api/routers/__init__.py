"""rsi.api.routers: endpoint modules (DD-13 §2)."""

from rsi.api.routers import auth, batches, evalsets, profiles, runs, suts

ALL_ROUTERS = [auth.router, suts.router, evalsets.router, profiles.router,
               batches.router, runs.router]

__all__ = ["ALL_ROUTERS", "auth", "batches", "evalsets", "profiles", "runs", "suts"]
