import http from 'node:http';
import {WebSocketServer, type WebSocket} from 'ws';

export interface MockGateway {
  url: string;
  port: number;
  close: () => Promise<void>;
  broadcastSsot: (event: Record<string, unknown>) => void;
  broadcastToken: (text: string) => void;
  tasks: {id: string; prompt: string}[];
}

export async function startMockGateway(): Promise<MockGateway> {
  const tasks: {id: string; prompt: string}[] = [];
  const ssotClients = new Set<WebSocket>();
  const tokenClients = new Set<WebSocket>();
  let seq = 1;
  const snapshot = {
    project_id: 'mock-project',
    tasks: [
      {id: 't1', title: 'seed', status: 'queued', depends_on: '[]'},
    ],
    agents: [{id: 'a1', worker_id: 'brain', phase: 'idle', loop_status: ''}],
    decisions: [
      {
        id: 'd1',
        branch: 'main',
        choice: 'keep-left',
        version_tag: 'v1',
        is_head: true,
        is_final: false,
        summary: 'merge conflict',
      },
    ],
  };

  const server = http.createServer((req, res) => {
    const url = new URL(req.url ?? '/', 'http://127.0.0.1');
    if (req.method === 'GET' && url.pathname === '/health') {
      return json(res, 200, {ok: true, gateway: 'mock'});
    }
    if (req.method === 'GET' && url.pathname === '/api/v1/ssot/snapshot') {
      return json(res, 200, snapshot);
    }
    if (req.method === 'GET' && url.pathname === '/api/v1/health/models') {
      return json(res, 200, {targets: []});
    }
    if (req.method === 'POST' && url.pathname === '/api/v1/agent/task') {
      let body = '';
      req.on('data', (c) => {
        body += c;
        if (body.length > 1_000_000) {
          req.destroy();
        }
      });
      req.on('end', () => {
        let prompt = '';
        try {
          prompt = String(JSON.parse(body).prompt ?? '');
        } catch {
          prompt = body;
        }
        const id = `task-${tasks.length + 1}`;
        tasks.push({id, prompt});
        snapshot.tasks = [
          {id, title: prompt.slice(0, 80) || 'task', status: 'queued', depends_on: '[]'},
          ...snapshot.tasks,
        ].slice(0, 24);
        const event = {
          type: 'ssot',
          table: 'task_state',
          op: 'insert',
          seq: seq++,
          rows: [{id, title: prompt.slice(0, 80), status: 'queued'}],
        };
        for (const ws of ssotClients) {
          if (ws.readyState === ws.OPEN) {
            ws.send(JSON.stringify(event));
          }
        }
        json(res, 202, {id, status: 'queued', forwarded: true});
      });
      return;
    }
    res.writeHead(404);
    res.end('not found');
  });

  const wss = new WebSocketServer({noServer: true});
  server.on('upgrade', (req, socket, head) => {
    const url = new URL(req.url ?? '/', 'http://127.0.0.1');
    if (url.pathname !== '/api/v1/stream') {
      socket.destroy();
      return;
    }
    wss.handleUpgrade(req, socket, head, (ws) => {
      const ssot = url.searchParams.get('channel') === 'ssot' || url.searchParams.get('events') === '1';
      if (ssot) {
        ssotClients.add(ws);
        ws.send(
          JSON.stringify({
            type: 'ssot',
            table: 'agent_state',
            op: 'update',
            seq: seq++,
            rows: [{id: 'a1', worker_id: 'brain', phase: 'idle', loop_status: 'ready'}],
          }),
        );
      } else {
        tokenClients.add(ws);
        ws.send(JSON.stringify({type: 'token', text: 'hello-token'}));
      }
      ws.on('close', () => {
        ssotClients.delete(ws);
        tokenClients.delete(ws);
      });
    });
  });

  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const addr = server.address();
  if (!addr || typeof addr === 'string') {
    throw new Error('mock bind failed');
  }

  return {
    url: `http://127.0.0.1:${addr.port}`,
    port: addr.port,
    tasks,
    broadcastSsot(event) {
      const payload = JSON.stringify(event);
      for (const ws of ssotClients) {
        if (ws.readyState === ws.OPEN) {
          ws.send(payload);
        }
      }
    },
    broadcastToken(text) {
      const payload = JSON.stringify({type: 'token', text});
      for (const ws of tokenClients) {
        if (ws.readyState === ws.OPEN) {
          ws.send(payload);
        }
      }
    },
    close: () =>
      new Promise((resolve, reject) => {
        for (const ws of [...ssotClients, ...tokenClients]) {
          ws.close();
        }
        server.close((err) => (err ? reject(err) : resolve()));
      }),
  };
}

function json(res: http.ServerResponse, status: number, body: unknown): void {
  res.writeHead(status, {'Content-Type': 'application/json'});
  res.end(JSON.stringify(body));
}

const isMain = process.argv[1]?.includes('mock_gateway');
if (isMain) {
  const mock = await startMockGateway();
  process.stdout.write(`mock gateway ${mock.url}\n`);
}
