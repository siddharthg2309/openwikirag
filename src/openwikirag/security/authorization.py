from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    """Roles are coarse-grained bundles of permissions within one tenant."""

    VIEWER = "viewer"
    EDITOR = "editor"
    ADMIN = "admin"
    OPERATOR = "operator"


class Permission(StrEnum):
    """Actions that application use cases can authorize."""

    READ_DOCUMENTS = "documents:read"
    REVIEW_WIKI_PAGES = "wiki:review"
    WRITE_DOCUMENTS = "documents:write"
    DELETE_DOCUMENTS = "documents:delete"
    REINDEX_DOCUMENTS = "documents:reindex"
    MANAGE_MEMBERS = "members:manage"
    READ_AUDIT_LOG = "audit:read"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset({Permission.READ_DOCUMENTS}),
    Role.EDITOR: frozenset(
        {
            Permission.READ_DOCUMENTS,
            Permission.REVIEW_WIKI_PAGES,
            Permission.WRITE_DOCUMENTS,
            Permission.REINDEX_DOCUMENTS,
        }
    ),
    Role.ADMIN: frozenset(
        {
            Permission.READ_DOCUMENTS,
            Permission.REVIEW_WIKI_PAGES,
            Permission.WRITE_DOCUMENTS,
            Permission.DELETE_DOCUMENTS,
            Permission.REINDEX_DOCUMENTS,
            Permission.MANAGE_MEMBERS,
            Permission.READ_AUDIT_LOG,
        }
    ),
    # Operators are trusted platform identities. Their cross-tenant behavior
    # will later be restricted further by an explicit operator policy and audit.
    Role.OPERATOR: frozenset(Permission),
}


@dataclass(frozen=True, slots=True)
class Principal:
    """Authenticated identity plus the tenant membership used for a request."""

    subject_id: str
    tenant_id: str
    role: Role
    is_active: bool = True


class AuthorizationError(Exception):
    """Base error for authorization failures."""


class InactivePrincipalError(AuthorizationError):
    """Raised when a disabled identity attempts an operation."""


class TenantAccessDeniedError(AuthorizationError):
    """Raised when a principal crosses its tenant boundary."""


class PermissionDeniedError(AuthorizationError):
    """Raised when a role does not contain a requested permission."""


class AuthorizationService:
    """Central policy evaluator used by application use cases.

    Controllers should not duplicate role checks. They should call this
    service, so a later move from RBAC to a richer policy model has one seam.
    """

    def require(
        self,
        principal: Principal,
        permission: Permission,
        *,
        resource_tenant_id: str | None = None,
    ) -> None:
        if not principal.is_active:
            raise InactivePrincipalError("The principal is inactive.")

        if resource_tenant_id is not None:
            is_operator = principal.role is Role.OPERATOR
            same_tenant = principal.tenant_id == resource_tenant_id
            if not is_operator and not same_tenant:
                raise TenantAccessDeniedError("The principal cannot access this tenant.")

        if permission not in ROLE_PERMISSIONS[principal.role]:
            raise PermissionDeniedError(
                f"Role '{principal.role.value}' lacks permission '{permission.value}'."
            )

    def has(
        self,
        principal: Principal,
        permission: Permission,
        *,
        resource_tenant_id: str | None = None,
    ) -> bool:
        try:
            self.require(
                principal,
                permission,
                resource_tenant_id=resource_tenant_id,
            )
        except AuthorizationError:
            return False
        return True
