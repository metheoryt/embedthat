FROM ghcr.io/astral-sh/uv:python3.12-trixie-slim

# ffmpeg for YT streams merge
# NodeJS + vot-cli for YT video translations https://github.com/FOSWLY/vot-cli
RUN apt-get update \
    && apt-get install -y ffmpeg curl \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && apt-get remove -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g vot-cli \
    && npm cache clean --force

WORKDIR /app

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1

# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy

# Place executables in the environment at the front of the path
ENV PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock /app/

# Install the project's dependencies using the lockfile and settings
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Then, add the rest of the project source code and install it
# Installing separately from its dependencies allows optimal layer caching
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Reset the entrypoint, don't invoke `uv`
ENTRYPOINT []

CMD ["python", "main.py"]
