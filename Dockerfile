FROM odoo:18.0

USER root

RUN apt update
RUN apt install -y python3-matplotlib python3-numpy

USER odoo
