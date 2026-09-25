# tsagent — Mission Control dashboard

A cinematic, real-time UI for the tsagent multi-agent alarm troubleshooting system.

![stack](https://img.shields.io/badge/stack-React%2018%20·%20Vite%205%20·%20TypeScript-4cd0f0)

The dashboard subscribes to the **Server-Sent Events** feed the backend emits
(`GET /api/stream`) and paints the whole investigation live, step by step:

```
ingest → correlate → plan → promql → reflect → decide → node (SSH) → reflect → report
```

Every event the pipeline emits (queries executed/rejected, reflections demanding a retry,
allow-list refusals, SSH commands, the final RCA) appears in the timeline with a status chip,
and the animated pipeline strip shows which stage is currently active.

## Quick start

```bash
cd web
npm install
npm run dev            # http://localhost:5173, proxies /api → http://localhost:8080
```

By default the dev server proxies `/api` to a local tsagent backend on `:8080`. To point at a
remote instance (e.g. your OpenShift Route) instead:

```bash
TSAGENT_BACKEND=https://your-route.apps.example.com npm run dev
# or, for a production build:
VITE_TSAGENT_API=https://your-route.apps.example.com npm run build
```

## Triggering a livedata demo

The header button **▶ IGNITE DEMO** posts to `POST /api/demo?background=true`, which replays a
complete, scripted investigation through the *real* event system and data store — no LLM, no
Prometheus, no SSH required. The same endpoint is what the real pipeline emits into, so the
dashboard cannot tell the difference between the demo and a genuine incident.

To drive real traffic, point AlertManager at the API:

```yaml
receivers:
  - name: tsagent
    webhook_configs:
      - url: http://tsagent.tsagent.svc:8080/alerts
        send_resolved: true
```

## Pointing at your OpenShift deployment

The container image builds this dashboard and the API pod serves it at `/` on the same Route
(`TSAGENT_UI_DIR`), so a deployed tsagent needs no separate UI hosting. Open
`https://<route-host>/`.

To develop the UI against a deployed backend instead:

1. Rebuild and redeploy the tsagent pod — the event layer ships inside it, no new Service is
   needed. The existing Route already exposes the API.
2. Run the dashboard against that Route:

   ```bash
   TSAGENT_BACKEND=https://tsagent.apps.example.com npm run dev
   # or build once: VITE_TSAGENT_API=https://tsagent.apps.example.com npm run build
   ```

3. If the connection is idle for long stretches (HAProxy keeps a default server timeout), extend
   the Route for long-lived SSE:

   ```yaml
   metadata:
     annotations:
       haproxy.router.openshift.io/timeout: 60m
   ```

   The event stream is resilient to disconnects regardless — the dashboard reconnects
   automatically and resumes from the last `seq` it saw.


## API surface the UI uses

| Endpoint | Purpose |
|---|---|
| `GET /api/stream` | Live event feed (Server-Sent Events) |
| `GET /api/events?after=<seq>` | Event history / reconnect resume |
| `GET /api/overview` | Incidents + alarms + counts + pending groups |
| `GET /incidents/{id}` | Incident detail with alarms + evidence |
| `GET /incidents/{id}/report` | RCA report (JSON), `?format=md` for Markdown |
| `POST /api/demo` | Scripted demo run |

## Build for production

```bash
npm run build        # emits static files into web/dist
npm run preview      # serves the production bundle
```

`dist/` is gitignored. Serve it from any static host (or a Route) pointed at the tsagent API.
