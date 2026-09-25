"""Tests for seed_users() ADMIN rename branch (iteration 6).

Validates:
  * No duplicate ADMIN in the demo organization after seeding
  * RENAME behavior when ADMIN_EMAIL is changed to a fresh unused email
  * EXISTING-EMAIL behavior (no rename/duplication)
  * PASSWORD-UPDATE preserved when hash mismatches
  * Guard: no rename when 0 or >1 ADMINs exist in demo org
  * Other 6 demo users still seeded idempotently
"""
import os
import sys
import uuid
import pytest
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient

sys.path.insert(0, "/app/backend")

import db as db_mod  # noqa: E402
import auth as auth_mod  # noqa: E402
from db import DEMO_ORG_ID  # noqa: E402
from auth import hash_password, verify_password  # noqa: E402


ORIGINAL_ADMIN_EMAIL = "nelson@patika.dev"
ORIGINAL_ADMIN_PASSWORD = "TugmaAdmin!2026"

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _rebind_db():
    """Rebind motor client to the current test's event loop to avoid
    'Event loop is closed' when pytest-asyncio creates a new loop per test."""
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    new_db = client[os.environ["DB_NAME"]]
    db_mod.db = new_db
    db_mod.client = client
    auth_mod.db = new_db
    yield
    client.close()


# Re-import seed_users AFTER rebind at call time via auth_mod.seed_users
async def seed_users():
    return await auth_mod.seed_users()


# Alias db to auth_mod.db inside tests so it uses current-loop client
def _db():
    return auth_mod.db


async def _admin_count():
    return await _db().users.count_documents(
        {"organization_id": DEMO_ORG_ID, "role": "ADMIN"}
    )


async def _get_admin_by_email(email):
    return await _db().users.find_one({"email": email})


@pytest.mark.asyncio
async def test_00_initial_state_single_admin():
    """Baseline: exactly one ADMIN in demo org and it is nelson@patika.dev."""
    # Ensure environment set to canonical admin
    os.environ["ADMIN_EMAIL"] = ORIGINAL_ADMIN_EMAIL
    os.environ["ADMIN_PASSWORD"] = ORIGINAL_ADMIN_PASSWORD
    await seed_users()
    count = await _admin_count()
    assert count == 1, f"Expected 1 ADMIN in demo org, got {count}"
    admin = await _get_admin_by_email(ORIGINAL_ADMIN_EMAIL)
    assert admin is not None
    assert admin["role"] == "ADMIN"
    assert admin["organization_id"] == DEMO_ORG_ID


@pytest.mark.asyncio
async def test_01_rename_when_admin_email_changed_to_fresh_email():
    """Simulate ADMIN_EMAIL set to fresh unused email -> rename in place."""
    admin_before = await _get_admin_by_email(ORIGINAL_ADMIN_EMAIL)
    assert admin_before, "Baseline admin missing"
    orig_id = admin_before["id"]
    orig_hash = admin_before["password_hash"]
    orig_ver = admin_before.get("token_version", 0)
    orig_org = admin_before["organization_id"]

    fresh_email = f"tmp-admin-{uuid.uuid4().hex[:8]}@tugmatest.local"
    # Assert fresh doesn't already exist
    assert await _db().users.find_one({"email": fresh_email}) is None

    os.environ["ADMIN_EMAIL"] = fresh_email
    # Password kept same – shouldn't matter for rename branch
    os.environ["ADMIN_PASSWORD"] = ORIGINAL_ADMIN_PASSWORD

    try:
        await seed_users()

        # Admin count still 1
        count = await _admin_count()
        assert count == 1, f"Duplicate ADMIN created; count={count}"

        # Same id, new email, preserved fields
        renamed = await _get_admin_by_email(fresh_email)
        assert renamed is not None, "Renamed admin not found"
        assert renamed["id"] == orig_id
        assert renamed["password_hash"] == orig_hash
        assert renamed["role"] == "ADMIN"
        assert renamed["organization_id"] == orig_org
        assert renamed.get("token_version", 0) == orig_ver

        # Original email no longer exists
        assert await _get_admin_by_email(ORIGINAL_ADMIN_EMAIL) is None
    finally:
        # Restore: rename back via seed_users using original email
        os.environ["ADMIN_EMAIL"] = ORIGINAL_ADMIN_EMAIL
        os.environ["ADMIN_PASSWORD"] = ORIGINAL_ADMIN_PASSWORD
        await seed_users()

    # Verify restored
    restored = await _get_admin_by_email(ORIGINAL_ADMIN_EMAIL)
    assert restored is not None, "Failed to restore admin email"
    assert restored["id"] == orig_id
    assert restored["password_hash"] == orig_hash
    assert await _admin_count() == 1


@pytest.mark.asyncio
async def test_02_existing_email_no_rename_no_duplicate():
    """When ADMIN_EMAIL already exists, don't rename/duplicate."""
    os.environ["ADMIN_EMAIL"] = ORIGINAL_ADMIN_EMAIL
    os.environ["ADMIN_PASSWORD"] = ORIGINAL_ADMIN_PASSWORD

    admin_before = await _get_admin_by_email(ORIGINAL_ADMIN_EMAIL)
    assert admin_before
    id_before = admin_before["id"]
    hash_before = admin_before["password_hash"]

    await seed_users()
    await seed_users()  # idempotent

    assert await _admin_count() == 1
    admin_after = await _get_admin_by_email(ORIGINAL_ADMIN_EMAIL)
    assert admin_after["id"] == id_before
    # Password hash unchanged when password still verifies
    assert admin_after["password_hash"] == hash_before


@pytest.mark.asyncio
async def test_03_password_update_when_mismatch():
    """When configured ADMIN_PASSWORD differs, hash is updated."""
    admin_before = await _get_admin_by_email(ORIGINAL_ADMIN_EMAIL)
    assert admin_before
    id_before = admin_before["id"]
    hash_before = admin_before["password_hash"]

    new_password = f"TempPwd-{uuid.uuid4().hex[:6]}!A"
    os.environ["ADMIN_EMAIL"] = ORIGINAL_ADMIN_EMAIL
    os.environ["ADMIN_PASSWORD"] = new_password

    try:
        await seed_users()
        admin_after = await _get_admin_by_email(ORIGINAL_ADMIN_EMAIL)
        assert admin_after["id"] == id_before
        assert admin_after["password_hash"] != hash_before
        assert verify_password(new_password, admin_after["password_hash"])
        assert await _admin_count() == 1
    finally:
        # Restore original password hash
        os.environ["ADMIN_PASSWORD"] = ORIGINAL_ADMIN_PASSWORD
        await seed_users()

    admin_restored = await _get_admin_by_email(ORIGINAL_ADMIN_EMAIL)
    assert verify_password(ORIGINAL_ADMIN_PASSWORD, admin_restored["password_hash"])


@pytest.mark.asyncio
async def test_04_guard_when_multiple_admins_do_not_rename():
    """Insert a second ADMIN temporarily -> rename must NOT occur."""
    extra_id = str(uuid.uuid4())
    extra_email = f"extra-admin-{uuid.uuid4().hex[:6]}@tugmatest.local"
    await _db().users.insert_one({
        "id": extra_id, "email": extra_email,
        "password_hash": hash_password("x"), "name": "Extra Admin",
        "role": "ADMIN", "title": "Extra", "organization_id": DEMO_ORG_ID,
        "token_version": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    fresh_email = f"tmp-admin2-{uuid.uuid4().hex[:8]}@tugmatest.local"
    assert await _db().users.find_one({"email": fresh_email}) is None

    os.environ["ADMIN_EMAIL"] = fresh_email
    os.environ["ADMIN_PASSWORD"] = ORIGINAL_ADMIN_PASSWORD

    try:
        # Ensure original admin still exists
        orig_admin = await _get_admin_by_email(ORIGINAL_ADMIN_EMAIL)
        assert orig_admin is not None

        await seed_users()

        # Neither existing admin should have been renamed
        assert await _get_admin_by_email(ORIGINAL_ADMIN_EMAIL) is not None
        assert await _get_admin_by_email(extra_email) is not None
        # A NEW admin was inserted for fresh_email (fallback insert branch)
        inserted = await _get_admin_by_email(fresh_email)
        assert inserted is not None
        assert inserted["role"] == "ADMIN"
        # Now demo org has 3 admins
        assert await _admin_count() == 3
    finally:
        # Cleanup: remove the extra admin AND the inserted fresh_email admin
        await _db().users.delete_one({"id": extra_id})
        await _db().users.delete_one({"email": fresh_email})
        os.environ["ADMIN_EMAIL"] = ORIGINAL_ADMIN_EMAIL
        os.environ["ADMIN_PASSWORD"] = ORIGINAL_ADMIN_PASSWORD
        await seed_users()

    assert await _admin_count() == 1
    assert await _get_admin_by_email(ORIGINAL_ADMIN_EMAIL) is not None


@pytest.mark.asyncio
async def test_05_demo_users_seeded_idempotently():
    """The 6 demo role users are present and re-seeding is idempotent."""
    emails = [
        "compliance@tugmademo.ph", "risk@tugmademo.ph", "ops@tugmademo.ph",
        "finance@tugmademo.ph", "auditor@tugmademo.ph", "viewer@tugmademo.ph",
    ]
    await seed_users()
    counts_before = {}
    for e in emails:
        counts_before[e] = await _db().users.count_documents({"email": e})
        assert counts_before[e] == 1, f"{e} count={counts_before[e]}"

    await seed_users()
    for e in emails:
        assert await _db().users.count_documents({"email": e}) == 1


@pytest.mark.asyncio
async def test_06_final_state_admin_login_intact():
    """Final: admin still logs in with canonical credentials."""
    os.environ["ADMIN_EMAIL"] = ORIGINAL_ADMIN_EMAIL
    os.environ["ADMIN_PASSWORD"] = ORIGINAL_ADMIN_PASSWORD
    await seed_users()
    admin = await _get_admin_by_email(ORIGINAL_ADMIN_EMAIL)
    assert admin is not None
    assert verify_password(ORIGINAL_ADMIN_PASSWORD, admin["password_hash"])
    assert await _admin_count() == 1
