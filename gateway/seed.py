"""Idempotent seed system for Hermes.

Rules:
- NEVER overwrites existing records.
- Each seed function is guarded by an emptiness check.
- All seed data is generic and example-friendly — no personal names or real IPs.
- run_seed() is safe to call on every startup; it is a no-op on established deployments.
"""

import logging

import gateway.auth.db as db
from gateway.auth.password import hash_password

logger = logging.getLogger(__name__)

# ── Seed data constants ────────────────────────────────────────────────────────

_ADMIN_EMAIL    = "admin@example.com"
_ADMIN_USERNAME = "admin"
_ADMIN_PASSWORD = "admin"   # Must be changed after first login.
_ADMIN_DISPLAY  = "Admin"


# ── Individual seed functions ──────────────────────────────────────────────────

def seed_admin_user() -> None:
    """Insert a default admin user if no users exist.

    The generated password is intentionally weak — it must be changed
    immediately after first login.
    """
    users, total = db.list_users(page=1, limit=1)
    if total > 0:
        logger.debug("seed_admin_user: skipped (%d users already present)", total)
        return

    user = db.create_user(
        email=_ADMIN_EMAIL,
        username=_ADMIN_USERNAME,
        password_hash=hash_password(_ADMIN_PASSWORD),
        role="admin",
        display_name=_ADMIN_DISPLAY,
    )
    logger.info("seed_admin_user: created '%s' (id=%s)", user["email"], user["id"])
    logger.warning(
        "SEED: default admin account created (admin@example.com / admin). "
        "Change this password immediately."
    )


# ── Public entry point ─────────────────────────────────────────────────────────

def run_seed() -> None:
    """Seed the default admin user. A no-op once any user exists.

    Safe to call on every startup. (v1 also seeded example machines and a
    routing profile here; both were removed in S1.6.)
    """
    seed_admin_user()
