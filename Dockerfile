# Slim Debian-based Python image. The geospatial wheels (rasterio, pyogrio,
# pyproj) ship manylinux binaries with GDAL and PROJ bundled, so no system GDAL
# is needed and the image stays small.
FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="crs-normalize-action" \
      org.opencontainers.image.description="Detect and fix mixed or missing CRS in spatial datasets." \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# bash for the entrypoint; git so 'fix' mode can be followed by a commit step.
RUN apt-get update \
    && apt-get install --no-install-recommends -y bash git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/crs-normalize

# Install dependencies first so this layer is cached across source changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && find /usr/local/lib/python3.12 -name '__pycache__' -type d -prune -exec rm -rf {} + \
    && find /usr/local/lib/python3.12 -name 'tests' -type d -prune -exec rm -rf {} +

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# GitHub mounts the workspace here and runs the container with it as the cwd.
WORKDIR /github/workspace

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
