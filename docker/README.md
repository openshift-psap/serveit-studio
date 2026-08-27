# Building ServeIt Studio Container Images

There are two images:

| Image | Dockerfile | Purpose |
|---|---|---|
| `serveit-studio:workload` | `Dockerfile.workload` | Runs on the benchmark pod (dataset generation, guidellm) |
| `serveit-studio:server` | `Dockerfile.server` / `Dockerfile` | Runs the web/API server |

---

## Prerequisites

- **podman** installed (or docker — commands are identical, just replace `podman` with `docker`)
- Logged in to the registry:
  ```bash
  podman login quay.io
  ```
- For multi-arch builds (arm64 on an x86 host), register QEMU emulation **once**:
  ```bash
  podman run --rm --privileged docker.io/multiarch/qemu-user-static --reset -p yes
  ```

---

## Build the workload image (single arch)

Run this on the build host (e.g. `root@n42-h01-b05-mx750c.rdu3.labs.perfscale.redhat.com`).
The `TMPDIR` redirect keeps large intermediate files off the root filesystem.

```bash
cd /mnt/data/serveit-studio
git pull

# Build for the host architecture (amd64 on an x86 host)
TMPDIR=/mnt/data/tmp podman build \
  -f docker/Dockerfile.workload \
  -t quay.io/bbenshab/serveit-studio:workload \
  .

podman push quay.io/bbenshab/serveit-studio:workload
```

---

## Build a multi-arch image (amd64 + arm64) — full steps

Do this on the x86 build host. The arm64 build runs under QEMU emulation and takes
significantly longer (~1–2 hours) because guidellm/torch must be installed from source.

### 1. Build both arch-specific images

```bash
cd /mnt/data/serveit-studio && git pull

# amd64 (native — fast)
TMPDIR=/mnt/data/tmp podman build \
  --platform linux/amd64 \
  -f docker/Dockerfile.workload \
  -t quay.io/bbenshab/serveit-studio:workload-amd64 \
  .

# arm64 (emulated — slow)
TMPDIR=/mnt/data/tmp podman build \
  --platform linux/arm64 \
  -f docker/Dockerfile.workload \
  -t quay.io/bbenshab/serveit-studio:workload-arm64 \
  .
```

To run the arm64 build in the background and watch its log:

```bash
TMPDIR=/mnt/data/tmp podman build \
  --platform linux/arm64 \
  -f docker/Dockerfile.workload \
  -t quay.io/bbenshab/serveit-studio:workload-arm64 \
  . > /mnt/data/tmp/build-arm64.log 2>&1 &

tail -f /mnt/data/tmp/build-arm64.log
```

### 2. Push both arch images

```bash
TMPDIR=/mnt/data/tmp podman push quay.io/bbenshab/serveit-studio:workload-amd64
TMPDIR=/mnt/data/tmp podman push quay.io/bbenshab/serveit-studio:workload-arm64
```

### 3. Create and push the multi-arch manifest

```bash
# Remove any local image using the :workload name first
podman rmi quay.io/bbenshab/serveit-studio:workload 2>/dev/null || true

# Create a manifest list
podman manifest create quay.io/bbenshab/serveit-studio:workload

# Add both architectures (pulled from the registry)
podman manifest add quay.io/bbenshab/serveit-studio:workload \
  --arch amd64 docker://quay.io/bbenshab/serveit-studio:workload-amd64

podman manifest add quay.io/bbenshab/serveit-studio:workload \
  --arch arm64 docker://quay.io/bbenshab/serveit-studio:workload-arm64

# Verify both entries are present
podman manifest inspect quay.io/bbenshab/serveit-studio:workload

# Push the manifest list — a single :workload tag now serves both arches
TMPDIR=/mnt/data/tmp podman manifest push --all \
  quay.io/bbenshab/serveit-studio:workload \
  docker://quay.io/bbenshab/serveit-studio:workload
```

---

## Verify the image inside a running pod

```bash
kubectl exec -n <namespace> <pod-name> -- bash -c \
  "wc -c /usr/local/bin/generate_dataset /usr/local/bin/generate_turn_dataset"
```

Expected sizes (as of the current build):
- `generate_dataset`: **32077 bytes**
- `generate_turn_dataset`: **19461 bytes**

---

## Notes

- `/mnt/data/tmp` must exist and have enough free space (~20 GB for the arm64 build).
  Create it with `mkdir -p /mnt/data/tmp` if needed.
- The corpus file (`wikitext-103.txt`) is downloaded and baked into the image during
  `STEP 9` of the Dockerfile. This requires internet access from the build host.
- If a build fails partway through, re-running the same `podman build` command reuses
  cached layers up to the failing step.
