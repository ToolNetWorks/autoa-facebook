"""Which workspace a request acts on.

The business rule is 1 Pipeline = 1 CTV = 1 Workspace. A collaborator belongs to
exactly one workspace, and that workspace comes from the authenticated session
and from nowhere else. A request body or a URL that names another workspace can
never move a collaborator's data, and it can never make their own workspace
disappear either.

An administrator manages several workspaces and names the one they mean.
"""
from __future__ import annotations

from auth import Principal


class WorkspaceRequired(Exception):
    """The administrator did not name a workspace."""


class NoWorkspace(Exception):
    """The caller does not belong to any workspace."""


def workspace_for(principal: Principal, requested: int | None = None) -> int:
    """The one workspace the caller writes to.

    A collaborator writes to the workspace of their session. An administrator
    names the workspace they mean.
    """
    if principal.is_admin:
        if requested is None:
            raise WorkspaceRequired("Thiếu pipeline_id")
        return int(requested)
    if principal.pipeline_id is None:
        raise NoWorkspace("Tài khoản chưa được gán Workspace")
    return int(principal.pipeline_id)


def workspace_filter(principal: Principal, requested: int | None = None) -> int | None:
    """The workspace a list request is limited to.

    ``None`` means every workspace, which only an administrator may ask for. A
    collaborator always reads their own workspace, so a link that still points
    at another one cannot empty their list nor expose a foreign workspace.
    """
    if principal.is_admin:
        return int(requested) if requested is not None else None
    if principal.pipeline_id is None:
        raise NoWorkspace("Tài khoản chưa được gán Workspace")
    return int(principal.pipeline_id)


def scope_list(principal: Principal, requested: int | None, column: str,
               owner_column: str) -> tuple[str, list]:
    """The WHERE fragment and its parameters that limit a list to one workspace.

    Two rules meet here.

    The workspace comes from the session for a collaborator, so a link that
    still points at another workspace can neither empty their list nor expose
    a foreign one.

    The owner column stays a filter, because two collaborators of the same
    workspace must not read each other's Facebook cookies and sessions. With
    one collaborator per workspace, which is the rule of the product, the
    shared pool plus the collaborator's own rows is the whole workspace, so
    nothing of the workspace is ever missing from the list.

    The administrator supervises the whole workspace and is not filtered by
    ownership.
    """
    workspace = workspace_filter(principal, requested)
    if principal.is_admin:
        if workspace is None:
            return "1=1", []
        return f"{column}=?", [workspace]
    if workspace is None:
        raise NoWorkspace("Tài khoản chưa được gán Workspace")
    return (f"({column}=? AND ({owner_column} IS NULL OR {owner_column}=?))",
            [workspace, principal.user_id])
