# Base image pinned by digest; bump it deliberately (ADR 0010).
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159
WORKDIR /app
# Every dependency pinned by hash, and nothing else installed (--no-deps). The app runs from
# source, so no build step fetches anything unverified.
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes --no-deps -r requirements.lock
COPY src ./src
COPY policies ./policies
ENV PYTHONPATH=/app/src
EXPOSE 8000
CMD ["uvicorn", "sluice.api.app:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8000"]
