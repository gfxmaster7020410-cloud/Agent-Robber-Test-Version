import {EventEmitter} from 'node:events';
import Table from 'cli-table3';
import WebSocket from 'ws';
import type {
  AgentRow,
  DecisionRow,
  GatewayEvent,
  QuestionPrompt,
  SsotSnapshot,
  SubmitTaskInput,
  TaskAck,
  TaskRow,
} from './types.ts';

const BACKOFF_MS = [250, 500, 1000, 2000, 4000, 5000] as const;

export function assertLoopback(raw: string): URL {
  const url = new URL(raw);
  const host = url.hostname.toLowerCase();
  if (host !== '127.0.0.1' && host !== 'localhost' && host !== '::1') {
    throw new Error('gateway must be a loopback address');
  }
  return url;
}

export function httpToWs(httpUrl: string): string {
  const url = new URL(httpUrl);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.toString().replace(/\/$/, '');
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

export class SessionStore {
  tasks = new Map<string, TaskRow>();
  agents = new Map<string, AgentRow>();
  decisions = new Map<string, DecisionRow>();
  projectId = '';
  pendingQuestion: QuestionPrompt | null = null;

  applySnapshot(snap: SsotSnapshot): void {
    this.projectId = snap.project_id ?? this.projectId;
    this.tasks.clear();
    this.agents.clear();
    this.decisions.clear();
    for (const row of snap.tasks ?? []) {
      if (row?.id) {
        this.tasks.set(row.id, row);
      }
    }
    for (const row of snap.agents ?? []) {
      if (row?.id) {
        this.agents.set(row.id, row);
      }
    }
    for (const row of snap.decisions ?? []) {
      if (row?.id) {
        this.decisions.set(row.id, row);
      }
    }
  }

  applySsot(table: string, rows: Record<string, unknown>[]): void {
    for (const raw of rows) {
      const id = String(raw.id ?? '');
      if (!id) {
        continue;
      }
      if (table === 'task_state') {
        this.tasks.set(id, {
          id,
          title: String(raw.title ?? ''),
          status: String(raw.status ?? ''),
          depends_on: raw.depends_on != null ? String(raw.depends_on) : '[]',
          updated_at: Number(raw.updated_at ?? 0),
        });
      } else if (table === 'agent_state') {
        this.agents.set(id, {
          id,
          worker_id: String(raw.worker_id ?? ''),
          phase: String(raw.phase ?? ''),
          loop_status: String(raw.loop_status ?? ''),
          updated_at: Number(raw.updated_at ?? 0),
        });
      } else if (table === 'decisions_ledger') {
        this.decisions.set(id, {
          id,
          branch: String(raw.branch ?? 'main'),
          parent_id: raw.parent_id != null ? String(raw.parent_id) : undefined,
          choice: String(raw.choice ?? ''),
          version_tag: String(raw.version_tag ?? ''),
          is_head: Boolean(raw.is_head),
          is_final: Boolean(raw.is_final),
          confidence_score: Number(raw.confidence_score ?? 0),
          summary: String(raw.summary ?? ''),
        });
      }
    }
  }

  pendingConflicts(): DecisionRow[] {
    return [...this.decisions.values()].filter((row) => !row.is_final);
  }

  graphText(): string {
    const table = new Table({
      head: ['status', 'id', 'title'],
      chars: {
        top: '-',
        'top-mid': '+',
        'top-left': '+',
        'top-right': '+',
        bottom: '-',
        'bottom-mid': '+',
        'bottom-left': '+',
        'bottom-right': '+',
        left: '|',
        'left-mid': '|',
        mid: '-',
        'mid-mid': '+',
        right: '|',
        'right-mid': '|',
        middle: '|',
      },
      style: {head: [], border: []},
      colWidths: [10, 10, 28],
      wordWrap: true,
    });
    const tasks = [...this.tasks.values()].slice(0, 12);
    if (!tasks.length) {
      table.push(['-', '-', '(empty - run /sync)']);
    }
    for (const task of tasks) {
      table.push([task.status, task.id.slice(0, 8), task.title.slice(0, 26)]);
    }
    const lines = ['Task DAG', table.toString(), 'Hypothesis tree'];
    const decisions = [...this.decisions.values()].slice(0, 16);
    if (!decisions.length) {
      lines.push('  (no decisions yet)');
    }
    for (const row of decisions) {
      const mark = row.is_head ? '*' : row.is_final ? 'x' : '-';
      const parent = row.parent_id ? ` parent=${row.parent_id.slice(0, 8)}` : '';
      lines.push(
        `  ${mark} ${row.branch ?? 'main'} ${row.choice ?? ''} v=${row.version_tag ?? ''}${parent}`,
      );
    }
    const agent = [...this.agents.values()][0];
    if (agent) {
      lines.push(`Agent ${agent.worker_id || agent.id} phase=${agent.phase} loop=${agent.loop_status}`);
    }
    return lines.join('\n');
  }
}

export interface GatewayClientOptions {
  baseUrl?: string;
  fetchImpl?: typeof fetch;
  WebSocketImpl?: typeof WebSocket;
}

export class GatewayClient extends EventEmitter {
  readonly baseUrl: string;
  readonly store = new SessionStore();
  connected = false;
  private closed = false;
  private ssotSock: WebSocket | null = null;
  private tokenSock: WebSocket | null = null;
  private timers: NodeJS.Timeout[] = [];
  private attempt = 0;
  private readonly fetchImpl: typeof fetch;
  private readonly WebSocketImpl: typeof WebSocket;

  constructor(opts: GatewayClientOptions = {}) {
    super();
    this.baseUrl = (opts.baseUrl ?? 'http://127.0.0.1:8790').replace(/\/$/, '');
    assertLoopback(this.baseUrl);
    this.fetchImpl = opts.fetchImpl ?? fetch;
    this.WebSocketImpl = opts.WebSocketImpl ?? WebSocket;
    this.setMaxListeners(50);
  }

  onEvent(handler: (event: GatewayEvent) => void): () => void {
    this.on('event', handler);
    return () => this.off('event', handler);
  }

  private emitEvent(event: GatewayEvent): void {
    if (event.type === 'ssot') {
      this.store.applySsot(event.table, event.rows);
    } else if (event.type === 'snapshot') {
      this.store.applySnapshot(event.snapshot);
    } else if (event.type === 'question') {
      this.store.pendingQuestion = {
        id: event.id,
        question: event.question,
        options: event.options,
        allow_custom: event.allow_custom,
        timeout: event.timeout,
        source_worker: event.source_worker,
        task_id: event.task_id,
        created_at: event.created_at,
      };
    } else if (event.type === 'question_answer') {
      if (this.store.pendingQuestion?.id === event.question_id) {
        this.store.pendingQuestion = null;
      }
    }
    this.emit('event', event);
  }

  async health(): Promise<Record<string, unknown>> {
    const res = await this.fetchImpl(`${this.baseUrl}/health`);
    if (!res.ok) {
      throw new Error(`health ${res.status}`);
    }
    return (await res.json()) as Record<string, unknown>;
  }

  async snapshot(): Promise<SsotSnapshot> {
    const res = await this.fetchImpl(`${this.baseUrl}/api/v1/ssot/snapshot`);
    if (!res.ok) {
      throw new Error(`snapshot ${res.status}`);
    }
    const body = (await res.json()) as SsotSnapshot;
    const snap: SsotSnapshot = {
      project_id: body.project_id,
      tasks: Array.isArray(body.tasks) ? body.tasks : [],
      agents: Array.isArray(body.agents) ? body.agents : [],
      decisions: Array.isArray(body.decisions) ? body.decisions : [],
    };
    this.store.applySnapshot(snap);
    this.emitEvent({type: 'snapshot', snapshot: snap});
    return snap;
  }

  async submitTask(input: SubmitTaskInput): Promise<TaskAck> {
    const prompt = (input.prompt ?? '').trim();
    if (!prompt && !(input.title ?? '').trim()) {
      throw new Error('prompt is empty');
    }
    const res = await this.fetchImpl(`${this.baseUrl}/api/v1/agent/task`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        prompt,
        title: input.title ?? prompt.slice(0, 80),
        task_type: input.task_type ?? 'FAST_DRAFT',
      }),
    });
    const body = (await res.json().catch(() => ({}))) as TaskAck;
    if (!res.ok) {
      throw new Error(`task ${res.status}`);
    }
    return body;
  }

  answerQuestion(questionId: string, answer: string): void {
    const payload = JSON.stringify({type: 'answer', question_id: questionId, answer});
    const sock = this.ssotSock ?? this.tokenSock;
    if (sock && sock.readyState === WebSocket.OPEN) {
      sock.send(payload);
    }
  }

  async connect(): Promise<void> {
    this.closed = false;
    this.openSocket('ssot', `${httpToWs(this.baseUrl)}/api/v1/stream?channel=ssot`);
    this.openSocket('tokens', `${httpToWs(this.baseUrl)}/api/v1/stream`);
    await Promise.race([
      this.waitOpen(),
      sleep(2000),
    ]);
    try {
      await this.snapshot();
    } catch {
      /* snapshot is optional when the mock only speaks WS */
    }
  }

  async resync(): Promise<SsotSnapshot> {
    this.dropSockets();
    this.attempt = 0;
    await this.connect();
    try {
      return await this.snapshot();
    } catch {
      return {
        project_id: this.store.projectId,
        tasks: [...this.store.tasks.values()],
        agents: [...this.store.agents.values()],
        decisions: [...this.store.decisions.values()],
      };
    }
  }

  close(): void {
    this.closed = true;
    this.connected = false;
    for (const t of this.timers) {
      clearTimeout(t);
    }
    this.timers = [];
    this.dropSockets();
  }

  private dropSockets(): void {
    this.ssotSock?.removeAllListeners();
    this.tokenSock?.removeAllListeners();
    try {
      this.ssotSock?.close();
    } catch {
      /* ignore */
    }
    try {
      this.tokenSock?.close();
    } catch {
      /* ignore */
    }
    this.ssotSock = null;
    this.tokenSock = null;
  }

  private waitOpen(): Promise<void> {
    if (this.connected) {
      return Promise.resolve();
    }
    return new Promise((resolve) => {
      const done = (event: GatewayEvent) => {
        if (event.type === 'open') {
          this.off('event', done);
          resolve();
        }
      };
      this.on('event', done);
    });
  }

  private openSocket(channel: 'ssot' | 'tokens', url: string): void {
    if (this.closed) {
      return;
    }
    const sock = new this.WebSocketImpl(url);
    if (channel === 'ssot') {
      this.ssotSock = sock;
    } else {
      this.tokenSock = sock;
    }
    sock.on('open', () => {
      this.attempt = 0;
      this.connected = true;
      this.emitEvent({type: 'open', channel});
    });
    sock.on('message', (data) => {
      this.handleMessage(channel, data.toString());
    });
    sock.on('close', (code) => {
      if (channel === 'ssot') {
        this.ssotSock = null;
      } else {
        this.tokenSock = null;
      }
      this.connected = Boolean(this.ssotSock || this.tokenSock);
      this.emitEvent({type: 'close', channel, code});
      this.scheduleReconnect(channel, url);
    });
    sock.on('error', (err) => {
      this.emitEvent({type: 'error', message: err.message});
    });
  }

  private scheduleReconnect(channel: 'ssot' | 'tokens', url: string): void {
    if (this.closed) {
      return;
    }
    const delay = BACKOFF_MS[Math.min(this.attempt, BACKOFF_MS.length - 1)];
    this.attempt += 1;
    const timer = setTimeout(() => this.openSocket(channel, url), delay);
    this.timers.push(timer);
  }

  private handleMessage(channel: 'ssot' | 'tokens', raw: string): void {
    const pieces = splitPayloads(raw);
    for (const piece of pieces) {
      const parsed = tryJson(piece);
      if (channel === 'ssot') {
        const rec = asRecord(parsed);
        if (rec && (rec.type === 'ssot' || rec.table)) {
          this.emitEvent({
            type: 'ssot',
            table: String(rec.table ?? ''),
            op: String(rec.op ?? 'update'),
            rows: Array.isArray(rec.rows) ? (rec.rows as Record<string, unknown>[]) : [],
            seq: Number(rec.seq ?? 0),
            ts: Number(rec.ts ?? rec.Watermark ?? 0),
          });
          continue;
        }
        if (rec && rec.type === 'question') {
          this.emitEvent({
            type: 'question',
            id: String(rec.id ?? ''),
            question: String(rec.question ?? ''),
            options: Array.isArray(rec.options) ? (rec.options as string[]) : [],
            allow_custom: Boolean(rec.allow_custom ?? true),
            timeout: Number(rec.timeout ?? 120),
            source_worker: String(rec.source_worker ?? ''),
            task_id: String(rec.task_id ?? ''),
            created_at: Number(rec.created_at ?? 0),
          });
          continue;
        }
        if (rec && rec.type === 'question_answer') {
          this.emitEvent({
            type: 'question_answer',
            question_id: String(rec.question_id ?? ''),
            answer: String(rec.answer ?? ''),
          });
          continue;
        }
      }
      const text = tokenText(parsed, piece);
      if (text) {
        this.emitEvent({type: 'token', text, raw: piece});
      }
    }
  }
}

function splitPayloads(raw: string): string[] {
  const trimmed = raw.trim();
  if (!trimmed || trimmed.startsWith(':')) {
    return [];
  }
  if (trimmed.includes('\n\n') || trimmed.startsWith('data:')) {
    return trimmed
      .split(/\n\n/)
      .map((block) => {
        const line = block
          .split('\n')
          .map((row) => row.replace(/^data:\s?/, ''))
          .join('');
        return line.trim();
      })
      .filter(Boolean);
  }
  return [trimmed];
}

function tryJson(raw: string): unknown {
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function tokenText(parsed: unknown, fallback: string): string {
  const rec = asRecord(parsed);
  if (!rec) {
    return fallback;
  }
  if (typeof rec.text === 'string') {
    return rec.text;
  }
  if (typeof rec.content === 'string') {
    return rec.content;
  }
  if (typeof rec.delta === 'string') {
    return rec.delta;
  }
  if (typeof rec.token === 'string') {
    return rec.token;
  }
  if (rec.type === 'ssot') {
    return '';
  }
  return fallback;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
