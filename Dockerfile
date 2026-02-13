# Base Dockerfile for AIRAS ML Experiments
# This provides a reproducible environment for all experiment stages

FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv package manager
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.cargo/bin:${PATH}"

# Set working directory
WORKDIR /workspace

# Copy dependency files
COPY pyproject.toml ./
COPY uv.lock ./uv.lock 2>/dev/null || true

# Install Python dependencies using uv
# This layer will be cached unless dependencies change
RUN uv sync --frozen || uv sync

# Copy the rest of the application
COPY . .

# Create results directory
RUN mkdir -p .research/results

# Default command (can be overridden in workflow)
CMD ["bash"]
