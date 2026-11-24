import assert from 'node:assert/strict';
import {describe, it} from 'node:test';
import {GatewayClient} from '../src/gateway_client.ts';
import {SlashCommandParser} from '../src/command_parser.ts';
import {CommandRouter} from '../src/command_router.ts';
import {startMockGateway} from '../src/mock_gateway.ts';
import {formatLogLine, formatStatePane} from '../src/tui_app.tsx';

describe('GatewayClient + mock stream', () => {
  it('reconnects-ready client receives tokens, ssot diffs, and task ack', async () => {
    const mock = await startMockGateway();
    const client = new GatewayClient({baseUrl: mock.url});
    const events: string[] = [];
    client.onEvent((event) => events.push(event.type));
    await client.connect();

    await waitUntil(() => events.includes('token') && events.includes('ssot'));
    assert.equal(client.store.agents.get('a1')?.phase, 'idle');

    mock.broadcastToken('chunk-two');
    mock.broadcastSsot({
      type: 'ssot',
      table: 'task_state',
      op: 'update',
      seq: 9,
      rows: [{id: 't-live', title: 'live', status: 'running'}],
    });
    await waitUntil(() => client.store.tasks.has('t-live'));

    const ack = await client.submitTask({prompt: 'hello from tui'});
    assert.ok(ack.id);
    assert.equal(mock.tasks.at(-1)?.prompt, 'hello from tui');

    client.close();
    await mock.close();
    assert.ok(events.includes('open'));
    assert.ok(formatLogLine({type: 'token', text: 'chunk-two'}) === 'chunk-two');
  });

  it('runs /audit and /graph against the mock without blocking', async () => {
    const mock = await startMockGateway();
    const client = new GatewayClient({baseUrl: mock.url});
    await client.connect();
    const parser = new SlashCommandParser();
    const router = new CommandRouter({
      client,
      parser,
      root: process.cwd(),
      pacing: 'running',
      runIndexer: async () => JSON.stringify({files_parsed: 4, elapsed_ms: 1}),
    });

    const started = Date.now();
    const audit = await router.handle('/audit');
    const graph = await router.handle('/graph');
    const pause = await router.handle('/pause');
    const elapsed = Date.now() - started;

    assert.equal(audit.ok, true);
    assert.match(audit.content, /files_parsed/);
    assert.match(graph.content, /Task DAG/);
    assert.equal(pause.pacing, 'paused');
    assert.ok(elapsed < 2000, `commands lagged (${elapsed}ms)`);

    const pane = formatStatePane(client, pause.pacing ?? 'running', true);
    assert.match(pane, /paused/);
    assert.match(pane, /live/);

    client.close();
    await mock.close();
  });
});

async function waitUntil(pred: () => boolean, timeoutMs = 2000): Promise<void> {
  const start = Date.now();
  while (!pred()) {
    if (Date.now() - start > timeoutMs) {
      throw new Error('timeout waiting for gateway event');
    }
    await new Promise((r) => setTimeout(r, 20));
  }
}
