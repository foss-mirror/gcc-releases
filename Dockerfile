# SPDX-License-Identifier: GPL-3.0-or-later
ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS base
ARG ALMALINUX_VERSION
RUN . /etc/os-release && test "$ID" = almalinux && test "$VERSION_ID" = "$ALMALINUX_VERSION"
COPY keys/RPM-GPG-KEY-EPEL-8 /recipe/keys/RPM-GPG-KEY-EPEL-8
COPY sdk-packages.json /recipe/sdk-packages.json
COPY scripts/install-sdk.sh /recipe/scripts/install-sdk.sh
COPY scripts/download-rpm.sh /recipe/scripts/download-rpm.sh

FROM base AS builder
RUN bash /recipe/scripts/install-sdk.sh builder
WORKDIR /work
ENTRYPOINT ["bash", "/recipe/scripts/build-toolchain.sh"]

# No distribution compiler or C++ headers. The verifier checks that compiler
# programs and Binutils resolve inside the relocated package. SDK installation
# uses pinned, signature-verified RPMs.
FROM base AS verifier
RUN bash /recipe/scripts/install-sdk.sh verifier
ENTRYPOINT ["python3.11", "/recipe/scripts/verify.py"]
