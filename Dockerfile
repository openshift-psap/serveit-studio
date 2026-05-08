FROM registry.access.redhat.com/ubi9/ubi:9.4
ENV LANG="C.UTF-8" LC_ALL="C.UTF-8"
ENV PYTHONUNBUFFERED=1

# Install base dependencies
RUN \
    dnf install -y https://dl.fedoraproject.org/pub/epel/epel-release-latest-$(rpm -E %{rhel}).noarch.rpm && \
    dnf install -y \
        python3.11 python3.11-devel procps git jq wget vim \
        ethtool iputils net-tools qperf ninja-build screen \
    && \
    python3.11 -m ensurepip --upgrade && \
    python3.11 -m pip install --upgrade pip setuptools --no-cache-dir && \
    dnf clean all && \
    rm -rf /var/cache/dnf/* && \
    ln -sf /usr/bin/python3.11 /usr/bin/python3 && \
    ln -sf /usr/bin/python3.11 /usr/bin/python && \
    ln -sf /usr/bin/pip3.11 /usr/bin/pip3 && \
    ln -sf /usr/bin/pip3.11 /usr/bin/pip

# Install Python libraries for Inftune Studio tool
RUN \
    python3.11 -m pip install --no-cache-dir \
        "guidellm>=0.6.0,<0.7" \
        transformers \
        openai \
        httpx \
        pandas \
        matplotlib \
        gevent \
        gevent-websocket \
        plotly \
        Flask \
        Flask-SocketIO \
        requests \
        eventlet \
        Jinja2 \
        PyYAML \
        kubernetes \
        optuna

# Install kubectl
RUN KUBECTL_VERSION="1.30.2" && \
    curl -Lo ./kubectl "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl" && \
    chmod +x ./kubectl && \
    mv ./kubectl /usr/local/bin/kubectl

# Copy the Inftune Studio application to /app
WORKDIR /app
COPY . /app/

# Expose the port the web server will run on
EXPOSE 5000

# Keep container running (allows deploy.sh to start server.py or sleep for testing)
CMD ["sleep", "infinity"]
