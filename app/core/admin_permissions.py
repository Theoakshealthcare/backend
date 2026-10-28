"""Admin roles and the permission catalogue (PRD section 8.2).

This module is the code-side source of truth. The Alembic migration that creates the RBAC tables
seeds a frozen copy of it; ``test_admin_rbac`` fails if the two drift, so a new permission always
ships with its own migration.

Where the PRD table says "suspend" (Ops), "cancel-request" (Support) or "request" (Finance), that
is a narrower permission of its own: ``caregivers.suspend``, ``bookings.cancel_request``,
``ledger.adjust_request`` and ``pricing.request``.
"""

from typing import Dict, FrozenSet

SUPER_ADMIN = "super_admin"
OPERATIONS = "operations"
VERIFICATION = "verification"
FINANCE = "finance"
SUPPORT = "support"
CLINICAL_REVIEWER = "clinical_reviewer"
READ_ONLY_ANALYST = "read_only_analyst"

ROLE_NAMES: Dict[str, str] = {
    SUPER_ADMIN: "Super Admin",
    OPERATIONS: "Operations",
    VERIFICATION: "Verification",
    FINANCE: "Finance",
    SUPPORT: "Support",
    CLINICAL_REVIEWER: "Clinical Reviewer",
    READ_ONLY_ANALYST: "Read-only Analyst",
}

# Every permission that exists. Super Admin holds all of them.
ALL_PERMISSIONS: FrozenSet[str] = frozenset({
    "dashboard.read",
    "caregivers.read",
    "caregivers.approve",
    "caregivers.suspend",
    "documents.read",
    "bookings.read",
    "bookings.dispatch",
    "bookings.assign",
    "bookings.cancel",
    "bookings.cancel_request",
    "bookings.address",
    "bookings.correct_status",
    "payments.read",
    "payments.reconcile",
    "refunds.request",
    "refunds.approve",
    "refunds.approve_above_limit",
    "payouts.execute",
    "ledger.adjust",
    "ledger.adjust_request",
    "people.read",
    "people.pii",
    "people.suspend",
    "clinical.read",
    "pricing.write",
    "pricing.request",
    "settings.write",
    "support.inbox",
    "fallguard.write",
    "prescriptions.manage",
    "whatsapp.read",
    "admins.manage",
    "audit.read",
    "exports.pii",
    "careplans.read",
    "careplans.write",
    "careplans.templates",
})

ROLE_PERMISSIONS: Dict[str, FrozenSet[str]] = {
    SUPER_ADMIN: ALL_PERMISSIONS,
    OPERATIONS: frozenset({
        "dashboard.read", "caregivers.read", "caregivers.suspend", "bookings.read",
        "bookings.dispatch", "bookings.assign", "bookings.cancel", "bookings.address",
        "refunds.request", "people.read", "people.pii", "people.suspend", "settings.write",
        "support.inbox", "whatsapp.read",
    }),
    VERIFICATION: frozenset({
        "dashboard.read", "caregivers.read", "caregivers.approve", "caregivers.suspend",
        "documents.read", "people.read",
    }),
    FINANCE: frozenset({
        "dashboard.read", "caregivers.read", "bookings.read", "payments.read",
        "payments.reconcile", "refunds.request", "refunds.approve", "payouts.execute",
        "ledger.adjust_request", "people.read", "pricing.request", "exports.pii", "audit.read",
    }),
    SUPPORT: frozenset({
        "dashboard.read", "caregivers.read", "bookings.read", "bookings.cancel_request",
        "refunds.request", "people.read", "support.inbox", "whatsapp.read",
    }),
    CLINICAL_REVIEWER: frozenset({
        "dashboard.read", "caregivers.read", "documents.read", "bookings.read", "people.read",
        "clinical.read", "support.inbox", "fallguard.write", "prescriptions.manage",
        "careplans.read", "careplans.write", "careplans.templates",
    }),
    READ_ONLY_ANALYST: frozenset({"dashboard.read"}),
}

assert set(ROLE_PERMISSIONS) == set(ROLE_NAMES)
assert all(perms <= ALL_PERMISSIONS for perms in ROLE_PERMISSIONS.values())
