# FinSight Frontend

Next.js 16 (App Router) + React 19 + CopilotKit 1.59 + AG-UI client. Five-page investment research dashboard.

## Quick Start

```bash
cd web/nextjs-app
npm install
npm run dev
```

Open `http://localhost:3000`. Requires the backend orchestrator on port 8001.

## Pages

| Route | Description |
|---|---|
| `/` | Overview landing — architecture diagram, feature grid, CTAs |
| `/research` | **Primary page** — CopilotKit chat, agent activity tiles, BUY/HOLD/SELL badges, HTML/PDF downloads |
| `/dashboard` | Observability dashboard — KPIs, agent metrics, latency charts, RAGAS quality scores |
| `/memory` | Persistent briefs browser — search by ticker, expandable cards, report downloads |
| `/operator` | Service health dashboard — LED status for all 7 backend services |

## Architecture

```
Browser → /api/copilotkit (Next.js) → /a2a-agui (Orchestrator, port 8001)
                                        ↓ ADK Runner
                                   Agent Pool (RAG, Quant, Market Context)
                                        ↓
                                   MCP Server (port 8010)
```

CopilotKit connects via `HttpAgent` pointing at the orchestrator's AG-UI streaming endpoint. The Next.js layer is a pure pass-through — all LLM inference runs on the backend.

## Components

| File | Purpose |
|---|---|
| `components/Providers.tsx` | CopilotKit provider + app shell (Sidebar + main content) |
| `components/Sidebar.tsx` | Left nav — workspace links, recent queries |

## API Routes

| Route | Purpose |
|---|---|
| `POST /api/copilotkit` | CopilotKit runtime → orchestrator AG-UI bridge |
| `GET /api/dashboard` | Dashboard metrics — KPIs, agent breakdown, time series (`?hours=24`) |
| `GET /api/dashboard/scores` | RAGAS quality scores per agent |
| `GET /api/health` | Backend health proxy (`?svc=orchestrator\|rag\|quant\|market\|analytics\|reviewer\|mcp`) |

### Rewrites

| Pattern | Target |
|---|---|
| `/api/orch/:path*` | `http://localhost:8001/:path*` (orchestrator REST) |
| `/auth/:path*` | `http://localhost:8001/auth/:path*` (login/refresh/logout) |
| `/reports/:path*` | `http://localhost:8001/reports/:path*` (report downloads) |

## Design System

Warm ivory/clay palette. All CSS in `app/globals.css` — no Tailwind utility classes used despite being installed.

### Colors

| Token | Value | Role |
|---|---|---|
| `--clay` | `#8b6f4e` | Primary accent |
| `--ivory` | `#faf8f5` | Background |
| `--sand` | `#e0d8cc` | Borders |
| `--buy` | `#2a6b2a` | BUY signal |
| `--hold` | `#8b6f00` | HOLD signal |
| `--sell` | `#7a2c2c` | SELL signal |

### Agent Colors

| Agent | Foreground | Background |
|---|---|---|
| RAG | `#2c4a7c` (blue) | `#e0e6f0` |
| Quant | `#2a6b2a` (green) | `#e3f0e3` |
| Market Context | `#8b4513` (brown) | `#fce8d9` |
| Orchestrator | `#8b6f4e` (clay) | `#efe6d8` |
| MCP | `#5a3e7c` (purple) | `#ece2f4` |

### Typography

- **Headings**: Georgia, Times New Roman (`--serif`)
- **Body**: System font stack (`--sans`)
- **Code/Metadata**: JetBrains Mono (`--mono`)

## Lib

| File | Purpose |
|---|---|
| `lib/recentQueries.ts` | localStorage-backed recent query history (max 12) |

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `NEXT_PUBLIC_ORCHESTRATOR_URL` | Yes | Backend orchestrator URL (default `http://localhost:8001`) |
| `NEXT_PUBLIC_COPILOTKIT_API_KEY` | Yes | CopilotKit public API key |
| `LANGFUSE_PUBLIC_KEY` | For `/api/dashboard` | Langfuse public key |
| `LANGFUSE_SECRET_KEY` | For `/api/dashboard` | Langfuse secret key |
| `LANGFUSE_BASE_URL` | For `/api/dashboard` | Langfuse base URL (default `https://jp.cloud.langfuse.com`) |

## Scripts

```bash
npm run dev     # Development server (port 3000)
npm run build   # Production build
npm run start   # Production server
npm run lint    # ESLint
```

Use `run_ui.bat` / `stop_ui.bat` from the project root to start/stop all services including Next.js.

## Docker

Multi-stage Dockerfile (`Dockerfile`) for production deployment:

```bash
# Build and run via docker-compose (from project root)
docker compose up --build web
```

The Dockerfile has three stages:
1. **deps** — `node:20-alpine` + `npm ci`
2. **build** — `npm run build` with `output: 'standalone'`
3. **runner** — `node:20-alpine` + standalone output (~120MB final image, no `node_modules`)

The web service runs on port 3000 and is included in the CI Docker build matrix.
