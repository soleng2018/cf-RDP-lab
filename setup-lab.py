#!/usr/bin/env python3
"""Bring up Traefik, Authentik, a Cloudflare Tunnel, and one VM per USB dongle.

Run ./setup-lab from this folder. It asks for the domain, email,
Cloudflare API token, VM hostname, OS, and USB MAC.

Those three settings can also be supplied in the environment as DOMAIN, EMAIL,
and CLOUDFLARE_API_TOKEN. A value supplied that way is not asked again.

The token needs, on that account:
  Cloudflare Tunnel Edit
  Cloudflare One Networks Edit
  Zero Trust Edit
  Access: Apps and Policies Edit
  Access: Organizations, Identity Providers, and Groups Edit
  DNS Edit on the zone

The script leaves the current uplink alone. It does not choose a dongle or an
OS by itself. Nothing is deleted. Running it again checks the existing VMs
and can add another one.
"""

from __future__ import annotations

import fnmatch
import getpass
import json
import os
import pwd
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import lab_scope

LABS = Path(__file__).resolve().parent
STATE_PATH = LABS / ".setup-lab-state.json"
CRED_PATH = LABS / ".setup-lab-credentials"


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
TRAEFIK_SUBNET = "172.18.0.0/16"
LOGIN_PASSWORD = "Nile123$"
WINDOWS_USERNAME = "nile"
WINDOWS_PASSWORD = "nile1234"
AUTHENTIK_IMAGE = "ghcr.io/goauthentik/server:2025.6.4"
TRAEFIK_IMAGE = "traefik:v3.6.1"
CF = "https://api.cloudflare.com/client/v4"

PERMISSIONS = """
Create one custom token at https://dash.cloudflare.com/profile/api-tokens
Account permissions:
  Cloudflare Tunnel — Edit
  Cloudflare One Networks — Edit
  Zero Trust — Edit
  Access: Apps and Policies — Edit
  Access: Organizations, Identity Providers, and Groups — Edit
Zone permission, limited to the domain:
  DNS — Edit
""".strip()


class SetupError(Exception):
    pass


def log(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr, flush=True)
    raise SystemExit(code)


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, text=True, **kwargs)


def capture(cmd: list[str]) -> str:
    return subprocess.check_output(cmd, text=True)


def load_config() -> dict:
    domain = os.environ.get("DOMAIN", "").strip().lower().rstrip(".")
    email = os.environ.get("EMAIL", "").strip()
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    interactive = sys.stdin.isatty()
    if interactive and not (domain and email and token):
        print("Enter the lab settings. The API token stays hidden.")
        print(PERMISSIONS)
        print("")
    while not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", domain):
        if domain:
            print("Enter a domain such as example.com.")
        if not interactive:
            die("DOMAIN is missing. Run ./setup-lab in a terminal.")
        domain = input("Domain (eg abc.com): ").strip().lower().rstrip(".")
    while "@" not in email or email.startswith("@") or email.endswith("@"):
        if email:
            print("Enter an email address such as you@example.com.")
        if not interactive:
            die("EMAIL is missing. Run ./setup-lab in a terminal.")
        email = input("Email (eg you@abc.com): ").strip()
    while not token:
        if not interactive:
            die("CLOUDFLARE_API_TOKEN is missing. Run ./setup-lab in a terminal.")
        token = getpass.getpass("Cloudflare API token (hidden): ").strip()
        if not token:
            print("The API token is required.")
    return {"domain": domain, "email": email, "token": token}


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")
    os.chmod(STATE_PATH, 0o600)


def write_file(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    os.chmod(path, mode)


def cf(token: str, method: str, path: str, body=None, ok_404: bool = False):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(CF + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        if ok_404 and exc.code == 404:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"errors": [{"message": raw[:400]}]}
        errors = payload.get("errors") or []
        messages = payload.get("messages") or []
        parts = []
        for item in errors + messages:
            text = item.get("message", "")
            pointer = (item.get("source") or {}).get("pointer")
            if pointer:
                text = f"{text} [{pointer}]"
            if text:
                parts.append(text)
        detail = "; ".join(parts) or raw[:500]
        raise SetupError(f"Cloudflare {method} {path} failed ({exc.code}): {detail}")
    if isinstance(payload, dict) and payload.get("success") is False:
        errors = payload.get("errors") or []
        detail = "; ".join(e.get("message", "") for e in errors)
        raise SetupError(f"Cloudflare {method} {path} failed: {detail}")
    if isinstance(payload, dict) and "result" in payload:
        return payload["result"]
    return payload


def docker_clients_ready() -> bool:
    return (CLIENTS / "parameters.txt").exists() and (CLIENTS / "reconcile.sh").exists()


def require_docker_clients() -> None:
    if docker_clients_ready():
        return
    custom = os.environ.get("DOCKER_CLIENTS_DIR", "").strip()
    if custom:
        die(
            f"docker-clients is not available at {CLIENTS}.\n"
            "DOCKER_CLIENTS_DIR is set, so a new copy was not downloaded.\n"
            "The script stopped before changing the lab."
        )
    if CLIENTS.exists():
        try:
            occupied = any(CLIENTS.iterdir())
        except OSError:
            occupied = True
        if occupied:
            die(
                f"{CLIENTS} exists but parameters.txt or reconcile.sh is missing.\n"
                "That folder was left in place. The script stopped before changing the lab."
            )
        CLIENTS.rmdir()
    log(f"Downloading docker-clients into {CLIENTS}.")
    try:
        run(["git", "clone", "https://github.com/soleng2018/docker-clients.git", str(CLIENTS)])
    except (OSError, subprocess.CalledProcessError) as exc:
        die(f"docker-clients could not be downloaded ({exc}).\nThe script stopped before changing the lab.")
    if not docker_clients_ready():
        die(
            f"The downloaded docker-clients at {CLIENTS} has no parameters.txt or reconcile.sh.\n"
            "The script stopped before changing the lab."
        )


def require_local() -> None:
    if not Path("/dev/kvm").exists():
        die("KVM is not available (/dev/kvm). Windows VMs cannot start on this host.")
    try:
        subprocess.check_call(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        die("Docker is not available to this user.")
    for port in (80, 443):
        with socket.socket() as sock:
            sock.settimeout(0.5)
            busy = sock.connect_ex(("127.0.0.1", port)) == 0
        if busy and not container_running("traefik"):
            die(f"Port {port} is already in use. Traefik needs ports 80 and 443.")


def container_running(name: str) -> bool:
    out = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", name],
        text=True, capture_output=True,
    )
    return out.returncode == 0 and out.stdout.strip() == "true"


def preflight(cfg: dict) -> tuple[str, str, str]:
    token = cfg["token"]
    try:
        verify = cf(token, "GET", "/user/tokens/verify")
    except SetupError as exc:
        die(str(exc))
    if verify.get("status") != "active":
        die("The Cloudflare API token is not active.")
    zones = cf(token, "GET", "/zones?" + urllib.parse.urlencode({"name": cfg["domain"]}))
    if not zones:
        die(f"Zone {cfg['domain']} is not on this Cloudflare account. Add the zone before running the script.")
    zone = zones[0]
    account = zone["account"]["id"]
    probes = [
        f"/accounts/{account}/cfd_tunnel?is_deleted=false&per_page=1",
        f"/accounts/{account}/access/identity_providers?per_page=1",
        f"/accounts/{account}/access/organizations",
        f"/accounts/{account}/access/apps?per_page=1",
        f"/accounts/{account}/access/policies?per_page=1",
        f"/accounts/{account}/teamnet/routes",
        f"/accounts/{account}/infrastructure/targets",
        f"/accounts/{account}/gateway/rules",
        f"/zones/{zone['id']}/dns_records?per_page=1",
    ]
    failed = []
    for path in probes:
        try:
            cf(token, "GET", path)
        except SetupError as exc:
            failed.append(str(exc))
    if failed:
        die(
            "The API token cannot run this script. No containers or DNS records were changed.\n\n"
            + "\n".join(f"  - {item}" for item in failed)
            + "\n\n"
            + PERMISSIONS
        )
    org = cf(token, "GET", f"/accounts/{account}/access/organizations")
    auth_domain = ""
    if isinstance(org, dict):
        auth_domain = org.get("auth_domain") or ""
    elif isinstance(org, list) and org:
        auth_domain = org[0].get("auth_domain") or ""
    if not auth_domain:
        die("Cloudflare did not return the Zero Trust team domain, so the OIDC callback cannot be set.")
    if not auth_domain.endswith(".cloudflareaccess.com"):
        auth_domain = auth_domain + ".cloudflareaccess.com"
    log(f"Cloudflare account {account}, zone {cfg['domain']}, team {auth_domain}")
    return account, zone["id"], auth_domain


def server_address() -> str:
    for iface in default_uplinks():
        try:
            out = capture(["ip", "-4", "-o", "addr", "show", "dev", iface])
        except subprocess.CalledProcessError:
            continue
        for line in out.splitlines():
            parts = line.split()
            if "inet" not in parts:
                continue
            address = parts[parts.index("inet") + 1].split("/")[0]
            if address and not address.startswith("127."):
                return address
    return ""


def default_uplinks() -> list[str]:
    ifaces = []
    for line in capture(["ip", "-4", "route", "show", "default"]).splitlines():
        parts = line.split()
        if "dev" in parts:
            ifaces.append(parts[parts.index("dev") + 1])
    return ifaces


def usb_node(iface: str) -> Path | None:
    device = Path(f"/sys/class/net/{iface}/device")
    if not device.exists():
        return None
    current = device.resolve()
    while current != current.parent:
        if (current / "busnum").exists() and (current / "devnum").exists():
            return current
        current = current.parent
    return None


def has_global_ipv4(iface: str) -> bool:
    try:
        out = capture(["ip", "-4", "-o", "addr", "show", "dev", iface])
    except subprocess.CalledProcessError:
        return False
    return any("scope global" in line for line in out.splitlines())


def mac_of(iface: str) -> str:
    return Path(f"/sys/class/net/{iface}/address").read_text().strip().lower()


def free_usb_dongles() -> list[dict]:
    uplinks = default_uplinks()
    uplink_macs = {mac_of(iface) for iface in uplinks if Path(f"/sys/class/net/{iface}/address").exists()}
    uplink_nodes = {node for iface in uplinks if (node := usb_node(iface))}
    used = macs_in_parameters()
    found = []
    for iface_path in sorted(Path("/sys/class/net").iterdir()):
        iface = iface_path.name
        if not (iface_path / "address").exists():
            continue
        node = usb_node(iface)
        if node is None:
            continue
        mac = mac_of(iface)
        if mac in uplink_macs or iface in uplinks or node in uplink_nodes:
            log(f"Leaving uplink {iface} ({mac}) untouched.")
            continue
        if has_global_ipv4(iface):
            log(f"Skipping {iface} ({mac}): it has an IPv4 address.")
            continue
        if mac in used:
            log(f"USB dongle {mac} is already assigned to a VM.")
            continue
        found.append({"iface": iface, "mac": mac})
    return found


def macs_in_parameters() -> set[str]:
    found = set()
    if not PARAMS.exists():
        return found
    for line in PARAMS.read_text().splitlines():
        if line.strip().startswith("#") or "=" not in line:
            continue
        for match in re.findall(r"([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})", line):
            found.add(match.lower())
    return found


def testbed_name() -> str:
    return lab_scope.server_name(socket.gethostname())


def memory_for_vm(count: int) -> str:
    mem = 0
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            mem = int(line.split()[1]) // 1024
    leave = 2048
    each = max(0, (mem - leave) // max(count, 1))
    if each >= 8192:
        return "8G"
    if each >= 4096:
        return "4G"
    die(
        f"Not enough free memory for a Windows VM ({mem} MB available). "
        "The uplink and existing containers were not changed."
    )
    return "4G"


def cpu_for_vm() -> str:
    cores = os.cpu_count() or 2
    return str(max(2, min(4, cores - 1)))


def port_free(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def used_ports_and_ips() -> tuple[set[int], set[str]]:
    ports, ips = set(), set()
    if PARAMS.exists():
        for line in PARAMS.read_text().splitlines():
            if line.strip().startswith("#") or "=" not in line or line.split("=", 1)[0].strip().isupper():
                continue
            parts = [p.strip().strip('"') for p in line.split("=", 1)[1].split(",")]
            if len(parts) >= 8:
                for value in parts[6:8]:
                    if value.isdigit():
                        ports.add(int(value))
            for match in re.findall(r"ip=([0-9.]+)", line):
                ips.add(match)
    return ports, ips


def next_ip(used: set[str]) -> str:
    try:
        return lab_scope.vm_address(testbed_name(), used)
    except ValueError as exc:
        die(str(exc))
    return ""


def ensure_network() -> None:
    inspect = subprocess.run(["docker", "network", "inspect", "traefik"], capture_output=True, text=True)
    if inspect.returncode == 0:
        log("Docker network traefik already exists.")
        return
    run(["docker", "network", "create", "--subnet", TRAEFIK_SUBNET, "traefik"])
    log(f"Created Docker network traefik ({TRAEFIK_SUBNET}).")


def ensure_traefik(cfg: dict) -> None:
    directory = LABS / "traefik"
    write_file(directory / "secrets" / "cf_api.secret", cfg["token"] + "\n", 0o600)
    write_file(
        directory / ".env",
        f"TRAEFIK_IMAGE={TRAEFIK_IMAGE}\nDOMAIN_NAME={cfg['domain']}\nEMAIL={cfg['email']}\n",
        0o600,
    )
    write_file(
        directory / "conf" / "authentik.yml",
        """http:
  middlewares:
    authentik:
      forwardAuth:
        address: http://authentik-server:9000/outpost.goauthentik.io/auth/traefik
        trustForwardHeader: true
        authResponseHeaders:
          - X-authentik-username
          - X-authentik-groups
          - X-authentik-email
          - X-authentik-name
          - X-authentik-uid
          - X-authentik-jwt
          - X-authentik-meta-jwks
          - X-authentik-meta-outpost
          - X-authentik-meta-provider
          - X-authentik-meta-app
          - X-authentik-meta-version
""",
    )
    domain = cfg["domain"]
    write_file(
        directory / "docker-compose.yml",
        f"""services:
  traefik:
    container_name: traefik
    image: {TRAEFIK_IMAGE}
    restart: unless-stopped
    ports:
      - 443:443
      - 80:80
    command:
      - --providers.docker=true
      - --providers.docker.exposedbydefault=false
      - --providers.file=true
      - --providers.file.directory=/etc/traefik/conf/
      - --providers.file.watch=true
      - --entrypoints.web.address=:80
      - --entrypoints.web.http.redirections.entryPoint.to=websecure
      - --entrypoints.web.http.redirections.entryPoint.scheme=https
      - --entrypoints.websecure.address=:443
      - --certificatesresolvers.letsencrypt.acme.email={cfg['email']}
      - --certificatesresolvers.letsencrypt.acme.storage=/letsencrypt/acme.json
      - --certificatesresolvers.letsencrypt.acme.dnschallenge=true
      - --certificatesresolvers.letsencrypt.acme.dnschallenge.provider=cloudflare
      - --entrypoints.websecure.http.tls.certResolver=letsencrypt
      - --entrypoints.websecure.http.tls.domains[0].main={domain}
      - --entrypoints.websecure.http.tls.domains[0].sans=*.{domain}
    environment:
      CF_DNS_API_TOKEN_FILE: /run/secrets/cf_api
    secrets:
      - cf_api
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./certs:/letsencrypt
      - ./conf:/etc/traefik/conf/
    networks:
      - traefik
networks:
  traefik:
    external: true
secrets:
  cf_api:
    file: ./secrets/cf_api.secret
""",
    )
    run(["docker", "compose", "-f", str(directory / "docker-compose.yml"), "up", "-d"])
    log("Traefik is up.")


def ensure_authentik(cfg: dict, state: dict) -> None:
    directory = LABS / "authentik"
    secret = state.get("authentik_secret") or secrets.token_urlsafe(36)
    pg = state.get("pg_pass") or secrets.token_urlsafe(18)
    bootstrap = state.get("bootstrap_token") or secrets.token_urlsafe(24)
    state.update({
        "authentik_secret": secret,
        "pg_pass": pg,
        "bootstrap_token": bootstrap,
    })
    save_state(state)
    write_file(
        directory / ".env",
        "\n".join([
            f"AUTHENTIK_SECRET_KEY={secret}",
            f"AUTHENTIK_BOOTSTRAP_PASSWORD={LOGIN_PASSWORD}",
            f"AUTHENTIK_BOOTSTRAP_EMAIL={cfg['email']}",
            f"AUTHENTIK_BOOTSTRAP_TOKEN={bootstrap}",
            f"PG_PASS={pg}",
            "PG_USER=authentik",
            "PG_DB=authentik",
            "AUTHENTIK_ERROR_REPORTING__ENABLED=false",
            "",
        ]),
        0o600,
    )
    write_file(
        directory / "docker-compose.yml",
        f"""services:
  postgresql:
    image: postgres:16-alpine
    container_name: authentik-db
    restart: unless-stopped
    environment:
      POSTGRES_PASSWORD: ${{PG_PASS}}
      POSTGRES_USER: ${{PG_USER}}
      POSTGRES_DB: ${{PG_DB}}
    volumes:
      - database:/var/lib/postgresql/data
    env_file: .env
    networks: [traefik]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -d authentik -U authentik"]
      interval: 10s
      timeout: 5s
      retries: 10
  redis:
    image: redis:alpine
    container_name: authentik-redis
    restart: unless-stopped
    command: --save 60 1 --loglevel warning
    networks: [traefik]
  server:
    image: {AUTHENTIK_IMAGE}
    container_name: authentik-server
    restart: unless-stopped
    command: server
    env_file: .env
    environment:
      AUTHENTIK_REDIS__HOST: redis
      AUTHENTIK_POSTGRESQL__HOST: postgresql
      AUTHENTIK_POSTGRESQL__USER: authentik
      AUTHENTIK_POSTGRESQL__NAME: authentik
      AUTHENTIK_POSTGRESQL__PASSWORD: ${{PG_PASS}}
    depends_on:
      postgresql:
        condition: service_healthy
      redis:
        condition: service_started
    labels:
      - traefik.enable=true
      - traefik.http.routers.authentik.rule=Host(`authentik.{cfg["domain"]}`)
      - traefik.http.routers.authentik.entrypoints=websecure
      - traefik.http.routers.authentik.tls=true
      - traefik.http.routers.authentik.tls.certresolver=letsencrypt
      - traefik.http.services.authentik.loadbalancer.server.port=9000
    networks: [traefik]
  worker:
    image: {AUTHENTIK_IMAGE}
    container_name: authentik-worker
    restart: unless-stopped
    command: worker
    user: root
    env_file: .env
    environment:
      AUTHENTIK_REDIS__HOST: redis
      AUTHENTIK_POSTGRESQL__HOST: postgresql
      AUTHENTIK_POSTGRESQL__USER: authentik
      AUTHENTIK_POSTGRESQL__NAME: authentik
      AUTHENTIK_POSTGRESQL__PASSWORD: ${{PG_PASS}}
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    depends_on:
      postgresql:
        condition: service_healthy
      redis:
        condition: service_started
    networks: [traefik]
networks:
  traefik:
    external: true
volumes:
  database:
""",
    )
    run(["docker", "compose", "-f", str(directory / "docker-compose.yml"), "up", "-d"])
    log("Authentik is starting.")


def authentik_ip() -> str:
    return capture(["docker", "inspect", "-f", "{{.NetworkSettings.Networks.traefik.IPAddress}}", "authentik-server"]).strip()


def wait_authentik(state: dict) -> None:
    deadline = time.time() + 180
    token = state["bootstrap_token"]
    while time.time() < deadline:
        if container_running("authentik-server"):
            try:
                ip = authentik_ip()
                req = urllib.request.Request(f"http://{ip}:9000/api/v3/core/users/me/")
                req.add_header("Authorization", f"Bearer {token}")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status == 200:
                        log("Authentik API is ready.")
                        return
            except (urllib.error.URLError, subprocess.CalledProcessError, SetupError):
                pass
        time.sleep(5)
    die("Authentik did not become ready. Traefik and Authentik were left running so the error can be inspected.")


def ak(state: dict, method: str, path: str, body=None):
    ip = authentik_ip()
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://{ip}:9000/api/v3{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {state['bootstrap_token']}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:500]
        raise SetupError(f"Authentik {method} {path} failed ({exc.code}): {detail}")


def ak_results(payload) -> list:
    if isinstance(payload, dict) and "results" in payload:
        return payload["results"]
    if isinstance(payload, list):
        return payload
    return []


def flow_id(state: dict, slug: str) -> str:
    payload = ak(state, "GET", f"/flows/instances/?slug={slug}")
    results = ak_results(payload)
    if not results:
        raise SetupError(f"Authentik flow {slug} was not found.")
    return results[0]["pk"]


def authentik_signing_key(state: dict) -> str:
    certs = ak_results(ak(state, "GET", "/crypto/certificatekeypairs/?page_size=50"))
    preferred = next((c for c in certs if c.get("name") == "authentik Self-signed Certificate"), None)
    chosen = preferred or (certs[0] if certs else None)
    if not chosen:
        raise SetupError("Authentik has no certificate to sign the Cloudflare login token.")
    return chosen["pk"]


def ensure_oidc(cfg: dict, state: dict, auth_domain: str) -> str:
    token = cfg["token"]
    account = state["account_id"]
    redirect = f"https://{auth_domain}/cdn-cgi/access/callback"
    signing_key = authentik_signing_key(state)
    consent = flow_id(state, "default-provider-authorization-implicit-consent")
    providers = ak_results(ak(state, "GET", "/providers/oauth2/?search=Cloudflare_OIDC"))
    provider = next((p for p in providers if p.get("name") == "Provider for Cloudflare_OIDC"), None)
    if provider is None:
        scopes = ak_results(ak(state, "GET", "/propertymappings/provider/scope/?page_size=100"))
        wanted = [s["pk"] for s in scopes if s.get("scope_name") in {"openid", "email", "profile"}]
        client_id = state.get("oidc_client_id") or secrets.token_hex(20)
        client_secret = state.get("oidc_client_secret") or secrets.token_urlsafe(32)
        state["oidc_client_id"] = client_id
        state["oidc_client_secret"] = client_secret
        save_state(state)
        provider = ak(state, "POST", "/providers/oauth2/", {
            "name": "Provider for Cloudflare_OIDC",
            "authorization_flow": consent,
            "invalidation_flow": flow_id(state, "default-provider-invalidation-flow"),
            "client_type": "confidential",
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uris": [{"matching_mode": "strict", "url": redirect}],
            "property_mappings": wanted,
            "signing_key": signing_key,
            "sub_mode": "hashed_user_id",
            "issuer_mode": "per_provider",
            "include_claims_in_id_token": True,
        })
        log("Created the Authentik OIDC provider.")
    elif provider.get("signing_key") != signing_key or provider.get("authorization_flow") != consent:
        provider = ak(state, "PATCH", f"/providers/oauth2/{provider['pk']}/", {
            "signing_key": signing_key,
            "authorization_flow": consent,
        })
        log("Updated the Authentik OIDC provider to sign login tokens.")

    apps = ak_results(ak(state, "GET", "/core/applications/?search=Cloudflare_OIDC"))
    if not any(a.get("slug") == "cloudflare-idp-oidc" for a in apps):
        ak(state, "POST", "/core/applications/", {
            "name": "Cloudflare_OIDC",
            "slug": "cloudflare-idp-oidc",
            "provider": provider["pk"],
        })
    client_id = state.get("oidc_client_id") or provider.get("client_id")
    client_secret = state.get("oidc_client_secret")
    if not client_secret:
        raise SetupError("The Authentik OIDC client secret is not in the state file, so Cloudflare cannot be updated.")
    domain = cfg["domain"]
    oidc_config = {
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_url": f"https://authentik.{domain}/application/o/authorize/",
        "token_url": f"https://authentik.{domain}/application/o/token/",
        "certs_url": f"https://authentik.{domain}/application/o/cloudflare-idp-oidc/jwks/",
        "scopes": ["openid", "email", "profile"],
        "email_claim_name": "email",
    }
    existing = cf(token, "GET", f"/accounts/{account}/access/identity_providers?per_page=50") or []
    item = lab_scope.idp_for_domain(existing, domain)
    if item:
        current = item.get("config") or {}
        stale = item.get("name") != lab_scope.idp_name(domain) or any(
            current.get(key) != oidc_config[key] for key in ("client_id", "auth_url", "token_url", "certs_url")
        )
        returned_secret = current.get("client_secret") or ""
        if returned_secret and returned_secret != client_secret:
            stale = True
        if stale:
            cf(token, "PUT", f"/accounts/{account}/access/identity_providers/{item['id']}", {
                "name": lab_scope.idp_name(domain),
                "type": "oidc",
                "config": oidc_config,
            })
            log(f"Updated the Cloudflare identity provider for {domain}.")
        else:
            log(f"Cloudflare identity provider for {domain} already exists.")
        state["idp_id"] = item["id"]
        save_state(state)
        return item["id"]
    created = cf(token, "POST", f"/accounts/{account}/access/identity_providers", {
        "name": lab_scope.idp_name(domain),
        "type": "oidc",
        "config": oidc_config,
    })
    state["idp_id"] = created["id"]
    save_state(state)
    log("Created the Cloudflare Authentik identity provider.")
    return created["id"]


def ensure_console_access(state: dict, vms: list[dict]) -> None:
    providers = ak_results(ak(state, "GET", "/providers/proxy/?page_size=100"))
    outposts = ak_results(ak(state, "GET", "/outposts/instances/"))
    embedded = next((item for item in outposts if item.get("type") == "proxy"), None)
    if embedded is None:
        raise SetupError("Authentik has no proxy outpost for the VM console.")
    assigned = list(embedded.get("providers") or [])
    users = ak_results(ak(state, "GET", "/core/users/?search=akadmin"))
    admin = next((user for user in users if user.get("username") == "akadmin"), None)
    consent = flow_id(state, "default-provider-authorization-implicit-consent")
    invalidation = flow_id(state, "default-provider-invalidation-flow")
    changed = False
    for vm in vms:
        host = f"https://{vm['console']}"
        provider = next((item for item in providers if item.get("external_host") == host), None)
        if provider is None:
            provider = ak(state, "POST", "/providers/proxy/", {
                "name": vm["target"],
                "authorization_flow": consent,
                "invalidation_flow": invalidation,
                "mode": "forward_single",
                "external_host": host,
            })
            providers.append(provider)
            log(f"Created the Authentik login for {vm['console']}.")
        apps = ak_results(ak(state, "GET", f"/core/applications/?search={urllib.parse.quote(vm['target'])}"))
        app = next((item for item in apps if item.get("slug") == vm["target"]), None)
        if app is None:
            app = ak(state, "POST", "/core/applications/", {
                "name": vm["target"],
                "slug": vm["target"],
                "provider": provider["pk"],
                "policy_engine_mode": "any",
            })
        if admin is not None:
            bindings = ak_results(ak(state, "GET", f"/policies/bindings/?target={app['pk']}"))
            if not any(item.get("user") == admin["pk"] for item in bindings):
                ak(state, "POST", "/policies/bindings/", {
                    "target": app["pk"],
                    "user": admin["pk"],
                    "order": 0,
                    "enabled": True,
                })
        if provider["pk"] not in assigned:
            assigned.append(provider["pk"])
            changed = True
    if changed:
        ak(state, "PATCH", f"/outposts/instances/{embedded['pk']}/", {"providers": assigned})
        log("Attached the VM consoles to the Authentik outpost.")


def ensure_policy(cfg: dict, state: dict, idp_id: str) -> str:
    token = cfg["token"]
    account = state["account_id"]
    policies = cf(token, "GET", f"/accounts/{account}/access/policies?per_page=50") or []
    item = lab_scope.policy_for_domain(policies, cfg["domain"], idp_id)
    body = {
        "name": lab_scope.policy_name(cfg["domain"]),
        "decision": "allow",
        "include": [{"email": {"email": cfg["email"]}}],
        "require": [{"login_method": {"id": idp_id}}],
    }
    if item:
        if item.get("name") != body["name"] or lab_scope.policy_email(item) != cfg["email"]:
            cf(token, "PUT", f"/accounts/{account}/access/policies/{item['id']}", body)
            log(f"Updated the Allow policy for {cfg['domain']}.")
        else:
            log(f"Allow policy for {cfg['domain']} already exists.")
        state["policy_id"] = item["id"]
        save_state(state)
        return item["id"]
    created = cf(token, "POST", f"/accounts/{account}/access/policies", body)
    state["policy_id"] = created["id"]
    save_state(state)
    log(f"Created the Allow policy for {cfg['email']}.")
    return created["id"]


def ensure_gateway(cfg: dict, state: dict) -> None:
    token = cfg["token"]
    account = state["account_id"]
    rules = cf(token, "GET", f"/accounts/{account}/gateway/rules") or []
    for rule in rules:
        if rule.get("filters") == ["l4"] and "access.target" in (rule.get("traffic") or "") and rule.get("action") == "allow":
            log("Gateway allow rule for infrastructure targets already exists.")
            return
    precedences = [r.get("precedence") or 0 for r in rules if r.get("filters") == ["l4"]]
    precedence = (max(precedences) + 1) if precedences else 1
    cf(token, "POST", f"/accounts/{account}/gateway/rules", {
        "name": "Allow infrastructure targets",
        "description": "Lets browser RDP reach Access infrastructure targets.",
        "enabled": True,
        "action": "allow",
        "filters": ["l4"],
        "traffic": "access.target",
        "precedence": precedence,
    })
    log("Created the Gateway allow rule for infrastructure targets.")


def ensure_tunnel(cfg: dict, state: dict) -> str:
    token = cfg["token"]
    account = state["account_id"]
    name = f"{testbed_name()}-tunnel"
    tunnels = cf(token, "GET", f"/accounts/{account}/cfd_tunnel?is_deleted=false&name={urllib.parse.quote(name)}") or []
    tunnel = next((t for t in tunnels if t.get("name") == name and not t.get("deleted_at")), None)
    if tunnel is None and state.get("tunnel_id"):
        saved = cf(token, "GET", f"/accounts/{account}/cfd_tunnel/{state['tunnel_id']}", ok_404=True)
        if saved and saved.get("name") == name and not saved.get("deleted_at"):
            tunnel = saved
    if tunnel is None:
        tunnel = cf(token, "POST", f"/accounts/{account}/cfd_tunnel", {"name": name, "config_src": "cloudflare"})
        log(f"Created tunnel {name}.")
    else:
        log(f"Reusing tunnel {tunnel.get('name', name)}.")
    state["tunnel_id"] = tunnel["id"]
    save_state(state)
    connector = cf(token, "GET", f"/accounts/{account}/cfd_tunnel/{tunnel['id']}/token")
    if isinstance(connector, dict):
        connector = connector.get("token") or connector.get("result") or ""
    if not isinstance(connector, str) or not connector:
        raise SetupError("Cloudflare did not return a tunnel connector token.")
    directory = LABS / "cloudflared"
    write_file(directory / ".env", f"TUNNEL_TOKEN={connector}\n", 0o600)
    write_file(
        directory / "docker-compose.yml",
        """services:
  tunnel:
    container_name: cloudflared-tunnel
    image: cloudflare/cloudflared:latest
    restart: unless-stopped
    command: tunnel run
    env_file: .env
    networks: [traefik]
networks:
  traefik:
    external: true
""",
    )
    run(["docker", "compose", "-f", str(directory / "docker-compose.yml"), "up", "-d"])
    log("cloudflared is up.")
    return tunnel["id"]


def ensure_dns(cfg: dict, state: dict, names: list[str]) -> None:
    token = cfg["token"]
    zone = state["zone_id"]
    tunnel_id = state["tunnel_id"]
    target = f"{tunnel_id}.cfargotunnel.com"
    existing = cf(token, "GET", f"/zones/{zone}/dns_records?per_page=200") or []
    by_name: dict[str, list] = {}
    for record in existing:
        by_name.setdefault(record.get("name"), []).append(record)
    for name in names:
        fqdn = name if name.endswith(cfg["domain"]) else f"{name}.{cfg['domain']}"
        records = by_name.get(fqdn) or []
        current = next((record for record in records if record.get("type") == "CNAME"), None)
        tunnel_cname = bool(current) and str(current.get("content") or "").endswith(".cfargotunnel.com")
        if current and current.get("content") == target and current.get("proxied") and len(records) == 1:
            continue
        if current and tunnel_cname and len(records) == 1 and (current.get("content") != target or not current.get("proxied")):
            cf(token, "PATCH", f"/zones/{zone}/dns_records/{current['id']}", {"content": target, "proxied": True})
            log(f"Pointed {fqdn} at this tunnel.")
            continue
        if records:
            shown = ", ".join(f"{record.get('type')} {record.get('content')}" for record in records)
            raise SetupError(
                f"{fqdn} already has DNS ({shown}). One domain runs one lab. "
                "Use a domain whose authentik, traefik, and VM names are free."
            )
        cf(token, "POST", f"/zones/{zone}/dns_records", {
            "type": "CNAME",
            "name": fqdn,
            "content": target,
            "proxied": True,
            "ttl": 1,
        })
        log(f"Created proxied CNAME {fqdn}.")


def ensure_ingress(cfg: dict, state: dict, hostnames: list[str], drop: list[str] | None = None) -> None:
    token = cfg["token"]
    account = state["account_id"]
    tunnel_id = state["tunnel_id"]
    current = cf(token, "GET", f"/accounts/{account}/cfd_tunnel/{tunnel_id}/configurations", ok_404=True) or {}
    config = (current or {}).get("config") or {}
    excluded = set(drop or [])
    ingress = [
        rule for rule in config.get("ingress") or []
        if rule.get("service") != "http_status:404" and rule.get("hostname") not in excluded
    ]
    known = {rule.get("hostname") for rule in ingress}
    for hostname in hostnames:
        if hostname not in known:
            ingress.append({
                "hostname": hostname,
                "service": "https://traefik:443",
                "originRequest": {"noTLSVerify": True, "originServerName": cfg["domain"]},
            })
    ingress.append({"service": "http_status:404"})
    cf(token, "PUT", f"/accounts/{account}/cfd_tunnel/{tunnel_id}/configurations", {
        "config": {"ingress": ingress, "warp-routing": {"enabled": True}},
    })
    log("Tunnel ingress updated. Private network routing is enabled.")


def parse_vm_lines(domain: str) -> list[dict]:
    if not PARAMS.exists():
        return []
    bed = testbed_name()
    vms = []
    for line in PARAMS.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw = [part.strip() for part in stripped.split("=", 1)]
        if key.isupper() or not key.startswith(bed + "/"):
            continue
        parts = [part.strip().strip('"') for part in raw.split(",")]
        if len(parts) < 10 or parts[0] not in {"windows", "macos"}:
            continue
        macs = [part.lower() for part in parts[9:] if re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", part.lower())]
        ip = next((part.split("=", 1)[1] for part in parts if part.startswith("ip=")), "")
        vm = key.split("/", 1)[1].replace("/", "-")
        vms.append({
            "key": key,
            "mac": macs[0] if macs else "",
            "ip": ip,
            "web_port": int(parts[6]),
            "remote_port": int(parts[7]),
            "os": parts[0],
            "console": f"{vm}.{domain}",
            "rdp_host": f"{vm}.{domain}",
            "username": parts[4],
            "password": parts[5],
            "target": vm,
        })
    return vms


_usb_alias_rows: list[tuple[str, str]] | None = None
_driver_attach_tried: set[str] = set()


def usb_alias_rows() -> list[tuple[str, str]]:
    global _usb_alias_rows
    if _usb_alias_rows is not None:
        return _usb_alias_rows
    rows = []
    path = Path(f"/lib/modules/{os.uname().release}/modules.alias")
    if path.exists():
        for line in path.read_text(errors="ignore").splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] == "alias" and parts[1].startswith("usb:"):
                rows.append((parts[1], parts[2]))
    _usb_alias_rows = rows
    return rows


def module_for_usb(modalias: str) -> str:
    for pattern, module in usb_alias_rows():
        if fnmatch.fnmatch(modalias, pattern):
            return module
    return ""


def as_root(args: list[str], data: str | None = None) -> bool:
    cmd = args if os.geteuid() == 0 else ["sudo", *args]
    result = subprocess.run(cmd, input=data, text=True, capture_output=True)
    return result.returncode == 0


def uplink_usb_names() -> set[str]:
    names = set()
    for iface in default_uplinks():
        node = usb_node(iface)
        if node is not None:
            names.add(node.name)
    return names


def attach_spare_dongles() -> None:
    root = Path("/sys/bus/usb/devices")
    if not root.exists():
        return
    skip = uplink_usb_names()
    for iface in sorted(root.iterdir()):
        if ":" not in iface.name or iface.name in _driver_attach_tried:
            continue
        parent = iface.name.split(":", 1)[0]
        if parent in skip or (iface / "driver").exists():
            continue
        net = iface / "net"
        if net.exists() and any(net.iterdir()):
            continue
        try:
            klass = (iface / "bInterfaceClass").read_text().strip().lower()
            modalias = (iface / "modalias").read_text().strip()
        except OSError:
            continue
        if klass not in {"ff", "02", "e0"}:
            continue
        module = module_for_usb(modalias)
        if not module:
            continue
        _driver_attach_tried.add(iface.name)
        log(f"Starting the network driver for USB dongle {parent} so its MAC address can be read.")
        if not as_root(["modprobe", module]):
            log(f"The driver for USB dongle {parent} could not be started.")
            continue
        bound = as_root(["tee", "/sys/bus/usb/drivers_probe"], iface.name + "\n")
        if not bound or not (iface / "driver").exists():
            driver = ""
            links = Path(f"/sys/module/{module}/drivers")
            if links.exists():
                for link in links.iterdir():
                    if link.name.startswith("usb:"):
                        driver = link.name.split(":", 1)[1]
                        break
            if driver:
                bound = as_root(["tee", f"/sys/bus/usb/drivers/{driver}/bind"], iface.name + "\n")
        if not bound:
            log(f"The driver for USB dongle {parent} could not be attached.")
            continue
        mac = ""
        for _ in range(8):
            if net.exists():
                for nic in net.iterdir():
                    address = nic / "address"
                    if address.exists():
                        mac = address.read_text().strip().lower()
                        break
            if mac:
                break
            time.sleep(1)
        if mac:
            log(f"USB dongle {parent} has MAC address {mac}.")
        else:
            log(f"USB dongle {parent} still has no MAC address.")


def visible_usb_macs() -> list[str]:
    attach_spare_dongles()
    uplinks = set(default_uplinks())
    uplink_macs = {mac_of(iface) for iface in uplinks if Path(f"/sys/class/net/{iface}/address").exists()}
    found = []
    for iface_path in sorted(Path("/sys/class/net").iterdir()):
        if not (iface_path / "address").exists() or usb_node(iface_path.name) is None:
            continue
        mac = mac_of(iface_path.name)
        if mac in uplink_macs or iface_path.name in uplinks:
            continue
        if mac not in found:
            found.append(mac)
    return found


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def ask_yes_no(prompt: str) -> bool:
    while True:
        answer = ask(prompt).lower()
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Enter yes or no.")


def ask_vms(cfg: dict) -> list[dict]:
    if not sys.stdin.isatty():
        log("No terminal is attached, so no new VM was requested.")
        return []
    used = macs_in_parameters()
    uplinks = set(default_uplinks())
    uplink_macs = {mac_of(iface) for iface in uplinks if Path(f"/sys/class/net/{iface}/address").exists()}
    existing_names = {key.split("/", 1)[1] for key in existing_vm_keys()}
    print("")
    print("Each VM needs a hostname, an OS, and the USB dongle MAC.")
    print("The hostname becomes the public name, for example win1." + cfg["domain"])
    seen = visible_usb_macs()
    if seen:
        print("USB dongles found on this host, other than the uplink. Type the MAC you want to use:")
        for mac in seen:
            print(f"  {mac}")
    else:
        print("No spare USB dongle is visible. Plug one in before entering its MAC.")
    requested = []
    adding = True
    if existing_names:
        adding = ask_yes_no("Add a VM? (yes or no): ")
    while adding:
        while True:
            hostname = ask("VM hostname (eg win1): ").lower()
            if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", hostname):
                print("Use a short hostname such as win1 or mac1, with no dots.")
                continue
            if hostname in existing_names or any(item["hostname"] == hostname for item in requested):
                print(f"{hostname} is already listed.")
                continue
            break
        while True:
            os_name = ask("OS (eg windows or macos): ").lower()
            if os_name in {"windows", "macos"}:
                break
            print("OS must be windows or macos.")
        while True:
            mac = ask("USB MAC address (eg 48:22:54:30:61:7a): ").lower().replace("-", ":")
            if not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
                print("MAC address must look like 48:22:54:30:61:7a.")
                continue
            if mac in uplink_macs:
                print(f"{mac} is the uplink. It was not accepted.")
                continue
            if mac in used or any(item["mac"] == mac for item in requested):
                print(f"{mac} is already assigned to a VM.")
                continue
            if mac not in visible_usb_macs():
                print(f"{mac} is not visible on this server. Plug in that dongle and enter the MAC again.")
                continue
            break
        requested.append({"hostname": hostname, "os": os_name, "mac": mac})
        existing_names.add(hostname)
        used.add(mac)
        adding = ask_yes_no("Add another VM? (yes or no): ")
    return requested


def existing_vm_keys() -> set[str]:
    found = set()
    if not PARAMS.exists():
        return found
    bed = testbed_name()
    for line in PARAMS.read_text().splitlines():
        if line.strip().startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key.startswith(bed + "/"):
            found.add(key)
    return found


def append_vm_lines(cfg: dict, requested: list[dict]) -> None:
    if not PARAMS.exists():
        die(f"docker-clients parameters file is missing: {PARAMS}")
    if not requested:
        return
    bed = testbed_name()
    ram = memory_for_vm(len(requested))
    cpu = cpu_for_vm()
    ports, ips = used_ports_and_ips()
    web = 8101
    remote = 3391
    text = PARAMS.read_text()
    if "TRAEFIK_SUBNET" not in text:
        text += f'\nTRAEFIK_SUBNET = "{TRAEFIK_SUBNET}"\n'
    for item in requested:
        while web in ports or not port_free(web):
            web += 1
        while remote in ports or not port_free(remote):
            remote += 1
        ip = next_ip(ips)
        version = "11" if item["os"] == "windows" else "sonoma"
        console = f"{item['hostname']}.{cfg['domain']}"
        line = (
            f'{bed}/{item["hostname"]} = "{item["os"]}","{version}","{ram}","{cpu}",'
            f'"{WINDOWS_USERNAME}","{WINDOWS_PASSWORD}","{web}","{remote}","{console}",'
            f'"{item["mac"]}","ip={ip}"\n'
        )
        text += line
        ports.update({web, remote})
        log(f"Added {item['os']} VM {item['hostname']} on {item['mac']}.")
        web += 1
        remote += 1
    PARAMS.write_text(text)
    os.chmod(PARAMS, 0o600)


def vm_container(key: str) -> str:
    bed, _, name = key.partition("/")
    return f"{bed}-{name.replace('/', '-')}"


def vm_compose(key: str) -> Path:
    bed, _, name = key.partition("/")
    return CLIENTS.parent / bed / name / "docker-compose.yml"


def iface_for_mac(mac: str) -> str:
    wanted = mac.lower()
    for iface in Path("/sys/class/net").iterdir():
        address = iface / "address"
        try:
            if address.exists() and address.read_text().strip().lower() == wanted:
                return iface.name
        except OSError:
            continue
    return ""


def recorded_usb(path: Path) -> tuple[str, str] | None:
    if not path.exists():
        return None
    match = re.search(r"hostbus=(\d+),hostaddr=(\d+)", path.read_text())
    if not match:
        return None
    return match.group(1), match.group(2)


def usb_address_present(bus: str, addr: str) -> bool:
    root = Path("/sys/bus/usb/devices")
    if not root.exists():
        return False
    for device in root.iterdir():
        try:
            if (device / "busnum").read_text().strip() == bus and (device / "devnum").read_text().strip() == addr:
                return True
        except OSError:
            continue
    return False


def repair_dongles(vms: list[dict]) -> set[str]:
    blocked = set()
    uplinks = set(default_uplinks())
    uplink_macs = {mac_of(iface) for iface in uplinks if Path(f"/sys/class/net/{iface}/address").exists()}
    for vm in vms:
        mac = (vm.get("mac") or "").lower()
        if not mac:
            continue
        if mac in uplink_macs:
            log(f"USB dongle {mac} is the uplink. It was not given to {vm['key']}.")
            blocked.add(vm["key"])
            continue
        iface = iface_for_mac(mac)
        if iface and iface not in uplinks:
            log(f"USB dongle {mac} is on the host ({iface}). {vm['key']} will use the current USB address.")
            continue
        name = vm_container(vm["key"])
        recorded = recorded_usb(vm_compose(vm["key"]))
        if container_running(name) and recorded and usb_address_present(*recorded):
            log(f"USB dongle {mac} is attached to the running VM {name}.")
            continue
        if container_running(name):
            log(f"USB dongle {mac} is no longer attached to {name}. Stopping it so the dongle can be attached again.")
            run(["docker", "stop", name])
            found = False
            for _ in range(15):
                iface = iface_for_mac(mac)
                if iface and iface not in uplinks:
                    found = True
                    break
                time.sleep(2)
            if found:
                log(f"USB dongle {mac} is back on the host. {vm['key']} will start with the new USB address.")
                continue
        log(f"Plug in the USB dongle {mac} for {vm['key']} and run the same command again.")
        blocked.add(vm["key"])
    return blocked


def require_visible_dongles(requested: list[dict], existing: list[dict]) -> None:
    visible = set(visible_usb_macs())
    missing = []
    for item in requested:
        if item["mac"] not in visible:
            missing.append(item["mac"])
    for vm in existing:
        mac = (vm.get("mac") or "").lower()
        if not mac or mac in visible:
            continue
        recorded = recorded_usb(vm_compose(vm["key"]))
        if container_running(vm_container(vm["key"])) and recorded and usb_address_present(*recorded):
            continue
        missing.append(mac)
    if not missing:
        return
    joined = ", ".join(dict.fromkeys(missing))
    die(
        f"USB dongle {joined} is not visible on this server.\n"
        "The script stopped before changing the lab.\n"
        "Plug in the dongle, or attach its driver, and run ./setup-lab again."
    )


def verify_login_key(state: dict) -> None:
    ip = authentik_ip()
    slug = "cloudflare-idp-oidc"
    try:
        with urllib.request.urlopen(f"http://{ip}:9000/application/o/{slug}/jwks/", timeout=20) as resp:
            keys = json.loads(resp.read().decode()).get("keys") or []
    except (urllib.error.URLError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        log(f"Authentik login key could not be read ({exc}). Desktop login may fail.")
        return
    if any(key.get("kty") == "RSA" for key in keys):
        log("Authentik login key is present.")
        return
    log("Authentik is not publishing a login key. Desktop login will fail until the OIDC provider has a signing certificate.")


def stage_windows_installers(vms: list[dict]) -> None:
    cache = CLIENTS / "cache"
    if not cache.is_dir():
        return
    isos = [
        path for path in cache.iterdir()
        if path.is_file() and path.suffix.lower() == ".iso" and path.stat().st_size >= 100 * 1024 * 1024
    ]
    if not isos:
        return
    bed = testbed_name()
    for vm in vms:
        if vm.get("os") != "windows":
            continue
        storage = CLIENTS.parent / bed / vm["target"] / "windows"
        storage.mkdir(parents=True, exist_ok=True)
        for iso in isos:
            dest = storage / iso.name
            if dest.exists() and dest.stat().st_size == iso.stat().st_size:
                log(f"Windows installer {iso.name} is already in {vm['key']}.")
                continue
            log(f"Placing cached Windows installer {iso.name} into {vm['key']}.")
            try:
                if dest.exists():
                    dest.unlink()
                os.link(iso, dest)
            except OSError:
                try:
                    shutil.copy2(iso, dest)
                except OSError as exc:
                    log(f"Could not place {iso.name} into {vm['key']} ({exc}). Windows will download it.")
                    continue
            log(f"Windows installer {iso.name} is ready in {vm['key']}.")


def reconcile() -> None:
    script = CLIENTS / "reconcile.sh"
    if not script.exists():
        die(f"Missing {script}")
    log("Starting the Windows VM with reconcile.sh.")
    run(["bash", str(script)], cwd=str(CLIENTS))


def wait_rdp(port: int, label: str = "Windows RDP") -> bool:
    log(f"Waiting for {label} on 127.0.0.1:{port}. A new install can take several minutes.")
    deadline = time.time() + 45 * 60
    while time.time() < deadline:
        if port_free(port) is False:
            log(f"Port {port} is accepting connections.")
            return True
        time.sleep(20)
    return False


def ensure_app_launcher(cfg: dict, state: dict, idp_id: str, policy_id: str) -> str:
    token = cfg["token"]
    account = state["account_id"]
    apps = cf(token, "GET", f"/accounts/{account}/access/apps?per_page=50") or []
    for app in apps:
        if app.get("type") != "app_launcher" or not app.get("domain"):
            continue
        present = lab_scope.launcher_idp_ids(app)
        if idp_id in present:
            log(f"App Launcher already exists at https://{app['domain']}")
            return app["domain"]
        policies = []
        for policy in app.get("policies") or []:
            if isinstance(policy, dict) and policy.get("id"):
                policies.append({"id": policy["id"], "precedence": policy.get("precedence") or 1})
            elif isinstance(policy, str):
                policies.append({"id": policy, "precedence": 1})
        if not any(policy["id"] == policy_id for policy in policies):
            policies.append({"id": policy_id, "precedence": len(policies) + 1})
        try:
            cf(token, "PUT", f"/accounts/{account}/access/apps/{app['id']}", {
                "name": app.get("name") or "App Launcher",
                "type": "app_launcher",
                "domain": app["domain"],
                "session_duration": app.get("session_duration") or "24h",
                "allowed_idps": present + [idp_id],
                "auto_redirect_to_identity": True,
                "policies": policies or [{"id": policy_id, "precedence": 1}],
            })
            log(f"Added this domain's login to the App Launcher at https://{app['domain']}")
        except SetupError as exc:
            log(f"The existing App Launcher was left unchanged: {exc}")
        return app["domain"]
    created = cf(token, "POST", f"/accounts/{account}/access/apps", {
        "name": "App Launcher",
        "type": "app_launcher",
        "session_duration": "24h",
        "allowed_idps": [idp_id],
        "auto_redirect_to_identity": True,
        "policies": [{"id": policy_id, "precedence": 1}],
    })
    domain = created.get("domain") or ""
    log(f"Enabled the App Launcher at https://{domain}")
    return domain


def rdp_browser_url(vm: dict, target: dict) -> str:
    ipv4 = (target.get("ip") or {}).get("ipv4") or {}
    vnet = ipv4.get("virtual_network_id")
    address = ipv4.get("ip_addr") or vm.get("ip")
    if not vnet or not address:
        return f"https://{vm['rdp_host']}"
    return f"https://{vm['rdp_host']}/rdp/{vnet}/{address}/3389"


def ensure_rdp(cfg: dict, state: dict, vm: dict, idp_id: str, policy_id: str) -> str:
    token = cfg["token"]
    account = state["account_id"]
    ensure_dns(cfg, state, [vm["rdp_host"]])
    routes = cf(token, "GET", f"/accounts/{account}/teamnet/routes") or []
    network = f"{vm['ip']}/32"
    conflict = lab_scope.foreign_route(routes, network, state["tunnel_id"])
    if conflict:
        raise SetupError(
            f"Private route {network} is already published by another tunnel. "
            "This server's VM address is already in use on the Cloudflare account."
        )
    if not any(r.get("network") == network and r.get("tunnel_id") == state["tunnel_id"] for r in routes):
        cf(token, "POST", f"/accounts/{account}/teamnet/routes", {
            "network": network,
            "tunnel_id": state["tunnel_id"],
            "comment": vm["target"],
        })
        log(f"Added private route {network}.")
    targets = cf(token, "GET", f"/accounts/{account}/infrastructure/targets") or []

    def target_address(item: dict) -> str:
        return ((item.get("ip") or {}).get("ipv4") or {}).get("ip_addr") or ""

    cloud_target = f"{testbed_name()}-{vm['target']}"
    target = next((t for t in targets if (t.get("hostname") or "").lower() == cloud_target), None)
    if target is None:
        target = next((t for t in targets if target_address(t) == vm["ip"]), None)
    if target is None:
        target = cf(token, "POST", f"/accounts/{account}/infrastructure/targets", {
            "hostname": cloud_target,
            "ip": {"ipv4": {"ip_addr": vm["ip"]}},
        })
        log(f"Added target {cloud_target} at {vm['ip']}.")
    criteria_name = (target or {}).get("hostname") or cloud_target
    browser_url = rdp_browser_url(vm, target)
    apps = cf(token, "GET", f"/accounts/{account}/access/apps?per_page=50") or []

    def app_matches(app: dict) -> bool:
        if app.get("domain") == vm["rdp_host"]:
            return True
        return any(dest.get("uri") == vm["rdp_host"] for dest in app.get("destinations") or [])

    if any(app_matches(app) for app in apps):
        log(f"RDP application {vm['rdp_host']} already exists.")
        return browser_url
    cf(token, "POST", f"/accounts/{account}/access/apps", {
        "name": vm["target"],
        "type": "rdp",
        "domain": vm["rdp_host"],
        "session_duration": "24h",
        "allow_authenticate_via_warp": False,
        "target_criteria": [{
            "port": 3389,
            "protocol": "RDP",
            "target_attributes": {"hostname": [criteria_name]},
        }],
        "allowed_idps": [idp_id],
        "auto_redirect_to_identity": True,
        "app_launcher_visible": True,
        "policies": [{"id": policy_id, "precedence": 1}],
    })
    log(f"Created browser RDP application {browser_url}")
    return browser_url


def write_credentials(cfg: dict, state: dict, vms: list[dict], launcher: str, rdp_urls: dict[str, str]) -> None:
    lines = [
        f"domain={cfg['domain']}",
        f"authentik=https://authentik.{cfg['domain']}",
        f"authentik_user=akadmin",
        f"authentik_email={cfg['email']}",
        f"app_launcher=https://{launcher}" if launcher else "app_launcher=",
        "",
    ]
    for vm in vms:
        lines += [
            f"vm={vm['key']}",
            f"console=https://{vm['console']}",
            f"desktop={rdp_urls.get(vm['key'], '')}",
            f"rdp={rdp_urls.get(vm['key'], '')}",
            f"windows_user={vm['username']}",
            f"usb_mac={vm['mac']}",
            "",
        ]
    write_file(CRED_PATH, "\n".join(lines), 0o600)


def main() -> None:
    require_docker_clients()
    cfg = load_config()
    require_local()
    account, zone, auth_domain = preflight(cfg)
    requested = ask_vms(cfg)
    existing = parse_vm_lines(cfg["domain"])
    if not requested and not existing:
        die("No VM was entered, and none is already listed. Enter a hostname, windows or macos, and a USB MAC.")
    require_visible_dongles(requested, existing)
    state = load_state()
    state["account_id"] = account
    state["zone_id"] = zone
    state["domain"] = cfg["domain"]
    save_state(state)

    ensure_network()
    ensure_traefik(cfg)
    ensure_authentik(cfg, state)
    wait_authentik(state)
    tunnel_id = ensure_tunnel(cfg, state)
    state["tunnel_id"] = tunnel_id
    bed = testbed_name()
    base_names = [f"authentik.{cfg['domain']}", f"traefik.{cfg['domain']}"]
    ensure_dns(cfg, state, base_names)
    append_vm_lines(cfg, requested)
    vms = parse_vm_lines(cfg["domain"])
    console_names = [vm["console"] for vm in vms]
    ensure_dns(cfg, state, console_names)
    ensure_ingress(
        cfg,
        state,
        base_names + [vm["console"] for vm in vms if vm.get("os") == "macos"],
        [vm["rdp_host"] for vm in vms if vm.get("os") != "macos"],
    )
    ensure_console_access(state, vms)
    idp_id = ensure_oidc(cfg, state, auth_domain)
    verify_login_key(state)
    policy_id = ensure_policy(cfg, state, idp_id)
    try:
        ensure_gateway(cfg, state)
    except SetupError as exc:
        log(f"Gateway rule was not created ({exc}). RDP setup will continue.")
    blocked = repair_dongles(vms)
    if blocked:
        missing = ", ".join(vm["mac"] for vm in vms if vm["key"] in blocked)
        die(
            f"USB dongle {missing} is not visible.\n"
            "The script stopped. Windows was not started, and no desktop URL was created.\n"
            "Services already running were left in place. Attach the dongle and run ./setup-lab again,\n"
            "or run python3 cleanup-lab.py to remove this lab."
        )
    stage_windows_installers(vms)
    reconcile()
    launcher = ensure_app_launcher(cfg, state, idp_id, policy_id)
    rdp_urls: dict[str, str] = {}
    waiting: set[str] = set()
    for vm in vms:
        if vm["key"] in blocked:
            continue
        if vm.get("os") == "macos":
            if not wait_rdp(vm["remote_port"], "macOS VNC"):
                waiting.add(vm["key"])
            continue
        if not wait_rdp(vm["remote_port"]):
            waiting.add(vm["key"])
            continue
        rdp_urls[vm["key"]] = ensure_rdp(cfg, state, vm, idp_id, policy_id)
    write_credentials(cfg, state, vms, launcher, rdp_urls)
    log("")
    log("Finished.")
    for vm in vms:
        desktop = rdp_urls.get(vm["key"])
        if desktop:
            log(f"Desktop URL for {vm['key']}:")
            log(desktop)
            if vm.get("os") == "windows":
                log("This URL opens after Windows finishes downloading and installing.")
                progress = f"http://{server_address()}:{vm['web_port']}/" if server_address() else f"port {vm['web_port']} on this server"
                log(f"To watch that progress from a computer that can reach this server, open {progress}")
            continue
        if vm.get("os") == "macos":
            log(f"macOS console for {vm['key']}:")
            log(f"https://{vm['console']}")
            progress = f"http://{server_address()}:{vm['web_port']}/" if server_address() else f"port {vm['web_port']} on this server"
            log(f"The install screen is {progress}")
            continue
        log(f"No desktop URL for {vm['key']}.")
        if vm["key"] in blocked:
            log(f"USB dongle {vm['mac']} is not visible, so the Windows VM was not started.")
            log("The App Launcher has no VM to show until that VM is running.")
            log("Attach the dongle, run ./setup-lab again, and answer no when asked to add a VM.")
        elif vm["key"] in waiting:
            log("Windows is still installing. Run ./setup-lab again after it finishes.")
        else:
            log("The desktop was not created.")
    if rdp_urls:
        log("")
        log(f"Sign in with {cfg['email']}. Authentik asks for the akadmin password.")
        log(f"Windows user is {WINDOWS_USERNAME}.")
        log(f"The desktop URL is also saved in {CRED_PATH}")


if __name__ == "__main__":
    try:
        main()
    except SetupError as exc:
        die(str(exc))
