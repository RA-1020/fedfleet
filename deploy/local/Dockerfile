# One image for SuperLink and SuperNodes, with the app's dependencies baked in (task 3.6).
# Built for linux/amd64 (Azure) and linux/arm64 (the Oracle aggregator) by
# .github/workflows/image.yml and published to ghcr.io/ra-1020/fedfleet.
#
# Hardened: a fixed base tag, no build cache or bytecode in the image, and the processes run as
# an unprivileged user (uid 10001), not root. The image holds no keys, certificates or data: those
# are mounted at run time (a Kubernetes Secret, or a volume) and .dockerignore keeps them out of
# the build context. Next step: pin the base by digest (Renovate, task 15.6, does this).
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FLWR_DISABLE_RUNTIME_DEPENDENCY_INSTALLATION=1

RUN useradd --uid 10001 --user-group --create-home --home-dir /home/fedfleet \
            --shell /usr/sbin/nologin fedfleet

WORKDIR /app
# CPU-only PyTorch first (the largest layer, cached separately from the app's code). Not pinned
# yet: the version must exist on the CPU index for both amd64 and arm64; the build log shows the
# one chosen, and Renovate (task 15.6) will pin it. Flower is pinned exactly below.
# hadolint ignore=DL3013
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu
COPY pyproject.toml ./
COPY fedfleet ./fedfleet
RUN pip install . "flwr[dp]==1.38.0"

# Flower keeps its state under $HOME/.flwr and FedFleet writes runs to $HOME/fedfleet-runs.
USER 10001:10001
ENV HOME=/home/fedfleet
WORKDIR /home/fedfleet
