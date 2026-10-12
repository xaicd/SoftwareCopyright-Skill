FROM debian:stable-slim
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null; \
    apt-get update && \
    apt-get install -y --no-install-recommends libreoffice-writer fonts-dejavu-core fontconfig && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /data
ENTRYPOINT ["soffice"]
