#!/usr/bin/env python3
"""Use cases for running the lab on more than one server."""

import lab_scope


def check(label: str, condition: bool) -> None:
    if not condition:
        raise SystemExit(f"FAIL {label}")
    print(f"ok {label}")


def test_fresh_domain_does_not_take_another_login() -> None:
    providers = [{
        "id": "old",
        "name": "Authentik",
        "type": "oidc",
        "config": {"auth_url": "https://authentik.nileinstaller.com/application/o/authorize/"},
    }]
    check("fresh domain ignores another domain's provider", lab_scope.idp_for_domain(providers, "example.com") is None)
    check("same domain reuses the provider that points at it", lab_scope.idp_for_domain(providers, "nileinstaller.com")["id"] == "old")
    check("named provider is found", lab_scope.idp_for_domain([
        {"id": "new", "name": "Authentik (example.com)", "type": "oidc", "config": {}},
    ], "example.com")["id"] == "new")


def test_cleanup_deletes_only_this_domain() -> None:
    providers = [
        {"id": "old", "name": "Authentik", "type": "oidc", "config": {"auth_url": "https://authentik.nileinstaller.com/application/o/authorize/"}},
        {"id": "other", "name": "Authentik (example.com)", "type": "oidc", "config": {"auth_url": "https://authentik.example.com/application/o/authorize/"}},
    ]
    deleted = lab_scope.idp_ids_to_delete(providers, "example.com")
    check("cleanup of example.com leaves nileinstaller.com", deleted == ["other"])


def test_policy_stays_with_its_domain() -> None:
    policies = [{
        "id": "legacy",
        "name": "Allow lab admin",
        "require": [{"login_method": {"id": "idp-old"}}],
        "include": [{"email": {"email": "owner@example.com"}}],
    }, {
        "id": "new",
        "name": "Allow lab admin (example.com)",
        "require": [{"login_method": {"id": "idp-new"}}],
    }]
    check("new domain does not reuse another domain's allow policy", lab_scope.policy_for_domain(policies, "other.com", "idp-other") is None)
    check("same domain reuses its named policy", lab_scope.policy_for_domain(policies, "example.com", "idp-new")["id"] == "new")
    check("legacy policy is reused only for its own provider", lab_scope.policy_for_domain(policies, "nileinstaller.com", "idp-old")["id"] == "legacy")
    deleted = lab_scope.policy_ids_to_delete(policies, "example.com", {"idp-new"})
    check("cleanup removes only example.com's policy", deleted == ["new"])


def test_dns_and_routes_stay_apart() -> None:
    names = lab_scope.owned_names("example.com", "lab1", [{"hostname": "win1", "published": "win1.example.com"}])
    check("www is not a lab name", "www.example.com" not in names)
    check("the zone apex is not a lab name", "example.com" not in names)
    check("authentik and the VM name are lab names", {"authentik.example.com", "win1.example.com"} <= names)
    check("a published name on another domain is ignored", "win1.other.com" not in lab_scope.owned_names(
        "example.com", "lab1", [{"hostname": "win1", "published": "win1.other.com"}],
    ))
    first = lab_scope.host_octet("alpha")
    second = lab_scope.host_octet("beta")
    check("two servers get different address blocks", first != second)
    used: set[str] = set()
    one = lab_scope.vm_address("alpha", used)
    two = lab_scope.vm_address("beta", set())
    check("VM addresses are inside 172.18.0.0/16", one.startswith("172.18.") and two.startswith("172.18."))
    check("VM addresses differ between servers", one != two)
    foreign = lab_scope.foreign_route(
        [{"network": "172.18.101.11/32", "tunnel_id": "tunnel-a"}],
        "172.18.101.11/32",
        "tunnel-b",
    )
    check("a route on another tunnel is a conflict", foreign is not None)
    check("a route on this tunnel is not a conflict", lab_scope.foreign_route(
        [{"network": "172.18.101.11/32", "tunnel_id": "tunnel-b"}],
        "172.18.101.11/32",
        "tunnel-b",
    ) is None)


def test_shared_account_objects() -> None:
    apps = [
        {"type": "rdp", "domain": "win1.example.com"},
        {"type": "app_launcher", "name": "App Launcher", "domain": "team.cloudflareaccess.com"},
    ]
    names = lab_scope.owned_names("example.com", "lab1", [{"hostname": "win1", "published": ""}])
    check("launcher stays while another server's app remains", lab_scope.should_delete_launcher([
        {"type": "rdp", "domain": "win1.other.com"},
        {"type": "app_launcher", "domain": "team.cloudflareaccess.com"},
    ], names) is False)
    check("launcher can be removed when only this lab's apps remain", lab_scope.should_delete_launcher(apps, names) is True)
    check("gateway rule stays while another tunnel exists", lab_scope.should_delete_gateway([
        {"id": "ours"}, {"id": "theirs"},
    ], "ours") is False)
    check("gateway rule can be removed when this tunnel is the only one", lab_scope.should_delete_gateway([
        {"id": "ours"},
    ], "ours") is True)
    check("an existing launcher needs this domain's provider added", lab_scope.launcher_idp_ids(
        {"allowed_idps": ["idp-old"]},
    ) == ["idp-old"])


if __name__ == "__main__":
    test_fresh_domain_does_not_take_another_login()
    test_cleanup_deletes_only_this_domain()
    test_policy_stays_with_its_domain()
    test_dns_and_routes_stay_apart()
    test_shared_account_objects()
    print("all cases passed")
