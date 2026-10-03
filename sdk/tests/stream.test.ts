import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  LedgerLensAlertStream,
  type ConnectionStateEvent,
  type RiskScoreAlert,
  type WebSocketLike,
} from "../src/stream";

class FakeSocket implements WebSocketLike {
  onopen: WebSocketLike["onopen"] = null;
  onmessage: WebSocketLike["onmessage"] = null;
  onclose: WebSocketLike["onclose"] = null;
  onerror: WebSocketLike["onerror"] = null;
  sent: string[] = [];
  closed = false;
  constructor(public url: string) {}
  send(data: string) {
    this.sent.push(data);
  }
  close() {
    this.closed = true;
  }
  open() {
    this.onopen?.({});
  }
  push(msg: unknown) {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }
  serverDrop(code = 1001) {
    this.onclose?.({ code });
  }
}

const alert = (wallet: string): RiskScoreAlert => ({
  wallet,
  asset_pair: "XLM/USDC",
  score: 90,
  benford_flag: true,
  ml_flag: true,
  confidence: 0.9,
  timestamp: "2026-01-01T00:00:00Z",
});

function setup() {
  const sockets: FakeSocket[] = [];
  const states: ConnectionStateEvent[] = [];
  const stream = new LedgerLensAlertStream({
    baseUrl: "ws://api.test/",
    apiKey: "k",
    initialDelayMs: 100,
    maxDelayMs: 1000,
    random: () => 0.5,
    webSocketFactory: (url) => {
      const s = new FakeSocket(url);
      sockets.push(s);
      return s;
    },
  });
  stream.onStateChange((e) => states.push(e));
  return { stream, sockets, states };
}

describe("LedgerLensAlertStream", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("reconnects and resubscribes after a server disconnect", () => {
    const { stream, sockets, states } = setup();
    const received: string[] = [];
    stream.subscribe((a) => received.push(a.wallet), "GA");
    expect(sockets[0].url).toBe(
      "ws://api.test/ws/alerts?api_key=k&wallet_filter=GA",
    );
    sockets[0].open();

    sockets[0].serverDrop(1001);
    expect(stream.state).toBe("reconnecting");
    vi.advanceTimersByTime(50);
    expect(sockets).toHaveLength(2);
    expect(sockets[1].url).toBe(sockets[0].url);
    sockets[1].open();
    sockets[1].push({ event: "risk_score_alert", data: alert("GA") });

    expect(received).toEqual(["GA"]);
    expect(states.map((s) => s.state)).toEqual([
      "connecting",
      "open",
      "reconnecting",
      "open",
    ]);
    expect(states[2]).toMatchObject({
      attempt: 1,
      delayMs: 50,
      closeCode: 1001,
    });
  });

  it("applies exponential backoff with jitter capped at maxDelayMs", () => {
    const { stream, sockets, states } = setup();
    stream.subscribe(() => {});
    for (let i = 0; i < 6; i++) {
      sockets[i].serverDrop();
      vi.runOnlyPendingTimers();
    }
    const delays = states
      .filter((s) => s.state === "reconnecting")
      .map((s) => s.delayMs);
    expect(delays).toEqual([50, 100, 200, 400, 500, 500]);
  });

  it("resets backoff after a successful reconnect", () => {
    const { stream, sockets, states } = setup();
    stream.subscribe(() => {});
    sockets[0].serverDrop();
    vi.runOnlyPendingTimers();
    sockets[1].serverDrop();
    vi.runOnlyPendingTimers();
    sockets[2].open();
    sockets[2].serverDrop();
    const last = states[states.length - 1];
    expect(last).toMatchObject({
      state: "reconnecting",
      attempt: 1,
      delayMs: 50,
    });
  });

  it("answers server pings and filters alerts client-side for mixed subscriptions", () => {
    const { stream, sockets } = setup();
    const a: string[] = [];
    const all: string[] = [];
    stream.subscribe((x) => a.push(x.wallet), "GA");
    stream.subscribe((x) => all.push(x.wallet));
    const live = sockets[sockets.length - 1];
    expect(live.url).toBe("ws://api.test/ws/alerts?api_key=k");
    live.push({ event: "ping" });
    expect(live.sent).toEqual([JSON.stringify({ event: "pong" })]);
    live.push({ event: "risk_score_alert", data: alert("GB") });
    live.push({ event: "risk_score_alert", data: alert("GA") });
    expect(a).toEqual(["GA"]);
    expect(all).toEqual(["GB", "GA"]);
  });

  it("does not reconnect after close()", () => {
    const { stream, sockets } = setup();
    stream.subscribe(() => {});
    sockets[0].open();
    stream.close();
    expect(sockets[0].closed).toBe(true);
    vi.runAllTimers();
    expect(sockets).toHaveLength(1);
    expect(stream.state).toBe("closed");
  });
});
