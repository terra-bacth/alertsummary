"""FastAPI application entrypoint: wires ingest -> correlation -> orchestrator."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator

from tsagent import __version__
from tsagent.api.routes import router
from tsagent.config import get_settings
from tsagent.correlation import CorrelationEngine
from tsagent.demo import DemoDriver
from tsagent.events import EventBus, EventCollector
from tsagent.graph import GraphDeps, Orchestrator
from tsagent.llm import LLMGateway
from tsagent.logging_setup import configure_logging
from tsagent.store import Repository
from tsagent.tools.ssh import AllowList

_settings = get_settings()
# When started via `python -m tsagent` logging is already configured; this covers a bare
# `uvicorn tsagent.main:app` (local dev) so the level/format settings still apply.
if not logging.getLogger().handlers:
    configure_logging(_settings.log_level, _settings.log_format)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app_config = settings.load_app_config()
    inventory = settings.load_inventory()
    logger.info(
        "config: dir=%s db=%s checkpoints=%s ui=%s",
        settings.config_dir, settings.db_path, settings.checkpoint_path, settings.ui_dir or "off",
    )

    repo = Repository(settings.db_path)
    await repo.connect()

    events_bus = EventBus(queue_size=app_config.events.subscriber_queue_size)
    event_collector = EventCollector(
        events_bus, repo=repo, maxlen=app_config.events.buffer_size
    )
    await event_collector.start()

    deps = GraphDeps(
        gateway=LLMGateway(settings.config_dir / "llm.yaml"),
        inventory=inventory,
        app_config=app_config,
        allowlist=AllowList.load(settings.config_dir / "ssh_allowlist.yaml"),
        repo=repo,
        config_dir=settings.config_dir,
        events=event_collector,
    )
    orchestrator = Orchestrator(deps, checkpoint_path=settings.checkpoint_path)
    await orchestrator.start()
    if app_config.orchestrator.resume_on_start:
        resumed = await orchestrator.resume_unfinished()
        if resumed:
            logger.info("resumed %d incident(s) left unfinished by the previous run", len(resumed))

    correlator = CorrelationEngine(
        config=app_config.correlation,
        inventory=inventory,
        repo=repo,
        on_incident=orchestrator.handle_incident,
        events=event_collector,
    )
    await correlator.start()

    demo = None
    if app_config.demo.enabled:
        demo = DemoDriver(
            repo, event_collector, inventory, step_pause=app_config.demo.step_pause_seconds
        )

    app.state.settings = settings
    app.state.app_config = app_config
    app.state.inventory = inventory
    app.state.repo = repo
    app.state.deps = deps
    app.state.orchestrator = orchestrator
    app.state.correlator = correlator
    app.state.demo = demo
    app.state.events = event_collector
    app.state.events_bus = events_bus
    logger.info(
        "tsagent ready: %d cluster(s), correlation window %ds, demo %s",
        len(inventory.clusters), app_config.correlation.window_seconds,
        "on" if demo else "off",
    )

    yield

    await correlator.stop()
    await orchestrator.stop(grace_seconds=app_config.orchestrator.shutdown_grace_seconds)
    events_bus.close()
    await repo.close()


app = FastAPI(
    title="AlertSummarizationAgent",
    version=__version__,
    lifespan=lifespan,
    root_path=_settings.root_path,
)
app.include_router(router)

if _settings.root_path:
    _prefix = "/" + _settings.root_path.strip("/")

    @app.middleware("http")
    async def _prefix_trailing_slash(request: Request, call_next):
        # "/tsagent" -> "/tsagent/": the dashboard's relative asset and API URLs resolve against
        # the directory, so the bare prefix would load a page with broken links.
        if request.url.path == _prefix:
            return RedirectResponse(f"{_prefix}/", status_code=308)
        return await call_next(request)

# The API is read-mostly and role/SSO controls belong in front of the Route, so CORS defaults to
# open. When the dashboard is served by this process (TSAGENT_UI_DIR) it is same-origin anyway;
# narrow TSAGENT_CORS_ORIGINS if you host the UI elsewhere.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Self-metrics on /metrics for the ServiceMonitor. Registered before the UI mount below so the
# catch-all static route can never shadow it.
if _settings.metrics_enabled:
    Instrumentator(excluded_handlers=["/healthz", "/readyz", "/metrics"]).instrument(app).expose(
        app, include_in_schema=False
    )

def mount_ui(target: FastAPI, ui_dir: str) -> bool:
    """Serve the built dashboard at ``/``, same-origin, so it needs no CORS and no second Route.

    Must be called after every API route is registered: routes are matched in order, so the API
    wins and anything else falls through to the SPA.
    """
    if not ui_dir:
        return False
    directory = Path(ui_dir)
    if not (directory / "index.html").is_file():
        logger.warning("TSAGENT_UI_DIR=%s has no index.html; dashboard not served", directory)
        return False
    target.mount("/", StaticFiles(directory=directory, html=True), name="ui")
    logger.info("serving dashboard from %s", directory)
    return True


mount_ui(app, _settings.ui_dir)
