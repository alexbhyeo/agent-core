# Firecracker Playground: start and stop

Commands for running the Playground (frontend + backend) next to FcSessionManager.
Run them from the repository root on a Linux host with Docker and KVM; on Windows use
the WSL2 distro whose Docker sees `/dev/kvm`:

```bash
wsl -d Ubuntu                      # Windows only
cd /path/to/agent-core             # e.g. /mnt/d/git_projects/agent-core
```

## One-time setup

### Settings file

Create `~/fc-playground.env` (keep it out of the repository) and restrict it to your user:

```bash
cat > ~/fc-playground.env <<'EOF'
# FcSessionManager service token (any long random string; the backend uses it to call the manager)
FCM_API_TOKEN=change-me
# Access token the browser must enter in the Playground settings dialog
PLAYGROUND_TOKEN=change-me-too
# OpenAI-compatible LLM the agents use
API_KEY=your-llm-api-key
API_BASE=https://api.deepseek.com
MODEL_NAME=deepseek-v4-flash
MODEL_PROVIDER=OpenAI
EOF
chmod 600 ~/fc-playground.env
```

Generate random tokens with `python3 -c 'import secrets; print(secrets.token_urlsafe(24))'`.

### VM images (first run on a machine only)

```bash
deploy/firecracker/scripts/build.sh
```

## Start

```bash
set -a; . ~/fc-playground.env; set +a

# 1. Firecracker manager (the backend needs it); skip if it is already running
FCM_DEFAULT_MEM_MIB=1024 FCM_WARM_POOL_SIZE=2 \
  deploy/firecracker/scripts/run_manager.sh

# 2. Backend API on port 8800 (next to the manager)
PLAYGROUND_ALLOWED_ORIGINS="http://localhost:8080,http://127.0.0.1:8080" \
PLAYGROUND_DOCKER=1 \
  deploy/firecracker/playground/backend/run_backend.sh

# 3. Frontend on port 8080 (static site)
PLAYGROUND_API_BASE_URL=http://localhost:8800 \
  deploy/firecracker/playground/frontend/run_frontend.sh
```

Open **http://localhost:8080**, enter the backend URL (`http://localhost:8800`) and
`PLAYGROUND_TOKEN` in the settings dialog, then pick a user in the bottom-left menu.

- The start scripts rebuild the image and recreate the container, so use them after code
  changes. The first backend build takes several minutes; later builds are cached.
- `PLAYGROUND_DOCKER=1` enables the "Restart FcSessionManager" button in the operations view.
- Per-user tiers (e.g. the `noisy` / `fenced` test users): add `FCM_TIERS=...` and
  `FCM_USER_TIERS=...` in front of `run_manager.sh`; see the Tests section of
  [../README.md](../README.md).
- Frontend on another machine: set `PLAYGROUND_API_BASE_URL` to the backend URL as the browser
  reaches it, and add the frontend's origin to `PLAYGROUND_ALLOWED_ORIGINS` on the backend.

## Stop

```bash
docker stop fc-playground-frontend fc-playground-backend   # Playground only
docker stop -t 300 fc-manager                              # also the manager
```

Stopping the manager first saves running VMs to disk, which can take up to a few minutes
(hence `-t 300`); sessions wake on the next request after a restart.

## Start again without rebuilding

```bash
docker start fc-manager                                    # if it was stopped
docker start fc-playground-backend fc-playground-frontend
```

The containers keep their settings. Chat history and the Playground database are on the
`fc-playground-data` volume; VM sessions are on the `fc-sessions` volume.

## Status and logs

```bash
docker ps --filter name=fc-                     # which services are up
curl -s http://localhost:8800/healthz           # backend health
docker logs -f fc-playground-backend            # backend log (Ctrl+C to leave)
docker logs -f fc-manager                       # manager log
```

## Remove

```bash
docker rm -f fc-playground-frontend fc-playground-backend
docker volume rm fc-playground-data             # also deletes chat history
```

Stop the manager first (`docker stop -t 300 fc-manager`) before removing it with
`docker rm -f fc-manager`: `rm -f` kills it without the graceful shutdown, so running VMs
would not be saved.
