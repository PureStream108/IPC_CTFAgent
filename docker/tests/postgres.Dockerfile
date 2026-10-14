# Run pytest-postgresql against its own temporary server as a non-root user.
FROM ipc-ctfagent:latest
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pytest pytest-cov pytest-postgresql httpx
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /workspace
USER postgres
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--basetemp=/tmp/ipc-native-pytest"]
