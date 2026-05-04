// LabFlow TypeScript SDK — hand-written, dependency-free.
//
// Why hand-written, not auto-generated?
//   * The OpenAPI surface is large but only a small slice is used by
//     90% of integrations (meetings, tasks, decisions, wiki, search).
//   * A 150-line bespoke client beats a 5,000-line generated one for
//     reading, debugging, and tree-shaking.
//   * Auto-generation can be added later — the existing /openapi.json
//     is unchanged and well-formed.

export interface ClientOptions {
  /** Base URL of the LabFlow API, e.g. "https://labflow.example". */
  baseUrl: string;
  /** API key for the X-API-Key header. Optional in dev when auth is disabled. */
  apiKey?: string;
  /** Inject a custom fetch (for tests, edge runtimes, etc.). */
  fetch?: typeof fetch;
}

export interface Meeting {
  id: number;
  title: string;
  meeting_type?: string;
  occurred_at?: string;
  finalized?: boolean;
}

export interface Task {
  id: number;
  title: string;
  status: string;
  owner_id?: number | null;
  due_date?: string | null;
  state?: string | null;
}

export interface Decision {
  id: number;
  statement?: string;
  rationale?: string;
  confidence?: number;
}

export interface WikiPage {
  id: number;
  slug: string;
  title: string;
  summary?: string | null;
  body?: string;
  current_revision_id?: number | null;
  updated_at?: string;
}

export interface FeedEvent {
  id: number;
  actor: string;
  action: string;
  entity_type: string;
  entity_id: number | null;
  created_at: string;
  metadata_json?: string | null;
}

export interface ForecastResult {
  sprint: { slug: string; task_count: number };
  method: string;
  samples: number;
  slope_per_day: number;
  remaining_today: number;
  eta_iso: string | null;
  eta_within_sprint: boolean | null;
  confidence_days: number;
  warning: string | null;
}

export class LabFlowError extends Error {
  constructor(public readonly status: number, message: string,
              public readonly body?: unknown) {
    super(message);
    this.name = "LabFlowError";
  }
}

export class LabFlowClient {
  private readonly baseUrl: string;
  private readonly apiKey?: string;
  private readonly fetchImpl: typeof fetch;

  constructor(opts: ClientOptions) {
    if (!opts.baseUrl) throw new Error("baseUrl is required");
    // Strip trailing slashes so we can join paths with a leading slash.
    // Use a manual loop instead of a regex to avoid any polynomial-time
    // backtracking concerns from CodeQL on user-supplied baseUrls.
    let base = opts.baseUrl;
    while (base.length > 0 && base.charCodeAt(base.length - 1) === 47) {
      base = base.slice(0, -1);
    }
    this.baseUrl = base;
    this.apiKey = opts.apiKey;
    this.fetchImpl = opts.fetch ?? fetch;
  }

  // ---- core ---------------------------------------------------------
  private async request<T>(method: string, path: string,
                           body?: unknown): Promise<T> {
    const headers: Record<string, string> = {
      "Accept": "application/json",
    };
    if (this.apiKey) headers["X-API-Key"] = this.apiKey;
    if (body !== undefined) headers["Content-Type"] = "application/json";

    const res = await this.fetchImpl(`${this.baseUrl}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await res.text();
    let data: unknown = undefined;
    if (text) {
      try { data = JSON.parse(text); } catch { data = text; }
    }
    if (!res.ok) {
      throw new LabFlowError(res.status,
        `LabFlow ${method} ${path} -> ${res.status}`, data);
    }
    return data as T;
  }

  // ---- meetings -----------------------------------------------------
  createMeeting(body: { title: string; transcript: string; notes?: string }) {
    return this.request<Meeting>("POST", "/api/meetings", body);
  }
  getMeeting(id: number) {
    return this.request<Meeting>("GET", `/api/meetings/${id}`);
  }

  // ---- tasks --------------------------------------------------------
  listOpenTasks() {
    return this.request<{ tasks: Task[] }>("GET",
      "/api/tasks?status=open");
  }
  transitionTask(id: number, toState: string) {
    return this.request<{ task_id: number; from_state: string; to_state: string }>(
      "POST", `/api/tasks/${id}/transition`, { to_state: toState });
  }

  // ---- decisions ----------------------------------------------------
  listDecisions(limit = 50) {
    return this.request<{ decisions: Decision[] }>("GET",
      `/api/decisions?limit=${limit}`);
  }

  // ---- wiki ---------------------------------------------------------
  upsertWikiPage(body: { title: string; body: string; slug?: string }) {
    return this.request<WikiPage>("POST", "/api/wiki/pages", body);
  }
  getWikiPage(slug: string) {
    return this.request<WikiPage & { backlinks: unknown[] }>(
      "GET", `/api/wiki/pages/${encodeURIComponent(slug)}`);
  }
  searchWiki(q: string, limit = 20) {
    const u = `/api/wiki/search?q=${encodeURIComponent(q)}&limit=${limit}`;
    return this.request<{ hits: WikiPage[] }>("GET", u);
  }

  // ---- search -------------------------------------------------------
  search(q: string, limit = 20) {
    return this.request<{ results: unknown[] }>("GET",
      `/api/search?q=${encodeURIComponent(q)}&limit=${limit}`);
  }

  // ---- watchers / feed ---------------------------------------------
  watch(entityType: string, entityId: number, delivery = "feed") {
    return this.request<{ id: number; delivery: string }>(
      "POST", "/api/watchers",
      { entity_type: entityType, entity_id: entityId, delivery });
  }
  feed(limit = 50) {
    return this.request<{ events: FeedEvent[] }>("GET",
      `/api/feed?limit=${limit}`);
  }

  // ---- forecasting & dashboards ------------------------------------
  forecastSprint(slug: string) {
    return this.request<ForecastResult>("GET",
      `/api/forecast/sprint/${encodeURIComponent(slug)}`);
  }
  dashboardData(slug: string) {
    return this.request<{ slug: string; widgets: unknown[] }>(
      "GET", `/api/dashboards/${encodeURIComponent(slug)}/data`);
  }

  // ---- audit chain --------------------------------------------------
  verifyAuditChain() {
    return this.request<{ ok: boolean; checked: number;
                          first_break_id: number | null }>(
      "GET", "/api/audit/verify");
  }
}

export default LabFlowClient;
