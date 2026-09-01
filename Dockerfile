FROM mirror.gcr.io/library/node:24-slim AS web-builder

RUN npm install --global bun@1.3.14

WORKDIR /web

COPY agent-ui/package.json agent-ui/bun.lock ./
RUN bun install --frozen-lockfile

COPY agent-ui ./
RUN bun run build

FROM mirror.gcr.io/library/python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY agent ./agent
COPY client ./client
COPY config ./config
COPY control_plane ./control_plane
COPY context ./context
COPY evals ./evals
COPY hooks ./hooks
COPY prompts ./prompts
COPY safety ./safety
COPY scripts ./scripts
COPY tools ./tools
COPY ui ./ui
COPY utils ./utils
COPY --from=web-builder /web/dist ./agent-ui/dist
COPY main.py ./

RUN pip install --no-cache-dir uv \
    && uv pip install --system .

EXPOSE 8080

# --proxy-headers/--forwarded-allow-ips lets the app trust X-Forwarded-Proto/For
# from the TLS-terminating reverse proxy (see the caddy service / Caddyfile).
CMD ["uvicorn", "control_plane.api:app", "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
