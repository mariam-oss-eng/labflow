# `@labflow/sdk` — TypeScript client

Hand-written, dependency-free, ~150-line client for the LabFlow REST
API. Works in Node ≥18 and modern browsers (anything with `fetch`).

```bash
npm install @labflow/sdk
```

```ts
import { LabFlowClient } from "@labflow/sdk";

const client = new LabFlowClient({
  baseUrl: "https://labflow.example",
  apiKey: process.env.LABFLOW_API_KEY,
});

await client.createMeeting({
  title: "Sprint planning",
  transcript: "We decided to use Postgres. TODO: write migration. Owner: @alice",
});

const tasks = await client.listOpenTasks();
console.log(tasks.tasks.length, "open");

const forecast = await client.forecastSprint("sprint-3");
console.log(`Projected completion: ${forecast.eta_iso} (±${forecast.confidence_days}d)`);
```

## Why hand-written?

The OpenAPI surface is broad but most integrations touch ≤10
endpoints. A focused 150-line client is easier to read, debug,
tree-shake, and audit than 5,000 lines of generated code. If you need
the *full* surface, point your generator of choice at
`https://your.host/openapi.json` — that contract is unchanged.

## Coverage

- meetings: `createMeeting`, `getMeeting`
- tasks: `listOpenTasks`, `transitionTask`
- decisions: `listDecisions`
- wiki: `upsertWikiPage`, `getWikiPage`, `searchWiki`
- search: `search`
- watchers / feed: `watch`, `feed`
- forecasting: `forecastSprint`
- dashboards: `dashboardData`
- audit: `verifyAuditChain`

Open an issue (or, even better, a PR) if you'd like more methods
shipped here — the bar is "common path for typical integrations".

## Dependency policy

This package has **zero runtime dependencies**. The only `devDependency`
is `typescript` for the build. We will keep it that way.
