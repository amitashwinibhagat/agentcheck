# Deploying AgentCheck on a GCP e2-micro (Always Free)

One small VM, two containers, real TLS. No managed platform, no trial clock.

**What you get free, per Google's docs:** 1 non-preemptible `e2-micro`
(1 GB RAM), 30 GB standard persistent disk, and **1 GB/month of outbound
data transfer**, in `us-west1`, `us-central1` or `us-east1`.

**On that 1 GB of egress** — measured against this app, not guessed:

| | |
|---|---|
| A UI page load (html + css + js) | ~75 KB |
| One full 467-attack red-team run | ~144 KB |
| `/v1/runs` | ~3.7 KB |
| `/v1/trust` | ~0.3 KB |

So ~1 GB is roughly **11,000 page loads or 7,000 red-team runs** a month. Fine
for a demo, but it is a real ceiling: if the URL gets passed around, watch it.
Serve a private install and it will never come close.

---

## 0. Check which account and region you are on

Always Free is **US-only** (`us-west1`, `us-central1`, `us-east1`) and the
allowance is **per billing account**. A default region elsewhere — Asia, Europe
— means every command below is billed, silently.

```bash
gcloud auth list                 # which accounts are authenticated
gcloud config list               # ACTIVE account, project and region
gcloud billing accounts list     # the billing account that owns the free tier
```

Then work in a **separate configuration** so an unrelated production setup
(project, region) is left untouched:

```bash
gcloud config configurations create agentcheck
gcloud config configurations activate agentcheck
gcloud config set account  <the account that can see the billing account>
gcloud config set compute/zone us-central1-a        # US: free-tier eligible
gcloud projects create agentcheck-demo-$(date +%s) --name="AgentCheck demo"
gcloud config set project "$(gcloud projects list --filter='name:AgentCheck demo' \
  --format='value(projectId)' | head -1)"
gcloud billing projects link "$(gcloud config get-value project)" \
  --billing-account=<BILLING_ACCOUNT_ID>
gcloud config list               # zone MUST be a US region, or stop
```

A dedicated project is worth it: the free e2-micro allowance is consumed per
billing account, and mixing this into a production project makes both harder
to reason about.

## 1. Enable the Compute API

A brand-new project has it off, and the first compute command fails with
`SERVICE_DISABLED`:

```bash
gcloud services enable compute.googleapis.com
```

## 2. Reserve a static IP first

Do this **before** the VM so it can be attached at creation. Ephemeral
addresses change when a VM stops and starts, which would break DNS and your
certificate. A static IP is free **while attached to a running VM** (Google
bills unattached ones, so don't reserve and forget).

```bash
gcloud compute addresses create agentcheck-ip --region=us-central1
gcloud compute addresses describe agentcheck-ip --region=us-central1 \
  --format='get(address)'
```

## 3. Create the VM

```bash
IP=$(gcloud compute addresses describe agentcheck-ip --region=us-central1 \
  --format='get(address)')

gcloud compute instances create agentcheck \
  --machine-type=e2-micro \
  --zone=us-central1-a \
  --image-family=debian-12 \
  --image-project=debian-cloud \
  --boot-disk-size=30GB \
  --boot-disk-type=pd-standard \
  --tags=agentcheck \
  --address="$IP"
```

Two flags are load-bearing for the free tier:

- **`--boot-disk-type=pd-standard`.** The default is `pd-balanced`, which is
  **not** covered — the free allowance is 30 GB-months of *standard* disk. Omit
  this and you pay, with a VM that otherwise looks identical.
- **`--address`** attaches the static IP you just reserved.

`e2-micro` is x86_64, so the image needs no cross-build.

Confirm before moving on:

```bash
gcloud compute disks describe agentcheck --zone=us-central1-a \
  --format='value(type)'                      # .../diskTypes/pd-standard
gcloud compute instances list --filter="machineType:e2-micro"   # exactly one
```

## 4. Open the firewall

```bash
gcloud compute firewall-rules create agentcheck-web \
  --allow=tcp:80,tcp:443 --target-tags=agentcheck \
  --description="AgentCheck HTTP/HTTPS"
```

Port 7373 stays closed — the app is not published on a host port. Only Caddy
is reachable, so nothing is ever served over plaintext.

## 5. Point a hostname at it

Use a domain you control (an `A` record to the static IP), or for a demo use
`sslip.io`, which resolves `<ip-with-dashes>.sslip.io` back to that IP:

```
34-12-34-56.sslip.io   →   34.12.34.56
```

Caddy will get a real Let's Encrypt certificate for either. Use a domain you
control for anything a customer sees.

## 6. Install Docker and add swap

```bash
gcloud compute ssh agentcheck --zone=us-central1-a

curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker
```

**Add swap before building.** The image build runs `pip install` and 1 GB of
RAM is tight; without swap the build can be OOM-killed and the failure looks
like a random network error.

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h
```

## 7. Get the code and configure

If the repo has no git remote, ship a tarball of just what the image needs:

```bash
# From the project root, on your machine
tar czf /tmp/agentcheck-deploy.tar.gz --exclude='__pycache__' --exclude='*.pyc' \
  agentcheck Dockerfile pyproject.toml README.md deploy/gcp
gcloud compute scp /tmp/agentcheck-deploy.tar.gz agentcheck:/tmp/ --zone=us-central1-a
gcloud compute ssh agentcheck --zone=us-central1-a \
  --command="mkdir -p ~/agentcheck && tar xzf /tmp/agentcheck-deploy.tar.gz -C ~/agentcheck"

# Then on the VM
cd ~/agentcheck/deploy/gcp && cp .env.example .env
```

```bash
git clone https://github.com/<you>/agentcheck.git && cd agentcheck/deploy/gcp
cp .env.example .env
```

Edit `.env`:

| variable | value |
|---|---|
| `SITE_ADDRESS` | your hostname (or the `sslip.io` name) |
| `ACME_EMAIL` | where Let's Encrypt sends expiry notices |
| `TYPESAFE_API_KEY` | your judge key — omit for the offline stub |
| `AGENTCHECK_DEMO` | `1` for a public prospect demo, `0` for private |

Sign-in and Postgres are optional; both are documented in the file itself.

## 8. Start it

```bash
sudo docker compose up -d --build
sudo docker compose ps
sudo docker compose logs -f caddy     # watch the certificate get issued
```

Use `sudo` unless you have re-logged in since `usermod -aG docker` — group
membership only applies to new sessions. First build takes a few minutes on an
e2-micro.

First boot takes a minute or two: build, then Caddy's ACME challenge.

## 9. Verify

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://$SITE_ADDRESS/
curl -sS https://$SITE_ADDRESS/v1/auth/me           # {"authenticated": false}
curl -sS https://$SITE_ADDRESS/v1/redteam/families  # 467 attacks
```

Then load the UI and check **Runs** shows content.

---

## Keeping data

`agentcheck-data` is a named volume on the boot disk. It survives
`docker compose restart`, `up --build`, and reboots. It does **not** survive
deleting the VM — so back up the one file that matters:

```bash
# On the VM. SQLite is safe to copy while the app runs only if you use .backup.
docker compose exec agentcheck python -c "
import sqlite3, os
p = os.environ['AGENTCHECK_HOME'] + '/agentcheck.db'
sqlite3.connect(p).backup(sqlite3.connect('/data/backup.db'))
print('wrote /data/backup.db')"

docker compose cp agentcheck:/data/backup.db ./agentcheck-$(date +%F).db
```

For a real deployment set `AGENTCHECK_DB_URL` to Postgres (Neon's free tier
works) — then the data lives outside the VM entirely.

## Updating

```bash
cd ~/agentcheck && git pull && cd deploy/gcp && docker compose up -d --build
```

## When something looks wrong

| symptom | likely cause |
|---|---|
| Caddy loops on certificates | DNS not pointing at the static IP yet, or port 80 blocked |
| Build dies mid-`pip install` | no swap (step 5) |
| 502 from Caddy | app container unhealthy: `docker compose logs agentcheck` |
| Live view doesn't update | a proxy buffering SSE; `flush_interval -1` handles it here |
| Data gone after a rebuild | something reset the volume; check `docker volume ls` |

## TLS renewal

Automatic and unattended. Caddy renews ~30 days before expiry, storing
certificates in the `caddy-data` volume. Nothing to schedule.
