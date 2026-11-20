"""Names and ownership rules shared by setup and cleanup.

A Cloudflare account can hold more than one lab. Each lab is one domain on
one server. These helpers decide which login provider, policy, DNS name,
and route belong to that lab.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re

SUBNET = "172.18.0.0/16"


def server_name(hostname: str) -> str:
    raw = hostname.split(".")[0].lower()
    name = re.sub(r"[^a-z0-9-]", "-", raw).strip("-") or "lab"
    return name[:20]


def idp_name(domain: str) -> str:
    return f"Authentik ({domain})"


def policy_name(domain: str) -> str:
    return f"Allow lab admin ({domain})"


def authentik_base(domain: str) -> str:
    return f"https://authentik.{domain}/"


def idp_urls(provider: dict) -> str:
    config = provider.get("config") or {}
    return " ".join(str(config.get(key) or "") for key in ("auth_url", "token_url", "certs_url"))


def idp_for_domain(providers: list[dict], domain: str) -> dict | None:
    marker = authentik_base(domain)
    wanted = idp_name(domain)
    for item in providers:
        if item.get("type") != "oidc":
            continue
        if item.get("name") == wanted or marker in idp_urls(item):
            return item
    return None


def idp_ids_to_delete(providers: list[dict], domain: str, state_idp_id: str = "") -> list[str]:
    marker = authentik_base(domain)
    wanted = idp_name(domain)
    found = []
    for item in providers:
        if item.get("type") != "oidc":
            continue
        urls = idp_urls(item)
        name = item.get("name") or ""
        if name == wanted or marker in urls:
            found.append(item["id"])
            continue
        if item.get("id") == state_idp_id and name == "Authentik" and (not urls.strip() or marker in urls):
            found.append(item["id"])
    return found


def policy_requires_idp(policy: dict, idp_id: str) -> bool:
    for rule in policy.get("require") or []:
        if not isinstance(rule, dict):
            continue
        login = rule.get("login_method") or {}
        if isinstance(login, dict) and login.get("id") == idp_id:
            return True
    return False


def policy_email(policy: dict) -> str:
    for rule in policy.get("include") or []:
        if not isinstance(rule, dict):
            continue
        email = (rule.get("email") or {}).get("email")
        if email:
            return email
    return ""


def policy_for_domain(policies: list[dict], domain: str, idp_id: str) -> dict | None:
    wanted = policy_name(domain)
    legacy = None
    for item in policies:
        if item.get("name") == wanted:
            return item
        if item.get("name") == "Allow lab admin" and policy_requires_idp(item, idp_id):
            legacy = item
    return legacy


def policy_ids_to_delete(policies: list[dict], domain: str, idp_ids: set[str], state_policy_id: str = "") -> list[str]:
    wanted = policy_name(domain)
    found = []
    for item in policies:
        name = item.get("name") or ""
        if name == wanted:
            found.append(item["id"])
            continue
        matches_idp = any(policy_requires_idp(item, idp_id) for idp_id in idp_ids)
        if name == "Allow lab admin" and (item.get("id") == state_policy_id or matches_idp):
            found.append(item["id"])
    return found


def host_octet(server: str) -> int:
    digest = hashlib.sha256(server.encode()).hexdigest()
    return 101 + (int(digest[:8], 16) % 150)


def vm_address(server: str, used: set[str]) -> str:
    network = ipaddress.ip_network(SUBNET)
    octet = host_octet(server)
    for last in range(11, 250):
        address = ipaddress.ip_address(int(network.network_address) + 256 * octet + last)
        if address in network and str(address) not in used:
            used.add(str(address))
            return str(address)
    raise ValueError(f"No free address left for server {server}.")


def owned_names(domain: str, server: str, vms: list[dict]) -> set[str]:
    names = {f"authentik.{domain}", f"traefik.{domain}"}
    for vm in vms:
        hostname = vm["hostname"]
        names.add(f"{hostname}.{domain}")
        names.add(f"rdp-{hostname}.{domain}")
        names.add(f"rdp-{server}-{hostname}.{domain}")
        names.add(f"{server}-{hostname}.{domain}")
        published = vm.get("published") or ""
        if published.endswith("." + domain):
            names.add(published)
    return names


def foreign_route(routes: list[dict], network: str, tunnel_id: str) -> dict | None:
    for route in routes:
        if route.get("network") != network:
            continue
        if route.get("tunnel_id") != tunnel_id:
            return route
        return None
    return None


def launcher_idp_ids(app: dict) -> list[str]:
    found = []
    for item in app.get("allowed_idps") or []:
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, dict) and item.get("id"):
            found.append(item["id"])
    return found


def should_delete_launcher(apps: list[dict], names: set[str]) -> bool:
    for app in apps:
        if app.get("type") == "app_launcher":
            continue
        domain_name = app.get("domain") or ""
        destinations = {item.get("uri") for item in app.get("destinations") or []}
        if domain_name in names or destinations & names:
            continue
        return False
    return True


def should_delete_gateway(tunnels: list[dict], tunnel_id: str) -> bool:
    return not any(item.get("id") != tunnel_id for item in tunnels)
