FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY gateway ./gateway
RUN uv sync --frozen --no-dev && useradd --create-home bridge && mkdir /data && chown bridge:bridge /data
USER bridge
ENV BRIDGE_DB=/data/jobs.sqlite
EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "modal_bridge.app:from_env", "--factory", "--host", "0.0.0.0", "--port", "8000"]
