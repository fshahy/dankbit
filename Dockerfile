# Pinned to a specific digest (not the floating "18.0" tag) so a routine
# rebuild can't silently pull in a newer/regressed Odoo nightly. Bump
# deliberately after testing: `docker pull odoo:18.0` then
# `docker inspect --format='{{index .RepoDigests 0}}' odoo:18.0`.
FROM odoo@sha256:c01e5bc381f087a3be2800d65cff8ad51ab0709dc54c3b81cd9b0d6c9b3a4d77

USER root

RUN apt update
RUN apt install -y python3-matplotlib python3-numpy

USER odoo
