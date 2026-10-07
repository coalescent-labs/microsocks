# Builds a microsocks binary for RHEL 8 (glibc 2.28). Thanks to glibc
# backward compatibility the same binary also runs on RHEL 9 and RHEL 10.
#
#   docker build -o out .        (or: podman build -o out .)
#
# the binary ends up in out/microsocks. to target another release:
#
#   docker build --build-arg BASE=registry.access.redhat.com/ubi9/ubi -o out .

ARG BASE=registry.access.redhat.com/ubi8/ubi:8.9

FROM ${BASE} AS build
RUN dnf -y install gcc make && dnf clean all
WORKDIR /src
COPY . .
RUN make clean && make CFLAGS=-O2

FROM scratch
COPY --from=build /src/microsocks /microsocks
