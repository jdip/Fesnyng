# Private service deployment

Fesnyng records service endpoints and their associations. The deployment owner
configures routing, firewall rules and access. Registering a URL does not publish
a port, configure Tailscale, start a service or grant network access.

From an employee's **Resources** view, use **Services** to register an employee
application or an existing registered container. Add a name, an HTTP(S) endpoint,
a custom/Tailscale route and any shared Project/thread associations. Keep tokens,
passwords and signed access grants out of the URL: organization members can read
the record. Editing or unregistering a record changes metadata only. Missing or
unavailable targets remain visible and their retained links can be edited or
detached. Refresh before confirming an operation if another member changed it.

Use one Linux organization work host with Docker and, optionally, Tailscale on
the host. An employee with raw Docker access administers that entire engine;
follow the [dedicated organization boundary](docker-resources.md). No additional
VM or Docker-in-Docker daemon is required.

## Supported private profile

This profile uses Linux, systemd, UFW with IPv6 enabled, and Docker's **iptables
firewall backend** (including ip6tables). It covers ordinary bridge port
publication and host-network application listeners on the explicitly reviewed
external interfaces. It does not claim support for Docker's nftables backend,
macvlan/ipvlan, arbitrary tunnels, newly added interfaces or deliberate changes
by an engine administrator. Review those configurations separately before use.

There are two ingress paths to protect:

- Host services and host-network containers traverse host input filtering.
  Configure UFW to deny incoming and routed traffic by default in both address
  families, with explicit administration and intended tailnet allowances.
- Published bridge ports traverse Docker forwarding. Add the deployment-owned
  restrictions before Docker's accept rules, through `DOCKER-USER` for **both**
  iptables and ip6tables. UFW incoming defaults alone do not protect this path.

Docker documents the [UFW interaction and networking-mode limits](https://docs.docker.com/engine/network/packet-filtering-firewalls/)
and the [DOCKER-USER processing order](https://docs.docker.com/engine/network/firewall-iptables/).
Keep Docker's own firewall management enabled. Do not flush Docker or Tailscale
chains, disable their firewall management, or append restrictions after Docker's
forwarding accepts.

The deployment's guard must preserve established replies, allow intended
`tailscale0` ingress, and reject new direct ingress from **every** reviewed
non-tailnet interface before returning to Docker's ordinary rules. An interface
list is part of the deployment contract: adding another physical interface,
VPN, bridge or routing mode requires a new review and connectivity proof.
This is protection against accidental direct publication, not a restriction on
a privileged employee deliberately reconfiguring the host or opening a tunnel.

Keep firewall configuration in root-owned deployment files. Docker startup must
require the host firewall and install or verify the guard before workloads can
start. Preserve this ordering across daemon and host restart. After a firewall
reload, inspect both families and repeat the connectivity checks; a service's
active systemd state alone does not prove rule ordering or reachability.

### Exercised guard and startup ordering

The validated deployment uses a root-owned Bash guard at
`/usr/local/sbin/fesnyng-docker-guard`. It checks both `/usr/sbin/iptables` and
`/usr/sbin/ip6tables`, creates its chains when absent, and verifies the exact
expected rule ordering before allowing Docker to start. It refuses unexpected
existing state instead of flushing shared rules. Its effective rules in each
family are below; `EXTERNAL_IF` is a placeholder for the reviewed physical
interface, not a literal interface name to install:

```text
DOCKER-USER: first rule jumps to FESNYNG-IN
FESNYNG-IN:
  -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN
  -i tailscale0 -j RETURN
  -i EXTERNAL_IF -j DROP
  -j RETURN
```

The Docker unit's deployment-owned drop-in contains:

```ini
[Unit]
After=ufw.service
Requires=ufw.service

[Service]
ExecStartPre=/usr/local/sbin/fesnyng-docker-guard
```

UFW's IPv4/IPv6 incoming and routed defaults are deny, with `tailscale0` access
and explicit administration/transport exceptions. Preserve required SSH access
and Tailscale's transport allowance while installing this profile; those are
intentional host exceptions, not application ports. The deployment owner reviews
the complete UFW rules, including any older broad allows.

A standalone UFW reload does not rerun Docker's `ExecStartPre`. Recheck the
`DOCKER-USER` first jump and complete guard in both families after a reload,
then run the client probes. Docker restart reruns the guard. Keep these files
and their backups under the host deployment owner; restoring an old unguarded
firewall is not proof cleanup.

## Optional host Tailscale route

Install and authenticate Tailscale on the organization work host using the
administrator's existing deployment process. Configure tailnet access policies
for the intended users. Fesnyng membership and tailnet membership are separate:
a user may see a registered service but lack the network permission to open it.

For an HTTP application, keep its host listener on loopback and configure an
explicit [Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve)
route. For example, after independently verifying an application on loopback:

```bash
tailscale serve --bg --https=8443 http://127.0.0.1:8080
tailscale serve status --json
```

Choose available ports under the organization's workflow. Use the actual URL
reported by the local installation; the example does not prescribe a port
allocation policy. HTTPS Serve requires the tailnet's HTTPS configuration.
Do not enable Funnel for this private profile. Registration never enables it.

To let the agent host report whether a registered endpoint matches an existing
Serve route, set its administrator-owned environment configuration, using the
actual organization UUID:

```bash
export FESNYNG_AGENT_HOST_TAILSCALE_SERVE='{"organization_id":"11111111-1111-4111-8111-111111111111"}'
```

The default read-only command is `tailscale serve status --json`. If the host
service needs a deployment-managed wrapper or narrowly configured noninteractive
sudo permission, supply its argument vector as the `command` property in the same
JSON setting. Never accept that setting from an employee or browser request.
The integration is optional and scoped to the configured organization; other
organizations do not receive this host's Serve status. It returns matching-route
status rather than the full tailnet or Serve inventory. Missing integration,
unreadable state, protocol mismatch or Funnel exposure is reported as unavailable.
Even a configured route leaves client reachability explicitly unverified.

A sibling container can publish its application port to host loopback. For an
application running inside an employee, expose only that application's listener
through a deployment-owned loopback proxy or another reviewed route. Never
route the employee's native harness API (port 4096), host API or Docker socket
as an application service. A proxy pointing to a container's bridge IP must be
updated if that IP changes; the organization workflow owns that lifecycle.

Alternative administrator-configured HTTPS proxies, VPNs and other networking
remain supported through custom endpoint registration. Their network policies
and validation belong to their deployment owner.

## Verify the actual network boundary

Before a proof, identify exact disposable containers, networks, routes and ports,
record their ownership, and establish teardown. Preserve shared services and
existing firewall/Serve configuration. Never use a broad Docker prune or a global
Serve reset for cleanup.

Use two independent listeners: a bridge container with a broadly published port,
and a host-network listener. Verify each listener locally before testing denial,
so a stopped application cannot produce a false firewall success. Use a client
with a real LAN route and a client permitted through the tailnet; a single
computer may supply both paths if the destination addresses select them.

| Check for each listener | IPv4 | IPv6 |
| --- | --- | --- |
| Host-local application response | Required | Required |
| Direct non-tailnet LAN ingress | Denied | Denied |
| Intended tailnet ingress | Allowed | Allowed |

Repeat the full matrix after container restart, Docker daemon restart, and host
reboot. Confirm the firewall chains and startup ordering each time. Exercise a
registered application endpoint from the user's network as well; a configured
Serve route or running container does not prove its upstream application works.
Test the actual response, not just an open TCP port.

On a publicly reachable deployment, also test direct IPv4/IPv6 ingress from an
independent outside-Internet client after each restart boundary. Private LAN and
ULA results are not public-Internet evidence. If a family or route cannot be
tested, record that gap and do not claim the complete boundary is verified.

On success, remove only exact proof containers/networks and the exact temporary
Serve route, restoring any prior route configuration. Keep the approved firewall
protection active. On failure, stop owned compute and retain the necessary private
diagnostics for investigation. Do not publish host inventories, private addresses,
raw firewall dumps or tailnet identifiers in issue/PR evidence.
