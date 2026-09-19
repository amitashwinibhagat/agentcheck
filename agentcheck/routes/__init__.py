"""HTTP route modules, one per domain.

Each module exposes `register(app, ...)` with exactly the shared state its
endpoints need — the same shape as the older `mount_web(app, store,
authorize)`, which was the first instance of this pattern. `create_app`
stays an assembly function: build shared objects, register domains, return.

`shared.py` holds the cross-domain helpers with `store` passed explicitly
instead of closed over. Domain modules import it; nothing here imports
`proxy`, so the dependency runs one way.

The submodules are imported here so `routes.<domain>` always resolves after
`from agentcheck import routes` — this list is the index of every HTTP
domain in the app.
"""

from agentcheck.routes import checksets
from agentcheck.routes import checks
from agentcheck.routes import monitor
from agentcheck.routes import policies
