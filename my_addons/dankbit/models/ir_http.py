# -*- coding: utf-8 -*-

import logging

from werkzeug.routing import BaseConverter

from odoo import models
from odoo.http import request

_logger = logging.getLogger(__name__)


class DankbitSymbolConverter(BaseConverter):
    """Matches only a Dankbit asset/instrument path segment — one
    starting with "BTC" or "ETH" (case-sensitive, same convention every
    ``asset.startswith("BTC")``/``startswith("ETH")`` check in
    controllers/main.py already uses), e.g. "BTC", "BTC-25JUL26",
    "ETH-29NOV24-98000-P".

    Without this, this addon's own bare top-level routes
    (``/<instrument>``, ``/<instrument>/zones``, ``/<asset>/weekly``,
    etc. — see controllers/main.py) used the plain ``string`` converter,
    which matches ANY single path segment. That silently swallowed
    website's own built-in pages living at a bare path — e.g.
    ``/contactus`` is not a hardcoded werkzeug route but a
    ``website.page`` record rendered through ir.http's generic
    page-serving fallback, which only ever runs when no controller
    matched the path at all. Since ``/<string:instrument>`` DID match
    "/contactus" (as instrument="contactus"), Dankbit's own chart
    controller always won and the fallback never got a chance to render
    the real Contact Us page.
    """
    regex = r"(?:BTC|ETH)[^/]*"


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    @classmethod
    def _get_converters(cls):
        converters = super()._get_converters()
        converters["dankbit_symbol"] = DankbitSymbolConverter
        return converters

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
