"""Durable host-local peer roster, routing origins, and directional credentials."""

from __future__ import annotations

import json
import secrets
from typing import Any
from uuid import UUID

from fastapi import HTTPException, Request
from pydantic import Field, model_validator

from fesnyng_backend.agent_models import Contract, Name
from fesnyng_backend.control_store import ControlPlaneStore
from fesnyng_backend.host_store import HostStore


class PeerAgent(Contract):
    agent_id: UUID
    name: Name
    title: str = Field(default="", max_length=120)
    host_id: UUID
    reports_to_agent_id: UUID | None = None


class PeerHost(Contract):
    host_id: UUID
    origin: str = Field(min_length=1, max_length=2000, pattern=r"^https?://[^\s]+$")
    outbound_token: str = Field(min_length=32, max_length=512)
    inbound_token: str = Field(min_length=32, max_length=512)


class PeerConfiguration(Contract):
    organization_id: UUID
    host_id: UUID
    version: int = Field(ge=1)
    agents: list[PeerAgent] = Field(default_factory=list)
    peers: list[PeerHost] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_roster_and_peers(self):
        if len({agent.agent_id for agent in self.agents}) != len(self.agents):
            raise ValueError("Peer roster agent ids must be unique")
        if len({peer.host_id for peer in self.peers}) != len(self.peers):
            raise ValueError("Peer host ids must be unique")
        if any(peer.host_id == self.host_id for peer in self.peers):
            raise ValueError("Peer configuration cannot include its own host")
        return self


class PeerConfigurationStore:
    """Own host-local peer routing configuration, separate from management bindings."""

    def __init__(self, host: HostStore):
        self.host = host

    def initialize(self) -> None:
        with self.host.connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS host_peer_configurations (
                    organization_id TEXT PRIMARY KEY REFERENCES host_bindings(organization_id),
                    configuration TEXT NOT NULL
                )"""
            )

    def apply(self, configuration: PeerConfiguration) -> dict[str, Any]:
        if configuration.host_id != self.host.instance_id:
            raise ValueError("Peer configuration belongs to another host")
        organization_id = str(configuration.organization_id)
        encoded = configuration.model_dump_json()
        with self.host.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute(
                "SELECT 1 FROM host_bindings WHERE organization_id=?", (organization_id,)
            ).fetchone():
                raise LookupError("Organization is not bound to host")
            row = connection.execute(
                "SELECT configuration FROM host_peer_configurations WHERE organization_id=?",
                (organization_id,),
            ).fetchone()
            if row is not None:
                current = PeerConfiguration.model_validate_json(row["configuration"])
                if configuration.version < current.version:
                    raise ValueError("Peer configuration version is stale")
                if configuration.version == current.version:
                    if encoded != row["configuration"]:
                        raise ValueError("Peer configuration version conflicts")
                    return _status(current)
            connection.execute(
                "INSERT INTO host_peer_configurations(organization_id,configuration) VALUES(?,?) "
                "ON CONFLICT(organization_id) DO UPDATE SET configuration=excluded.configuration",
                (organization_id, encoded),
            )
        return _status(configuration)

    def get(self, organization_id: str) -> dict[str, Any]:
        with self.host.connect() as connection:
            row = connection.execute(
                "SELECT configuration FROM host_peer_configurations WHERE organization_id=?",
                (organization_id,),
            ).fetchone()
        if row is None:
            raise LookupError("Peer configuration not found")
        return PeerConfiguration.model_validate_json(row["configuration"]).model_dump(mode="json")

    def status(self, organization_id: str) -> dict[str, Any]:
        return _status(PeerConfiguration.model_validate(self.get(organization_id)))

    def authenticate(self, organization_id: str, token: str) -> str | None:
        try:
            configuration = PeerConfiguration.model_validate(self.get(organization_id))
        except LookupError:
            return None
        for peer in configuration.peers:
            if secrets.compare_digest(peer.inbound_token, token):
                return str(peer.host_id)
        return None

    def agent(self, organization_id: str, agent_id: str) -> dict[str, Any]:
        configuration = PeerConfiguration.model_validate(self.get(organization_id))
        for agent in configuration.agents:
            if str(agent.agent_id) == agent_id:
                return agent.model_dump(mode="json")
        raise LookupError("Peer agent not found")

    def connection(self, organization_id: str, target_host: str) -> tuple[str, str]:
        configuration = PeerConfiguration.model_validate(self.get(organization_id))
        for peer in configuration.peers:
            if str(peer.host_id) == target_host:
                return peer.origin, peer.outbound_token
        raise LookupError("Peer host connection not found")


class ControlPeerConfigurationStore:
    """Control-plane desired peer topology and directional host-to-host credentials."""

    def __init__(self, control: ControlPlaneStore):
        self.control = control

    def initialize(self) -> None:
        with self.control.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS peer_configuration_versions (
                    organization_id TEXT NOT NULL REFERENCES organizations(id),
                    version INTEGER NOT NULL,
                    snapshot TEXT NOT NULL,
                    PRIMARY KEY(organization_id, version)
                );
                CREATE TABLE IF NOT EXISTS peer_directional_tokens (
                    organization_id TEXT NOT NULL REFERENCES organizations(id),
                    source_host_id TEXT NOT NULL REFERENCES hosts(id),
                    target_host_id TEXT NOT NULL REFERENCES hosts(id),
                    token TEXT NOT NULL,
                    PRIMARY KEY(organization_id, source_host_id, target_host_id)
                );
                CREATE TABLE IF NOT EXISTS peer_host_applications (
                    organization_id TEXT NOT NULL REFERENCES organizations(id),
                    host_id TEXT NOT NULL REFERENCES hosts(id),
                    desired_version INTEGER NOT NULL,
                    applied_version INTEGER,
                    error TEXT,
                    PRIMARY KEY(organization_id, host_id)
                );
                """
            )

    def desired(self, organization_id: str) -> dict[str, Any]:
        """Snapshot current same-organization placement, advancing only when it changes."""
        self.initialize()
        with self.control.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            agents = [
                dict(row)
                for row in connection.execute(
                    "SELECT id AS agent_id,name,title,host_id,reports_to_agent_id FROM agents "
                    "WHERE organization_id=? ORDER BY id",
                    (organization_id,),
                ).fetchall()
            ]
            hosts = [
                dict(row)
                for row in connection.execute(
                    "SELECT hosts.id AS host_id,hosts.api_url AS origin FROM hosts "
                    "JOIN organization_hosts ON organization_hosts.host_id=hosts.id "
                    "WHERE organization_hosts.organization_id=? ORDER BY hosts.id",
                    (organization_id,),
                ).fetchall()
            ]
            snapshot = {"agents": agents, "hosts": hosts}
            encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
            previous = connection.execute(
                "SELECT version,snapshot FROM peer_configuration_versions WHERE organization_id=? "
                "ORDER BY version DESC LIMIT 1",
                (organization_id,),
            ).fetchone()
            version = (
                previous["version"]
                if previous and previous["snapshot"] == encoded
                else ((previous["version"] if previous else 0) + 1)
            )
            if previous is None or previous["snapshot"] != encoded:
                connection.execute(
                    "INSERT INTO peer_configuration_versions(organization_id,version,snapshot) VALUES(?,?,?)",
                    (organization_id, version, encoded),
                )
            for source in hosts:
                for target in hosts:
                    if source["host_id"] == target["host_id"]:
                        continue
                    connection.execute(
                        "INSERT OR IGNORE INTO peer_directional_tokens "
                        "(organization_id,source_host_id,target_host_id,token) VALUES(?,?,?,?)",
                        (
                            organization_id,
                            source["host_id"],
                            target["host_id"],
                            secrets.token_urlsafe(32),
                        ),
                    )
        return {"organization_id": organization_id, "version": version, **snapshot}

    def configuration_for_host(
        self, organization_id: str, host_id: str, desired: dict[str, Any] | None = None
    ) -> PeerConfiguration:
        desired = desired or self.desired(organization_id)
        host_ids = {host["host_id"] for host in desired["hosts"]}
        if host_id not in host_ids:
            raise LookupError("Host is not bound to organization")
        with self.control.connect() as connection:
            tokens = {
                (row["source_host_id"], row["target_host_id"]): row["token"]
                for row in connection.execute(
                    "SELECT source_host_id,target_host_id,token FROM peer_directional_tokens "
                    "WHERE organization_id=?",
                    (organization_id,),
                ).fetchall()
            }
        peers = [
            PeerHost(
                host_id=host["host_id"],
                origin=host["origin"],
                outbound_token=tokens[(host_id, host["host_id"])],
                inbound_token=tokens[(host["host_id"], host_id)],
            )
            for host in desired["hosts"]
            if host["host_id"] != host_id
        ]
        return PeerConfiguration(
            organization_id=organization_id,
            host_id=host_id,
            version=desired["version"],
            agents=[PeerAgent.model_validate(agent) for agent in desired["agents"]],
            peers=peers,
        )

    def status(self, organization_id: str) -> dict[str, Any]:
        desired = self.desired(organization_id)
        with self.control.connect() as connection:
            applications = {
                row["host_id"]: dict(row)
                for row in connection.execute(
                    "SELECT host_id,desired_version,applied_version,error FROM peer_host_applications "
                    "WHERE organization_id=?",
                    (organization_id,),
                ).fetchall()
            }
        return {
            "organization_id": organization_id,
            "desired_version": desired["version"],
            "agent_count": len(desired["agents"]),
            "hosts": [
                {
                    "host_id": host["host_id"],
                    "desired_version": desired["version"],
                    "applied_version": applications.get(host["host_id"], {}).get("applied_version"),
                    "status": "applied"
                    if applications.get(host["host_id"], {}).get("applied_version")
                    == desired["version"]
                    else "pending",
                    "error": applications.get(host["host_id"], {}).get("error"),
                }
                for host in desired["hosts"]
            ],
        }

    def record_application(
        self,
        organization_id: str,
        host_id: str,
        version: int,
        applied: bool,
    ) -> None:
        with self.control.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT desired_version,applied_version FROM peer_host_applications "
                "WHERE organization_id=? AND host_id=?",
                (organization_id, host_id),
            ).fetchone()
            desired_version = max(current["desired_version"], version) if current else version
            confirmed = current["applied_version"] if current else None
            applied_version = (
                max(confirmed, version)
                if applied and confirmed is not None
                else (version if applied else confirmed)
            )
            error = (
                None
                if applied_version is not None and applied_version >= desired_version
                else "Peer host configuration is pending"
            )
            connection.execute(
                "INSERT INTO peer_host_applications "
                "(organization_id,host_id,desired_version,applied_version,error) VALUES(?,?,?,?,?) "
                "ON CONFLICT(organization_id,host_id) DO UPDATE SET "
                "desired_version=excluded.desired_version,applied_version=excluded.applied_version,"
                "error=excluded.error",
                (
                    organization_id,
                    host_id,
                    desired_version,
                    applied_version,
                    error,
                ),
            )


def _status(configuration: PeerConfiguration) -> dict[str, Any]:
    return {
        "organization_id": str(configuration.organization_id),
        "host_id": str(configuration.host_id),
        "version": configuration.version,
        "agent_count": len(configuration.agents),
        "peer_host_ids": [str(peer.host_id) for peer in configuration.peers],
    }


def require_peer(request: Request, organization_id: str) -> str:
    """Authenticate an inbound peer bearer without accepting a management binding."""
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Peer authentication required")
    source_host = request.app.state.peer_configuration.authenticate(organization_id, token)
    if source_host is None:
        raise HTTPException(401, "Peer authentication required")
    return source_host
