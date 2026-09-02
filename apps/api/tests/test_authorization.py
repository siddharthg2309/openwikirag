import pytest

from openwikirag.security.authorization import (
    AuthorizationService,
    InactivePrincipalError,
    Permission,
    PermissionDeniedError,
    Principal,
    Role,
    TenantAccessDeniedError,
)

AUTHZ = AuthorizationService()


def principal(role: Role, *, tenant_id: str = "tenant-a", active: bool = True) -> Principal:
    return Principal(
        subject_id="user-1",
        tenant_id=tenant_id,
        role=role,
        is_active=active,
    )


def test_viewer_can_read_but_cannot_write_documents() -> None:
    viewer = principal(Role.VIEWER)

    AUTHZ.require(viewer, Permission.READ_DOCUMENTS, resource_tenant_id="tenant-a")

    with pytest.raises(PermissionDeniedError):
        AUTHZ.require(viewer, Permission.WRITE_DOCUMENTS, resource_tenant_id="tenant-a")


def test_editor_can_reindex_but_cannot_manage_members() -> None:
    editor = principal(Role.EDITOR)

    AUTHZ.require(editor, Permission.REINDEX_DOCUMENTS, resource_tenant_id="tenant-a")

    with pytest.raises(PermissionDeniedError):
        AUTHZ.require(editor, Permission.MANAGE_MEMBERS, resource_tenant_id="tenant-a")


def test_admin_can_manage_members_and_delete_documents() -> None:
    admin = principal(Role.ADMIN)

    AUTHZ.require(admin, Permission.MANAGE_MEMBERS, resource_tenant_id="tenant-a")
    AUTHZ.require(admin, Permission.DELETE_DOCUMENTS, resource_tenant_id="tenant-a")


def test_non_operator_cannot_cross_tenant_boundary() -> None:
    viewer = principal(Role.VIEWER, tenant_id="tenant-a")

    with pytest.raises(TenantAccessDeniedError):
        AUTHZ.require(viewer, Permission.READ_DOCUMENTS, resource_tenant_id="tenant-b")


def test_operator_can_access_another_tenant_for_platform_operations() -> None:
    operator = principal(Role.OPERATOR, tenant_id="platform")

    AUTHZ.require(operator, Permission.READ_AUDIT_LOG, resource_tenant_id="tenant-b")


def test_inactive_principal_is_always_denied() -> None:
    inactive_admin = principal(Role.ADMIN, active=False)

    with pytest.raises(InactivePrincipalError):
        AUTHZ.require(inactive_admin, Permission.READ_DOCUMENTS, resource_tenant_id="tenant-a")


def test_has_returns_a_boolean_for_policy_checks() -> None:
    viewer = principal(Role.VIEWER)

    assert AUTHZ.has(viewer, Permission.READ_DOCUMENTS, resource_tenant_id="tenant-a")
    assert not AUTHZ.has(viewer, Permission.DELETE_DOCUMENTS, resource_tenant_id="tenant-a")
