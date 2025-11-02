<!-- .github/copilot-instructions.md - tailored guidance for AI coding agents -->

Purpose
-------
- Short, actionable guidance to work productively in this repository (an Odoo addon + local dev stack).

Big picture (what this repo is)
--------------------------------
- This project is an Odoo 18 addon (package name `dankbit`) mounted under `my_addons/dankbit` and packaged to run inside a Dockerized Odoo service (see `Dockerfile` and `docker-compose.yml`).
- The addon exposes website HTTP endpoints (image/chart generation) from `my_addons/dankbit/controllers/main.py` (class `ChartController`) and stores trade data in `dankbit.trade` (`my_addons/dankbit/models/trade.py`).

Key components & conventions
---------------------------
- Addon manifest: `my_addons/dankbit/__manifest__.py` — lists `data/` xml files, cron jobs, security and views. Use this when adding new data files or scheduled actions.
- Controllers: `my_addons/dankbit/controllers/*.py` — HTTP endpoints use Odoo `@http.route` and `request` to access models. Example patterns:
  - Use `request.env['dankbit.trade'].sudo()` to query trades.
  - Image endpoints build Matplotlib figures, gzip-compress the PNG and return via `request.make_response(..., headers=...)` (see `/ <instrument>/calls`, `/puts`, `/strike/<int:strike>`).
  - Template rendering uses `request.render('dankbit.<template_id>')` (see `help_page`).
- Models: `my_addons/dankbit/models/*.py` — model names use the `dankbit.` prefix (e.g. `dankbit.trade`, `dankbit.screenshot`). Look up computed fields (`@api.depends`) and class methods such as `get_index_price()` which call external APIs.
- Views & data: `my_addons/dankbit/views/` and `my_addons/dankbit/data/` — xml files control menus, scheduled actions (`ir_cron.xml`) and access rules. Add new views or scheduled jobs here and list them in `__manifest__.py`.

Developer workflows (how to run/debug)
------------------------------------
- Local dev uses Docker Compose. To start the stack and make the addon available, rebuild and start the `web` service so the `my_addons` mount is active:

```bash
docker-compose up --build
```

- `config/odoo.conf` is mounted into the container (`/etc/odoo/odoo.conf`). Use the Odoo UI (Apps -> Update) to install or upgrade the `dankbit` addon, or run inside the container:

```bash
# inside container
odoo-bin -c /etc/odoo/odoo.conf -u dankbit
```

- When changing system packages (e.g., adding apt dependencies like `python3-matplotlib`), update `Dockerfile` then rebuild the image (`docker-compose up --build`).

Patterns and repo-specific idioms
--------------------------------
- Configuration keys: the addon uses `ir.config_parameter` keys prefixed with `dankbit.` (e.g. `dankbit.from_price`, `dankbit.steps`, `dankbit.refresh_interval`). Read/write via `request.env['ir.config_parameter'].sudo()`.
- Caching: controller code uses a simple module-level cache `_INDEX_CACHE = {'timestamp':0,'price':None}` with `_CACHE_TTL` to avoid frequent external API calls. Honor this pattern if adding expensive lookups.
- External APIs: trades and index data are pulled from Deribit endpoints (`get_index_price`, `get_last_trades_by_instrument_and_time`) inside `models/trade.py`. Network calls are synchronous; consider timeouts when adding functionality.
- Screenshots: endpoint may create `dankbit.screenshot` records—images are stored base64-encoded (see `image_png` field) and may be created via `request.env['dankbit.screenshot'].sudo().create({...})`.

Where to look when making changes
---------------------------------
- HTTP endpoints / plotting: `my_addons/dankbit/controllers/main.py` (ChartController) — update routes and figure generation here.
- Business logic and data ingestion: `my_addons/dankbit/models/trade.py` — scheduled fetchers and helpers like `get_index_price` and `_create_new_trade` live here.
- Views / scheduled actions: `my_addons/dankbit/data/ir_cron.xml` and `my_addons/dankbit/views/*.xml` — add new cron jobs or menus and include them in `__manifest__.py`.

Quick examples (copyable patterns)
---------------------------------
- Add a public chart route:

```py
@http.route('/<string:instrument>/myview', type='http', auth='public', website=True)
def my_view(self, instrument):
    trades = request.env['dankbit.trade'].sudo().search([('name','ilike', instrument)])
    # build figure, compress and return same as other endpoints
```

- Read config params:

```py
icp = request.env['ir.config_parameter'].sudo()
steps = int(icp.get_param('dankbit.steps', default=100))
```

Integration & external dependencies
----------------------------------
- Docker image: `FROM odoo:18.0` (see `Dockerfile`). Additional apt packages (matplotlib, numpy) are installed there.
- Python deps in `requirements.txt`: `numpy`, `matplotlib`, `plotly` — the project expects plotting libs available in the runtime image.
- External services: Deribit public API (`deribit.com`) is used for market data.

Notes for AI agents
-------------------
- Prefer small, focused edits. When changing behavior that touches DB models or cron jobs, update `__manifest__.py` and `data/` xml as needed.
- Avoid changing the public HTTP routes' signatures unless the corresponding frontend templates or stored screenshot callers are updated.
- When adding network calls, add sensible timeouts and respect the existing caching pattern (`_INDEX_CACHE`/`_CACHE_TTL`).

If anything here is unclear or you want more examples (e.g., writing a new scheduled action, or adding tests), tell me which area to expand.
