# Server-side image: runs `lombo-shrink swap` (and `session`) next to the library. The encoding half runs on
# the main PC, not here.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
# Not root by default. The swap only needs to write the library, and copying the owner is optional (it notes and
# carries on when it cannot). Compose sets its own `user:`, which overrides this; plain `docker run` needs `--user`
# set to the owner of your media files if that is not 1000.
RUN useradd --uid 1000 --no-create-home --shell /usr/sbin/nologin app
USER 1000:1000
ENTRYPOINT ["lombo-shrink"]
CMD ["session", "status"]
