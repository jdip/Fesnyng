"""Organization-scoped department management."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request

from fesnyng_backend import auth
from fesnyng_backend.agent_models import DepartmentCreate, DepartmentResponse, DepartmentUpdate

if TYPE_CHECKING:
    from fesnyng_backend.control_store import ControlPlaneStore

router = APIRouter(prefix="/organizations/{organization_id}", tags=["departments"])


class DepartmentStore:
    def __init__(self, control: ControlPlaneStore):
        self.control = control

    def list_departments(self, organization_id: str) -> list[dict[str, Any]]:
        with self.control.connect() as connection:
            rows = connection.execute(
                "SELECT id,organization_id,name,parent_id,head_agent_id FROM departments "
                "WHERE organization_id=? ORDER BY name,id",
                (organization_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_department(self, organization_id: str, department_id: str) -> dict[str, Any]:
        with self.control.connect() as connection:
            row = _department_row(connection, organization_id, department_id)
        return dict(row)

    def create_department(self, organization_id: str, values: dict[str, Any]) -> dict[str, Any]:
        department = DepartmentCreate.model_validate(values).model_dump(mode="json")
        department_id = str(uuid4())
        with self.control.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _validate_parent(connection, organization_id, department_id, department["parent_id"])
            _validate_head(connection, organization_id, department["head_agent_id"])
            connection.execute(
                "INSERT INTO departments(id,organization_id,name,parent_id,head_agent_id) VALUES(?,?,?,?,?)",
                (
                    department_id,
                    organization_id,
                    department["name"],
                    department["parent_id"],
                    department["head_agent_id"],
                ),
            )
        return self.get_department(organization_id, department_id)

    def update_department(
        self, organization_id: str, department_id: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        values = DepartmentUpdate.model_validate(values).model_dump(mode="json", exclude_unset=True)
        if values.get("name", "present") is None:
            raise ValueError("Department name cannot be null")
        if not values:
            return self.get_department(organization_id, department_id)
        with self.control.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = _department_row(connection, organization_id, department_id)
            parent_id = values.get("parent_id", current["parent_id"])
            head_agent_id = values.get("head_agent_id", current["head_agent_id"])
            _validate_parent(connection, organization_id, department_id, parent_id)
            _validate_head(connection, organization_id, head_agent_id)
            connection.execute(
                "UPDATE departments SET name=?,parent_id=?,head_agent_id=? WHERE organization_id=? AND id=?",
                (
                    values.get("name", current["name"]),
                    parent_id,
                    head_agent_id,
                    organization_id,
                    department_id,
                ),
            )
        return self.get_department(organization_id, department_id)

    def delete_department(self, organization_id: str, department_id: str) -> None:
        with self.control.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _department_row(connection, organization_id, department_id)
            if connection.execute(
                "SELECT 1 FROM departments WHERE organization_id=? AND parent_id=?",
                (organization_id, department_id),
            ).fetchone():
                raise ValueError("Department has child departments")
            if connection.execute(
                "SELECT 1 FROM agents WHERE organization_id=? AND department_id=?",
                (organization_id, department_id),
            ).fetchone():
                raise ValueError("Department has assigned agents")
            connection.execute(
                "DELETE FROM departments WHERE organization_id=? AND id=?",
                (organization_id, department_id),
            )


def _department_row(
    connection: sqlite3.Connection, organization_id: str, department_id: str
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT id,organization_id,name,parent_id,head_agent_id FROM departments "
        "WHERE organization_id=? AND id=?",
        (organization_id, department_id),
    ).fetchone()
    if row is None:
        raise LookupError("Department not found")
    return row


def _validate_parent(
    connection: sqlite3.Connection, organization_id: str, department_id: str, parent_id: str | None
) -> None:
    visited = {department_id}
    while parent_id is not None:
        if parent_id in visited:
            raise ValueError("Department relationship would create a cycle")
        visited.add(parent_id)
        row = connection.execute(
            "SELECT parent_id FROM departments WHERE organization_id=? AND id=?",
            (organization_id, parent_id),
        ).fetchone()
        if row is None:
            raise ValueError("Parent department not found in this organization")
        parent_id = row["parent_id"]


def _validate_head(
    connection: sqlite3.Connection, organization_id: str, head_agent_id: str | None
) -> None:
    if (
        head_agent_id is not None
        and not connection.execute(
            "SELECT 1 FROM agents WHERE organization_id=? AND id=?",
            (organization_id, head_agent_id),
        ).fetchone()
    ):
        raise ValueError("Department head not found in this organization")


@contextmanager
def _domain_errors() -> Iterator[None]:
    try:
        yield
    except LookupError as error:
        raise HTTPException(404, str(error)) from None
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


def _store(request: Request) -> DepartmentStore:
    return DepartmentStore(auth.get_store(request))


@router.get("/departments", response_model=list[DepartmentResponse])
def list_departments(request: Request, organization_id: str) -> list[dict[str, Any]]:
    auth.require_member(request, organization_id)
    return _store(request).list_departments(organization_id)


@router.get("/departments/{department_id}", response_model=DepartmentResponse)
def get_department(request: Request, organization_id: str, department_id: str) -> dict[str, Any]:
    auth.require_member(request, organization_id)
    with _domain_errors():
        return _store(request).get_department(organization_id, department_id)


@router.post("/departments", status_code=201, response_model=DepartmentResponse)
def create_department(
    request: Request, organization_id: str, body: DepartmentCreate
) -> dict[str, Any]:
    auth.require_unsafe_request(request)
    auth.require_manager(request, organization_id)
    with _domain_errors():
        return _store(request).create_department(organization_id, body.model_dump(mode="json"))


@router.patch("/departments/{department_id}", response_model=DepartmentResponse)
def update_department(
    request: Request, organization_id: str, department_id: str, body: DepartmentUpdate
) -> dict[str, Any]:
    auth.require_unsafe_request(request)
    auth.require_manager(request, organization_id)
    with _domain_errors():
        return _store(request).update_department(
            organization_id, department_id, body.model_dump(mode="json", exclude_unset=True)
        )


@router.delete("/departments/{department_id}", status_code=204)
def delete_department(request: Request, organization_id: str, department_id: str) -> None:
    auth.require_unsafe_request(request)
    auth.require_manager(request, organization_id)
    with _domain_errors():
        _store(request).delete_department(organization_id, department_id)
