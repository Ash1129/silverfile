FROM python:3.11-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SILVERFILE_DATA_DIR=/data SILVERFILE_SEED_DEMO=1 SILVERFILE_REQUIRE_KEY=1 FORWARDED_ALLOW_IPS=*
COPY requirements.txt pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir -e .
COPY data ./data
COPY demo ./demo
COPY evals ./evals
COPY run.py README.md ./
RUN python data/generate_fixtures.py
VOLUME ["/data"]
EXPOSE 8000
CMD ["python", "run.py", "serve", "--host", "0.0.0.0", "--port", "8000"]
