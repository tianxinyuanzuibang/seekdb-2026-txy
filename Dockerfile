# MiniVectorDB - evaluation image
#
# The image contains the database source and starts it in the foreground,
# reading SQL from stdin and writing results to stdout.  No port is exposed
# and no network access is required at run time.
#
# Build:
#   docker build -t minivectordb:latest .
#
# Run (the driver talks over the stdin/stdout pipes):
#   docker run --rm -i -v minivectordb-data:/data --cpus=2 --memory=2g \
#       minivectordb:latest

FROM ubuntu:24.04

LABEL org.opencontainers.image.title="MiniVectorDB"
LABEL org.opencontainers.image.description="Lightweight vector database for the 2026 competition round 2"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    MVD_DATA_DIR=/data

# Python 3.12 is in the Ubuntu 24.04 archive.  The database only uses the
# standard library, so no extra packages and no network access are needed at
# run time.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        python3 \
        python3-minimal \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The database is pure Python, so "building" is just installing the sources.
COPY minivectordb/ /app/minivectordb/
COPY README.md /app/README.md
COPY docs/ /app/docs/
COPY tests/ /app/tests/
COPY conversation.json /app/conversation.json
COPY agent/ /app/agent/

RUN mkdir -p /data \
 && chmod 1777 /data

# Start the database in the foreground and keep stdin open.
# --data-dir is accepted for parity with the example in the task statement;
# MVD_DATA_DIR=/data above already sets the same default.
ENTRYPOINT ["python3", "-u", "-m", "minivectordb"]
CMD ["--data-dir", "/data"]
