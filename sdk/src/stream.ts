/**
 * Auto-reconnecting client for the `/ws/alerts` WebSocket endpoint
 * (`api/ws_router.py`).
 *
 * On any close the consumer did not request, the stream reconnects with
 * exponential backoff plus full jitter and transparently restores every
 * active subscription. Connection-state changes are observable through
 * {@link LedgerLensAlertStream.onStateChange}.
 */

/** Minimal WebSocket surface used by the stream (satisfied by the WHATWG `WebSocket`). */
export interface WebSocketLike {
  onopen: ((ev: unknown) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  onclose: ((ev: { code: number; reason?: string }) => void) | null;
  onerror: ((ev: unknown) => void) | null;
  send(data: string): void;
  close(code?: number, reason?: string): void;
}

export type WebSocketFactory = (url: string) => WebSocketLike;

export type ConnectionState = "connecting" | "open" | "reconnecting" | "closed";

export interface ConnectionStateEvent {
  state: ConnectionState;
  /** Reconnect attempt number (1-based); 0 outside a reconnect cycle. */
  attempt: number;
  /** Delay in ms before the next attempt, set when `state` is `"reconnecting"`. */
  delayMs?: number;
  /** Close code of the connection that dropped, when applicable. */
  closeCode?: number;
}

/** A `risk_score_alert` payload pushed by the server. */
export interface RiskScoreAlert {
  wallet: string;
  asset_pair: string;
  score: number;
  benford_flag: boolean;
  ml_flag: boolean;
  confidence: number;
  timestamp: string;
}

export interface LedgerLensAlertStreamOptions {
  /** WebSocket base URL, e.g. `wss://api.example.com`. */
  baseUrl: string;
  /** Admin API key passed as the `api_key` query parameter. */
  apiKey: string;
  /** Initial backoff delay in ms. Default: 500. */
  initialDelayMs?: number;
  /**
   * Upper bound in ms for the backoff delay. Default: 30000 (30 s).
   * Each attempt waits a uniformly random delay in
   * `[0, min(maxDelayMs, initialDelayMs * 2^(attempt-1))]` ("full jitter").
   */
  maxDelayMs?: number;
  /** Custom WebSocket constructor (defaults to the global `WebSocket`). */
  webSocketFactory?: WebSocketFactory;
  /** Random source in `[0, 1)`, injectable for tests. Default: `Math.random`. */
  random?: () => number;
}

type AlertHandler = (alert: RiskScoreAlert) => void;
type StateListener = (event: ConnectionStateEvent) => void;

interface Subscription {
  walletFilter: string | null;
  handler: AlertHandler;
}

export class LedgerLensAlertStream {
  private readonly baseUrl: string;
  private readonly apiKey: string;
  private readonly initialDelayMs: number;
  private readonly maxDelayMs: number;
  private readonly factory: WebSocketFactory;
  private readonly random: () => number;

  private readonly subscriptions = new Set<Subscription>();
  private readonly stateListeners = new Set<StateListener>();
  private socket: WebSocketLike | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private attempt = 0;
  private stopped = true;
  private _state: ConnectionState = "closed";

  constructor(options: LedgerLensAlertStreamOptions) {
    this.baseUrl = options.baseUrl.replace(/\/$/, "");
    this.apiKey = options.apiKey;
    this.initialDelayMs = options.initialDelayMs ?? 500;
    this.maxDelayMs = options.maxDelayMs ?? 30_000;
    this.random = options.random ?? Math.random;
    this.factory =
      options.webSocketFactory ??
      ((url) =>
        new (
          globalThis as unknown as {
            WebSocket: new (u: string) => WebSocketLike;
          }
        ).WebSocket(url));
  }

  /** Current connection state. */
  get state(): ConnectionState {
    return this._state;
  }

  /** Registers a connection-state listener. Returns an unsubscribe function. */
  onStateChange(listener: StateListener): () => void {
    this.stateListeners.add(listener);
    return () => this.stateListeners.delete(listener);
  }

  /**
   * Subscribes to alerts, optionally for a single wallet. Opens the connection
   * if needed. Subscriptions survive reconnects. Returns an unsubscribe function.
   */
  subscribe(
    handler: AlertHandler,
    walletFilter: string | null = null,
  ): () => void {
    const sub: Subscription = { walletFilter, handler };
    const before = this.serverFilter();
    this.subscriptions.add(sub);
    this.applySubscriptionChange(before);
    return () => {
      const prev = this.serverFilter();
      if (this.subscriptions.delete(sub)) this.applySubscriptionChange(prev);
    };
  }

  /** Closes the connection and stops reconnecting. */
  close(): void {
    this.stopped = true;
    this.clearTimer();
    const socket = this.socket;
    this.socket = null;
    if (socket) {
      socket.onclose = null;
      socket.close(1000, "client closed");
    }
    this.setState({ state: "closed", attempt: 0 });
  }

  // The server supports one `wallet_filter` per connection: use it when every
  // subscription targets the same wallet, otherwise filter client-side.
  private serverFilter(): string | null {
    const filters = new Set([...this.subscriptions].map((s) => s.walletFilter));
    return filters.size === 1 ? [...filters][0] : null;
  }

  private applySubscriptionChange(previousFilter: string | null): void {
    if (this.subscriptions.size === 0) {
      this.close();
      return;
    }
    if (this.stopped) {
      this.stopped = false;
      this.attempt = 0;
      this.connect();
    } else if (this.serverFilter() !== previousFilter && this.socket) {
      // Re-open with the new server-side filter without surfacing a reconnect.
      const socket = this.socket;
      socket.onclose = null;
      socket.close(1000, "resubscribe");
      this.connect();
    }
  }

  private url(): string {
    const params = new URLSearchParams({ api_key: this.apiKey });
    const filter = this.serverFilter();
    if (filter) params.set("wallet_filter", filter);
    return `${this.baseUrl}/ws/alerts?${params.toString()}`;
  }

  private connect(): void {
    this.clearTimer();
    if (this.attempt === 0) this.setState({ state: "connecting", attempt: 0 });
    const socket = this.factory(this.url());
    this.socket = socket;

    socket.onopen = () => {
      this.attempt = 0;
      this.setState({ state: "open", attempt: 0 });
    };
    socket.onmessage = (ev) => this.handleMessage(socket, ev.data);
    socket.onerror = () => {
      // A close event always follows; reconnect is handled there.
    };
    socket.onclose = (ev) => {
      if (this.socket !== socket) return;
      this.socket = null;
      if (!this.stopped) this.scheduleReconnect(ev.code);
    };
  }

  private scheduleReconnect(closeCode: number): void {
    this.attempt += 1;
    const cap = Math.min(
      this.maxDelayMs,
      this.initialDelayMs * 2 ** (this.attempt - 1),
    );
    const delayMs = Math.floor(this.random() * cap);
    this.setState({
      state: "reconnecting",
      attempt: this.attempt,
      delayMs,
      closeCode,
    });
    this.timer = setTimeout(() => this.connect(), delayMs);
  }

  private handleMessage(socket: WebSocketLike, data: unknown): void {
    let msg: { event?: string; data?: RiskScoreAlert };
    try {
      msg = JSON.parse(String(data));
    } catch {
      return;
    }
    if (msg.event === "ping") {
      socket.send(JSON.stringify({ event: "pong" }));
    } else if (msg.event === "risk_score_alert" && msg.data) {
      const alert = msg.data;
      for (const sub of this.subscriptions) {
        if (sub.walletFilter === null || sub.walletFilter === alert.wallet)
          sub.handler(alert);
      }
    }
  }

  private setState(event: ConnectionStateEvent): void {
    this._state = event.state;
    for (const listener of this.stateListeners) listener(event);
  }

  private clearTimer(): void {
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }
}
