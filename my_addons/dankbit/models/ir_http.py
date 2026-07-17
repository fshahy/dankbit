# -*- coding: utf-8 -*-

import logging

from odoo import models
from odoo.http import request

_logger = logging.getLogger(__name__)


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    @classmethod
    def _dispatch(cls, endpoint):
        try:
            return super()._dispatch(endpoint)
        finally:
            cls._dankbit_log_request(endpoint)

    @classmethod
    def _dankbit_log_request(cls, endpoint):
        # Every route this addon exposes lives on ChartController in
        # controllers/main.py — filtering on the endpoint's own module
        # covers all of them (present and future) without needing every
        # other Odoo route (backend RPC calls, other addons' controllers)
        # to be logged too. `endpoint` is a functools.partial wrapping the
        # bound controller method (see _generate_routing_rules), so there
        # is no __self__ to inspect directly, but functools.update_wrapper
        # still copies __module__ onto it.
        module = getattr(endpoint, "__module__", None) or ""
        if not module.startswith("odoo.addons.dankbit.controllers"):
            return
        try:
            request.env["dankbit.http.log"].sudo().create({
                "url": request.httprequest.path,
                "ip_address": request.httprequest.remote_addr,
                "user_agent": request.httprequest.user_agent.string,
            })
        except Exception:
            _logger.exception("dankbit: failed to log http request")
