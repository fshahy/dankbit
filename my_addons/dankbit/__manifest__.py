# -*- coding: utf-8 -*-
{
    "name": "Dankbit",
    "version": "0.1",
    "category": "Options Greeks",
    "author": "Farid Shahy <fshahy@gmail.com>",
    "summary": "",
    "description": "",
    "depends": ["website"],
    "data": [
        "data/ir_rule.xml",
        "data/ir_cron.xml",
        "data/ir_action.xml",
        "security/ir.model.access.csv",
        "views/res_config_settings_views.xml",
        "views/trade_views.xml",
        "views/screenshot_views.xml",
        "views/help_templates.xml",
        "wizard/plot_wizard_view.xml",
    ],
    "installable": True,
    "application": True,
    "auto_install": False,
}