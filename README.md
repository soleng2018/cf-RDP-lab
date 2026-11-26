# cf-RDP-lab
One script that brings up Traefik, Authentik, a Cloudflare Tunnel, and one Windows or macOS VM for each USB network dongle. A Windows VM gets a browser RDP address. A macOS VM gets a console at `https://<hostname>.<domain>`.

## Files
These files are the lab. `setup-lab.py` creates `traefik/`, `authentik/`, and `cloudflared/` on the server when it runs. Those folders, `.setup-lab-state.json`, and `.setup-lab-credentials` hold this server's tokens and passwords and are not part of the published copy.

| Path | What it is |
| --- | --- |
| `setup-lab` | The command to run |
| `setup-lab.py` | The setup program |
| `cleanup-lab.py` | Removes the lab this setup created |
| `lab_scope.py` | Shared names used by setup and cleanup |
| `test_lab_scope.py` | Checks that one server does not take another server's Cloudflare objects |
| `README.md` | This file |

## Prerequisites
* Ubuntu server with Docker running and KVM available (`/dev/kvm`)
* Ports 80 and 443 free
* The domain already added as a zone on the Cloudflare account
* One USB network dongle plugged in for each VM
* The server's internet connection left on a different interface. The script does not change the interface that has the default route
* `git`, so the script can download [docker-clients](https://github.com/soleng2018/docker-clients) when `~/docker-clients` is not already on the server

### Install git
If Docker is already installed, skip the Docker step.
```
sudo apt-get update
sudo apt-get install -y git
```
Confirm Docker and KVM:
```
docker run hello-world
ls /dev/kvm
```
`/dev/kvm` must exist. If it does not, enable Intel VT-x or AMD SVM in the BIOS.

### Cloudflare API token
Create one custom token at https://dash.cloudflare.com/profile/api-tokens

Account permissions:
* Cloudflare Tunnel — Edit
* Cloudflare One Networks — Edit
* Zero Trust — Edit
* Access: Apps and Policies — Edit
* Access: Organizations, Identity Providers, and Groups — Edit

Zone permission, limited to the lab domain:
* DNS — Edit

This is an API token. It is not the tunnel connector token, and it is not the Global API Key.

## Run the script
From this folder:
```
./setup-lab
```
It asks for one value at a time:

1. `Domain (eg abc.com)`
2. `Email (eg you@abc.com)`
3. `Cloudflare API token (hidden)`
4. `VM hostname (eg win1)`
5. `OS (eg windows or macos)`
6. `USB MAC address (eg 48:22:54:30:61:7a)`
7. `Add another VM? (yes or no)`

Before the prompts, the script looks for `~/docker-clients`. When that folder is missing, it downloads https://github.com/soleng2018/docker-clients.git into the home directory. An existing `~/docker-clients` folder is left as it is. If `parameters.txt` or `reconcile.sh` is missing from an existing folder, or the download fails, the script stops before it asks for the domain or creates the lab.

When `docker-clients` is already cloned in another folder, point the command at that folder. Replace the path below with the real one:

```
DOCKER_CLIENTS_DIR=/path/to/docker-clients ./setup-lab
```

If that path is missing, the script stops. It does not download a second copy. It also stops before it creates containers or DNS records when the token is missing or the zone is missing. If no VM is entered and none is already listed, it stops before creating a VM. A MAC that is not visible is rejected, and the script stops before it creates the lab.

## What the script does
1. Downloads `docker-clients` into `~/docker-clients` when that folder is missing. Then it asks for the domain, email, and API token, a VM hostname such as `win1`, the OS (`windows` or `macos`), and the USB dongle MAC. If a plugged-in dongle has no network driver, the script starts that driver, reads the MAC address, and shows it. Type that address to confirm it. The uplink MAC is rejected. After the MAC address it asks whether to add another VM. `no` continues the setup. `yes` asks for the next hostname, OS, and MAC.
2. Creates the `traefik` Docker network (`172.18.0.0/16`) when it does not exist.
3. Starts Traefik, with certificates from a Let's Encrypt DNS challenge.
4. Starts Authentik. The admin user is `akadmin` and the password is `Nile123$`. The admin email is the address entered at the prompt.
5. Creates a Cloudflare Tunnel, starts `cloudflared`, and publishes `authentik.<domain>` and `traefik.<domain>`.
6. Creates the Authentik OIDC application `Cloudflare_OIDC` and a Cloudflare identity provider named for this domain. A different domain keeps its own provider. The callback URL is taken from that account's Zero Trust team name.
7. Creates an Allow policy named for this domain and this email address.
8. Writes each VM you entered into `docker-clients/parameters.txt`. The public name is `<hostname>.<domain>`, for example `win1.<domain>` or `mac1.<domain>`. The server hostname is not part of that name. Windows 11 uses `nile` / `nile1234`. macOS uses Sonoma. RAM is 8 GB when the host can spare it, otherwise 4 GB. A saved Windows installer in `docker-clients/cache` is placed into each new Windows VM before it starts.
9. Publishes the console name, protects it with Authentik, creates the Gateway rule, and runs `reconcile.sh` to start the VM.
10. Enables the Access App Launcher. For each Windows VM it waits until RDP answers, then adds the private route, the Zero Trust target, and the browser desktop at `<hostname>.<domain>`. A macOS VM is started for the console at `https://<hostname>.<domain>` and does not get a browser RDP application. A new install can take several minutes.

One domain runs one lab. A second server uses its own domain and its own hostname. Servers on the same Cloudflare account receive different VM addresses, so their private routes do not overlap. `docker-clients` is read from the home directory unless `DOCKER_CLIENTS_DIR` is set.

## After it finishes
The script prints the address for each VM. It looks like this:

```
Desktop URL for <hostname>/win1:
https://win1.<domain>/rdp/<virtual-network-id>/<vm-ip>/3389
This URL opens after Windows finishes downloading and installing.
To watch that progress from a computer that can reach this server, open http://<server-address>:<console-port>/

macOS console for <hostname>/mac1:
https://mac1.<domain>
The install screen is http://<server-address>:<console-port>/
```

The desktop URL and the console URL are written to `.setup-lab-credentials` in this folder. The install screen address is printed only.

* Authentik: `https://authentik.<domain>` — user `akadmin`, password `Nile123$`
* App Launcher: `https://<team>.cloudflareaccess.com`
* Browser RDP: `https://win1.<domain>/rdp/<virtual-network-id>/<vm-ip>/3389`
* Windows: user `nile`, password `nile1234`

Open the Windows RDP address and sign in with the email address. Authentik asks for the `akadmin` password. Windows then asks for `nile` / `nile1234`. The App Launcher is still available at `https://<team>.cloudflareaccess.com`.

The VM files are created next to the `docker-clients` checkout, one folder per server name, for example `~/<hostname>/win1`. That folder name is not used in the public URL.

## Run the script again
Running the script again does not delete tunnels, containers, DNS records, or VMs. It checks the existing machine and fixes what it can.

If the desktop URL stops opening, or a VM loses its USB dongle, run `./setup-lab` again. The script checks that the dongle is attached, that the desktop name resolves through Cloudflare, and that Authentik can still sign the login. A dongle that was unplugged and plugged back in is attached at its new USB address. A dongle that is the uplink is left alone. If the dongle is missing, the script stops that VM from being rebuilt and tells you to plug it in and run the command again.

## Start again
`cleanup-lab.py` removes only this server's lab. It deletes `authentik.<domain>`, `traefik.<domain>`, and the VM names recorded for this server. Every other name in the zone is left as it is. The tunnel it deletes is named after this server. The identity provider and Allow policy it deletes are the ones for this domain. Another domain on the same Cloudflare account is left in place. The Gateway allow rule is removed only when this account has no other tunnel. The App Launcher is removed only when no other Access application remains.

```
python3 cleanup-lab.py
```

It reads the zone and API token saved by `setup-lab.py`. You do not pass them again. It reads `docker-clients` from your home directory, including when the command is started with sudo. A Windows installer already on disk is copied to `docker-clients/cache` and reused for every Windows VM on the next setup. The installed Windows disk is still deleted.

Then run `./setup-lab` again and answer the prompts.
