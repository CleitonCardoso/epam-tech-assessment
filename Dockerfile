# syntax=docker/dockerfile:1

# One production image for BOTH processes. `api` runs the default CMD; the `worker`
# service overrides `command:` with `python -m app.worker` (see compose.yaml).
#
# Deliberately single-stage and slim: no compilers, no test tooling, no source mounts.
# The requirements layer is installed before `COPY app` so editing application code does
# not reinstall dependencies.
FROM python:3.12-slim

# No .pyc files in the image, unbuffered logs (docker logs stays useful), no pip cache.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Non-root runtime user. uid 10001 is the first uid Docker assigns to host users on
# Linux; using it avoids overlapping with host accounts.
RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid appuser --create-home --shell /usr/sbin/nologin appuser

WORKDIR /app
RUN chown appuser:appuser /app

# Dependencies first: this layer is cached until requirements*.txt changes.
COPY requirements.txt requirements.lock.txt requirements-dev.txt requirements-dev.lock.txt ./

# The production image (api/worker) must not contain pytest or other dev/test tooling;
# only the `test` Compose profile passes INSTALL_DEV=1 to get requirements-dev.txt.
ARG INSTALL_DEV=0
# The .lock files are generated from the exact images we tested (`pip freeze`), so the build is
# reproducible; requirements*.txt stay the human-edited intent. Regenerate the locks whenever the
# intent changes: docker run --rm --entrypoint pip <image> freeze | grep '==' | sort
RUN pip install --no-cache-dir -r requirements.lock.txt \
    && if [ "$INSTALL_DEV" = "1" ]; then pip install --no-cache-dir -r requirements-dev.lock.txt; fi

# Application code last, so a code edit does not invalidate the dependency layer.
COPY --chown=appuser:appuser app ./app

USER appuser

EXPOSE 8000

# The api service uses this. The worker overrides it in compose.yaml.
# --proxy-headers makes X-Forwarded-* trusted behind the nginx frontend.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
