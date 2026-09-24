"""FastAPI application entrypoint: wires ingest -> correlation -> orchestrator."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

from tsagent import __version__
from tsagent.api.routes import router
from tsagent.config import get_settings
from tsagent.correlation import CorrelationEngine
from tsagent.demo import DemoDriver
from tsagent.events import EventBus, EventCollector
from tsagent.graph import GraphDeps, Orchestrator
from tsagent.llm import LLMGateway
from tsagent.store import Repository
from tsagent.tools.ssh import AllowList

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app_config = settings.load_app_config()
    inventory = settings.load_inventory()

    repo = Repository(settings.db_path)
    await repo.connect()

    events_bus = EventBus()
    event_collector = EventCollector(events_bus, repo=repo)
    await event_collector.start()

    deps = GraphDeps(
        gateway=LLMGateway(settings.config_dir / "llm.yaml"),
        inventory=inventory,
        app_config=app_config,
        allowlist=AllowList.load(settings.config_dir / "ssh_allowlist.yaml"),
        repo=repo,
        events=event_collector,
    )
    orchestrator = Orchestrator(deps, checkpoint_path=settings.checkpoint_path)
    await orchestrator.start()
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

    demo = DemoDriver(repo, event_collector, inventory)

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
        "tsagent ready: %d cluster(s), correlation window %ds",
        len(inventory.clusters), app_config.correlation.window_seconds,
    )

    yield

    await correlator.stop()
    await orchestrator.stop()
    events_bus.close()
    await repo.close()


app = FastAPI(title="AlertSummarizationAgent", version=__version__, lifespan=lifespan)
app.include_router(router)

# The dashboard is served cross-origin while it is a standalone app (a browser window run from
# another host). Restrict origins via TSAGENT_CORS_ORIGINS (comma-separated) if you care; the
# default is open because the API is already reachable to anything that can hit the Route, and
# the UI only reads. Role/SSO controls belong in front of the Route, not here.
_origins = [
    o.strip()
    for o in __import__("os").environ.get("TSAGENT_CORS_ORIGINS", "*").split(",")
    if o.strip()
] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Self-metrics on /metrics for the ServiceMonitor in the Helm chart.
Instrumentator(excluded_handlers=["/healthz", "/readyz", "/metrics"]).instrument(app).expose(
    app, include_in_schema=False
)
