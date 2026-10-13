# One image for SuperLink and SuperNodes, with the app's dependencies baked in.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    FLWR_DISABLE_RUNTIME_DEPENDENCY_INSTALLATION=1
WORKDIR /app
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
COPY pyproject.toml ./
COPY fedfleet ./fedfleet
RUN pip install --no-cache-dir .
