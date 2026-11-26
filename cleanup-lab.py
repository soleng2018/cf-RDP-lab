#!/usr/bin/env python3
"""Remove the lab created by setup-lab.py on this server.

Run it with no extra arguments. The zone and API token are the ones
setup-lab.py saved. Names it did not create on that zone are left as they are.
The tunnel removed is <hostname>-tunnel. Containers that setup-lab.py did not
create are left running.
"""

from __future__ import annotations

import json
import os
import pwd
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import lab_scope

LABS = Path(__file__).resolve().parent
STATE_PATH = LABS / ".setup-lab-state.json"
CRED_PATH = LABS / ".setup-lab-credentials"
TOKEN_PATH = LABS / "traefik" / "secrets" / "cf_api.secret"


def operator_home() -> Path:
    sudo_user = os.environ.get("SUDO_USER", "").strip()
    if sudo_user:
        try:
            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except KeyError:
            pass
    return Path.home()


CLIENTS = Path(os.environ.get("DOCKER_CLIENTS_DIR", str(operator_home() / "docker-clients")))
PARAMS = CLIENTS / "parameters.txt"
CF = "https://api.cloudflare.com/client/v4"
OWNED_CONTAINERS = (
    "traefik",
    "authentik-server",
    "authentik-worker",
    "authentik-db",
    "authentik-redis",
    "cloudflared-tunnel",
)


class CleanupError(Exception):
    pass


def log(message: str) -> None:
    print(message, flush=True)


def die(message: str) -> None:
    raise SystemExit(message)


def run(args: list[str]) -> None:
    result = subprocess.run(args, text=True)
    if result.returncode != 0:
        raise CleanupError(f"Command failed: {' '.join(args)}")


def testbed_name() -> str:
    return lab_scope.server_name(socket.gethostname())


def cf(token: str, method: str, path: str, body=None, ok_404: bool = False):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(CF + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        if ok_404 and exc.code == 404:
            return None
        raw = exc.read().decode()
        raise CleanupError(f"Cloudflare {method} {path} failed ({exc.code}): {raw[:400]}")
    if isinstance(payload, dict) and payload.get("success") is False:
        errors = payload.get("errors") or []
        detail = "; ".join(item.get("message", "") for item in errors)
        raise CleanupError(f"Cloudflare {method} {path} failed: {detail}")
    if isinstance(payload, dict) and "result" in payload:
        return payload["result"]
    return payload


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def vm_records(bed: str) -> list[dict]:
    if not PARAMS.exists():
        return []
    found = []
    for line in PARAMS.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw = [part.strip() for part in stripped.split("=", 1)]
        if not key.startswith(bed + "/"):
            continue
        parts = [part.strip().strip('"') for part in raw.split(",")]
        hostname = key.split("/", 1)[1].replace("/", "-")
        ip = next((part.split("=", 1)[1] for part in parts if part.startswith("ip=")), "")
        published = parts[8] if len(parts) > 8 else ""
        found.append({"key": key, "hostname": hostname, "ip": ip, "published": published})
    return found


def owned_names(domain: str, bed: str, vms: list[dict]) -> set[str]:
    return lab_scope.owned_names(domain, bed, vms)


def remove_container(name: str) -> None:
    inspect = subprocess.run(["docker", "inspect", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if inspect.returncode != 0:
        return
    subprocess.run(
        ["docker", "rm", "-f", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    log(f"Removed container {name}.")


def save_windows_installer(bed: str, hostname: str) -> bool:
    storage = CLIENTS.parent / bed / hostname / "windows"
    if not storage.is_dir():
        return True
    isos = [
        path for path in storage.iterdir()
        if path.is_file() and path.suffix.lower() == ".iso" and path.stat().st_size >= 100 * 1024 * 1024
    ]
    if not isos:
        return True
    cache = CLIENTS / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    for iso in isos:
        dest = cache / iso.name
        if dest.exists() and dest.stat().st_size == iso.stat().st_size:
            log(f"Windows installer {iso.name} is already saved.")
            continue
        log(f"Saving Windows installer {iso.name} before this VM is removed.")
        temporary = dest.with_name(dest.name + ".part")
        try:
            shutil.copy2(iso, temporary)
            os.replace(temporary, dest)
        except OSError as exc:
            if temporary.exists():
                temporary.unlink()
            log(f"Could not save {iso.name} ({exc}). This VM folder was left in place.")
            return False
        log(f"Saved Windows installer to {dest}.")
    return True


def remove_tree(path: Path) -> None:
    if not path.exists():
        return
    try:
        if path.is_dir():
            for child in sorted(path.rglob("*"), reverse=True):
                if child.is_file() or child.is_symlink():
                    child.unlink()
                else:
                    child.rmdir()
            path.rmdir()
        else:
            path.unlink()
    except OSError:
        run([
            "docker", "run", "--rm", "-v", f"{path.parent}:/cleanup", "alpine:3.20",
            "rm", "-rf", f"/cleanup/{path.name}",
        ])
    log(f"Removed {path}.")


def local_cleanup(bed: str, vms: list[dict], keep_saved: bool = False) -> None:
    for vm in vms:
        remove_container(f"{bed}-{vm['hostname']}")
    for name in OWNED_CONTAINERS:
        remove_container(name)
    for directory in (LABS / "authentik", LABS / "traefik", LABS / "cloudflared"):
        compose = directory / "docker-compose.yml"
        if compose.exists():
            subprocess.run(["docker", "compose", "-f", str(compose), "down", "-v"], check=False)
    inspect = subprocess.run(["docker", "network", "inspect", "traefik"], capture_output=True, text=True)
    if inspect.returncode == 0:
        try:
            attached = json.loads(inspect.stdout)[0].get("Containers") or {}
        except (json.JSONDecodeError, IndexError):
            attached = {}
        if not attached:
            subprocess.run(["docker", "network", "rm", "traefik"], check=False)
            log("Removed Docker network traefik.")
        else:
            log("Left Docker network traefik in place because another container is using it.")
    for vm in vms:
        if save_windows_installer(bed, vm["hostname"]):
            remove_tree(CLIENTS.parent / bed / vm["hostname"])
        else:
            log(f"Left {vm['hostname']} on disk so the Windows installer is not lost.")
    if PARAMS.exists() and vms:
        kept = []
        keys = {vm["key"] for vm in vms}
        for line in PARAMS.read_text().splitlines(keepends=True):
            key = line.split("=", 1)[0].strip() if "=" in line else ""
            if key in keys:
                continue
            kept.append(line)
        PARAMS.write_text("".join(kept))
        log(f"Removed this server's VM lines from {PARAMS}.")
    for path in (STATE_PATH, CRED_PATH):
        if keep_saved and path == STATE_PATH:
            continue
        if path.exists():
            path.unlink()
            log(f"Removed {path.name}.")
    bed_dir = CLIENTS.parent / bed
    if bed_dir.exists() and not any(bed_dir.iterdir()):
        bed_dir.rmdir()
    for directory in (LABS / "authentik", LABS / "traefik", LABS / "cloudflared"):
        if keep_saved and directory == TOKEN_PATH.parent.parent:
            continue
        remove_tree(directory)
    if keep_saved:
        log("The saved zone and API token were kept so cleanup can be run again.")


def cloud_cleanup(token: str, domain: str, bed: str, vms: list[dict], state: dict) -> None:
    zones = cf(token, "GET", "/zones?" + urllib.parse.urlencode({"name": domain})) or []
    if not zones:
        log(f"Zone {domain} was not found. Cloudflare records were left as they are.")
        return
    account = zones[0]["account"]["id"]
    zone = zones[0]["id"]
    tunnel_name = f"{bed}-tunnel"
    tunnels = cf(token, "GET", f"/accounts/{account}/cfd_tunnel?is_deleted=false") or []
    tunnel = next((item for item in tunnels if item.get("name") == tunnel_name), None)
    if tunnel is None and state.get("tunnel_id"):
        tunnel = cf(token, "GET", f"/accounts/{account}/cfd_tunnel/{state['tunnel_id']}", ok_404=True)
        if tunnel and tunnel.get("name") != tunnel_name:
            tunnel = None
    tunnel_id = tunnel.get("id") if tunnel else ""
    names = owned_names(domain, bed, vms)
    vm_names = {vm["hostname"] for vm in vms}
    vm_names.update(f"{bed}-{vm['hostname']}" for vm in vms)
    vm_ips = {f"{vm['ip']}/32" for vm in vms if vm.get("ip")}

    records = cf(token, "GET", f"/zones/{zone}/dns_records?per_page=200") or []
    for record in records:
        if record.get("name") not in names:
            continue
        cf(token, "DELETE", f"/zones/{zone}/dns_records/{record['id']}")
        log(f"Deleted DNS {record.get('name')}.")

    apps = cf(token, "GET", f"/accounts/{account}/access/apps?per_page=50") or []
    for app in apps:
        domain_name = app.get("domain") or ""
        destinations = {item.get("uri") for item in app.get("destinations") or []}
        if domain_name not in names and not destinations & names:
            continue
        cf(token, "DELETE", f"/accounts/{account}/access/apps/{app['id']}", ok_404=True)
        log(f"Deleted Access application {app.get('name') or domain_name}.")
    if lab_scope.should_delete_launcher(apps, names):
        for app in apps:
            if app.get("type") != "app_launcher":
                continue
            cf(token, "DELETE", f"/accounts/{account}/access/apps/{app['id']}", ok_404=True)
            log("Deleted the App Launcher.")

    if tunnel_id:
        routes = cf(token, "GET", f"/accounts/{account}/teamnet/routes") or []
        for route in routes:
            if route.get("tunnel_id") != tunnel_id:
                continue
            if route.get("network") not in vm_ips and route.get("comment") not in vm_names:
                continue
            cf(token, "DELETE", f"/accounts/{account}/teamnet/routes/{route['id']}", ok_404=True)
            log(f"Deleted private route {route.get('network')}.")
        targets = cf(token, "GET", f"/accounts/{account}/infrastructure/targets") or []
        owned_targets = {f"{bed}-{vm['hostname']}" for vm in vms}
        for target in targets:
            address = ((target.get("ip") or {}).get("ipv4") or {}).get("ip_addr") or ""
            hostname = target.get("hostname") or ""
            if hostname not in owned_targets and f"{address}/32" not in vm_ips:
                continue
            cf(token, "DELETE", f"/accounts/{account}/infrastructure/targets/{target['id']}", ok_404=True)
            log(f"Deleted target {hostname}.")

    tunnel_error = None
    if tunnel_id:
        try:
            delete_tunnel(token, account, tunnel_id, tunnel_name)
        except CleanupError as exc:
            tunnel_error = exc

    providers = cf(token, "GET", f"/accounts/{account}/access/identity_providers?per_page=50") or []
    idp_ids = lab_scope.idp_ids_to_delete(providers, domain, str(state.get("idp_id") or ""))
    for idp_id in idp_ids:
        cf(token, "DELETE", f"/accounts/{account}/access/identity_providers/{idp_id}", ok_404=True)
        log(f"Deleted the identity provider for {domain}.")
    policies = cf(token, "GET", f"/accounts/{account}/access/policies?per_page=50") or []
    for policy_id in lab_scope.policy_ids_to_delete(policies, domain, set(idp_ids), str(state.get("policy_id") or "")):
        cf(token, "DELETE", f"/accounts/{account}/access/policies/{policy_id}", ok_404=True)
        log(f"Deleted the Allow policy for {domain}.")
    if lab_scope.should_delete_gateway(tunnels, tunnel_id):
        rules = cf(token, "GET", f"/accounts/{account}/gateway/rules") or []
        for rule in rules:
            if rule.get("name") != "Allow infrastructure targets":
                continue
            cf(token, "DELETE", f"/accounts/{account}/gateway/rules/{rule['id']}", ok_404=True)
            log("Deleted the Gateway allow rule for infrastructure targets.")
    else:
        log("Left the Gateway allow rule in place because another tunnel is still on this account.")
    if tunnel_error:
        raise tunnel_error


def delete_tunnel(token: str, account: str, tunnel_id: str, tunnel_name: str) -> None:
    remove_container("cloudflared-tunnel")
    last_error = None
    for attempt in range(8):
        try:
            cf(token, "DELETE", f"/accounts/{account}/cfd_tunnel/{tunnel_id}", ok_404=True)
            log(f"Deleted tunnel {tunnel_name}.")
            return
        except CleanupError as exc:
            last_error = exc
            if "active connections" not in str(exc):
                raise
            if attempt == 7:
                break
            log("The tunnel still has a connector. Waiting for it to disconnect.")
            time.sleep(10)
    raise CleanupError(str(last_error))


def saved_token() -> str:
    if not TOKEN_PATH.exists():
        return ""
    return TOKEN_PATH.read_text().strip()


def main() -> None:
    bed = testbed_name()
    state = load_state()
    vms = vm_records(bed)
    domain = str(state.get("domain") or "").strip().lower().rstrip(".")
    token = saved_token()
    log(f"Cleaning the lab created on server {bed}.")
    cloud_failed = False
    if domain and token:
        try:
            cloud_cleanup(token, domain, bed, vms, state)
        except CleanupError as exc:
            cloud_failed = True
            log(f"Cloudflare cleanup stopped: {exc}")
    else:
        log("The saved zone or API token is missing, so Cloudflare was left unchanged.")
    local_cleanup(bed, vms, keep_saved=cloud_failed)
    if cloud_failed:
        log("Run cleanup-lab.py again to remove the remaining Cloudflare items.")
        return
    log("Cleanup finished. Run setup-lab.py to start again.")


if __name__ == "__main__":
    try:
        main()
    except CleanupError as exc:
        die(str(exc))
