#!/usr/bin/env node
import React from 'react';
import {render} from 'ink';
import chalk from 'chalk';
import {Command} from 'commander';
import {GatewayClient} from './gateway_client.ts';
import {TuiApp} from './tui_app.tsx';
import {startMockGateway} from './mock_gateway.ts';

async function main(): Promise<void> {
  const program = new Command();
  program
    .name('robber-ui')
    .description('Agent Robber TypeScript TUI for the Go gateway')
    .option('-g, --gateway <url>', 'gateway HTTP base', 'http://127.0.0.1:8790')
    .option('-r, --root <path>', 'workspace root', process.cwd())
    .option('--mock', 'start an in-process mock gateway', false)
    .parse(process.argv);

  const opts = program.opts<{gateway: string; root: string; mock: boolean}>();
  let base = opts.gateway;
  let mockClose: (() => Promise<void>) | undefined;
  if (opts.mock) {
    const mock = await startMockGateway();
    base = mock.url;
    mockClose = mock.close;
    process.stdout.write(chalk.cyan(`mock gateway ${mock.url}\n`));
  }

  const client = new GatewayClient({baseUrl: base});
  await client.connect();
  const instance = render(
    <TuiApp
      client={client}
      root={opts.root}
      onExit={() => {
        client.close();
        void mockClose?.();
      }}
    />,
    {patchConsole: false},
  );
  await instance.waitUntilExit();
  client.close();
  await mockClose?.();
}

main().catch((err) => {
  process.stderr.write(`${err instanceof Error ? err.message : String(err)}\n`);
  process.exitCode = 1;
});
