# SilverFile dashboard. Hardened: unprivileged user, no secrets in the image (they come from the
# environment at run time), fail closed without a data key, code owned by root and not writable.
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SILVERFILE_ENV=production SILVERFILE_DATA_DIR=/data SILVERFILE_SEED_DEMO=1

RUN groupadd --system --gid 10001 silverfile \
 && useradd --system --uid 10001 --gid silverfile --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin silverfile

WORKDIR /app
COPY requirements.txt pyproject.toml ./
COPY src ./src
RUN pip install -r requirements.txt && pip install -e .
COPY data ./data
COPY demo ./demo
COPY evals ./evals
COPY run.py README.md ./
RUN python data/generate_fixtures.py \
 && mkdir -p /data && chown silverfile:silverfile /data && chmod 700 /data

USER silverfile
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]
CMD ["python", "run.py", "serve", "--host", "0.0.0.0", "--port", "8000"]
