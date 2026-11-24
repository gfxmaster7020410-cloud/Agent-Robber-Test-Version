import {spawn} from 'node:child_process';
import {existsSync} from 'node:fs';
import path from 'node:path';
import type {GatewayClient} from './gateway_client.ts';
import type {SlashCommandParser} from './command_parser.ts';
import type {CommandResult, PacingState} from './types.ts';

export interface RouterContext {
  client: GatewayClient;
  parser: SlashCommandParser;
  root: string;
  pacing: PacingState;
  runIndexer?: (root: string) => Promise<string>;
}

export class CommandRouter {
  pacing: PacingState = 'running';
  pendingChoices: string[] = [];

  constructor(private readonly ctx: RouterContext) {
    this.pacing = ctx.pacing;
  }

  async handle(raw: string): Promise<CommandResult> {
    if (this.pendingChoices.length && !raw.trim().startsWith('/')) {
      return this.finishResolve(raw.trim());
    }
    const parsed = this.ctx.parser.parse(raw);
    if (parsed.kind === 'empty') {
      return {ok: true, content: ''};
    }
    if (parsed.kind === 'prompt') {
      const ack = await this.ctx.client.submitTask({prompt: parsed.text});
      return {ok: true, content: `task ${ack.id} ${ack.status ?? 'queued'}`};
    }
    if (parsed.kind === 'unknown') {
      const hint = parsed.suggestions.length
        ? ` did you mean ${parsed.suggestions.map((s) => '/' + s).join(', ')}?`
        : '';
      return {ok: false, content: `unknown command /${parsed.name}.${hint}`};
    }
    switch (parsed.name) {
      case 'help':
        return {ok: true, content: this.ctx.parser.help(parsed.args[0])};
      case 'clear':
        return {ok: true, content: '__clear__'};
      case 'exit':
        return {ok: true, content: '__exit__'};
      case 'audit':
        return this.audit(parsed.args);
      case 'sync':
        return this.sync();
      case 'graph':
        return {ok: true, content: this.ctx.client.store.graphText()};
      case 'resolve':
        return this.resolve(parsed.args.join(' '));
      case 'pause':
        return this.setPacing('paused');
      case 'resume':
        return this.setPacing('running');
      case 'snapshots':
        return this.forwardSlash('snapshots', parsed.args);
      case 'restore':
        return this.forwardSlash('restore', parsed.args);
      default:
        return {ok: false, content: `unhandled /${parsed.name}`};
    }
  }

  private async audit(args: string[]): Promise<CommandResult> {
    const query = args.join(' ').trim();
    if (query) {
      try {
        const ack = await this.ctx.client.submitTask({
          prompt: `/audit ${query}`,
          title: '/audit',
          task_type: 'REASONING',
        });
        return {ok: true, content: `queued /audit symbol graph as ${ack.id}`};
      } catch (postErr) {
        return {ok: false, content: `audit failed: ${String(postErr)}`};
      }
    }
    const runner = this.ctx.runIndexer ?? runRustIndexer;
    try {
      const body = await runner(this.ctx.root);
      return {ok: true, content: body || 'Rust indexer finished'};
    } catch (err) {
      try {
        const ack = await this.ctx.client.submitTask({
          prompt: '/audit',
          title: '/audit',
          task_type: 'REASONING',
        });
        return {
          ok: true,
          content: `indexer unavailable (${String(err)}). queued AMACE /audit as ${ack.id}`,
        };
      } catch (postErr) {
        return {ok: false, content: `audit failed: ${String(postErr)}`};
      }
    }
  }

  private async sync(): Promise<CommandResult> {
    const snap = await this.ctx.client.resync();
    return {
      ok: true,
      content: `SSOT resync tasks=${snap.tasks.length} agents=${snap.agents.length} decisions=${snap.decisions.length}`,
    };
  }

  private async resolve(choice: string): Promise<CommandResult> {
    const pending = this.ctx.client.store.pendingConflicts();
    if (choice) {
      this.pendingChoices = pending.map((row) => row.id);
      return this.finishResolve(choice);
    }
    if (!pending.length) {
      const ack = await this.ctx.client.submitTask({
        prompt: '/resolve',
        title: '/resolve',
        task_type: 'REASONING',
      });
      return {ok: true, content: `no local conflicts; queued /resolve as ${ack.id}`};
    }
    this.pendingChoices = pending.map((row) => row.id);
    const options = pending.map(
      (row, i) => `${i + 1}. ${row.choice || row.summary || row.id} (${row.branch ?? 'main'})`,
    );
    return {
      ok: true,
      interactive: true,
      options,
      content: `Pending conflicts:\n${options.join('\n')}\nType a number or choice to apply.`,
    };
  }

  private async finishResolve(choice: string): Promise<CommandResult> {
    const options = this.pendingChoices;
    this.pendingChoices = [];
    const ack = await this.ctx.client.submitTask({
      prompt: `/resolve ${choice}`,
      title: `/resolve ${choice}`.slice(0, 80),
      task_type: 'REASONING',
    });
    return {
      ok: true,
      content: `resolve '${choice}' sent (${ack.id}); ${options.length} local candidate(s)`,
    };
  }

  private async forwardSlash(name: string, args: string[]): Promise<CommandResult> {
    const slash = `/${name}${args.length ? ' ' + args.join(' ') : ''}`.trim();
    try {
      const ack = await this.ctx.client.submitTask({
        prompt: slash,
        title: slash.slice(0, 80),
        task_type: 'FAST_DRAFT',
      });
      return {ok: true, content: `queued ${slash} as ${ack.id}`};
    } catch (err) {
      return {ok: false, content: `${slash} failed: ${String(err)}`};
    }
  }

  private async setPacing(next: PacingState): Promise<CommandResult> {
    this.pacing = next;
    const slash = next === 'paused' ? '/horizon pause' : '/horizon resume';
    try {
      const ack = await this.ctx.client.submitTask({
        prompt: slash,
        title: slash,
        task_type: 'FAST_DRAFT',
      });
      return {
        ok: true,
        pacing: next,
        content: `pacing ${next} (task ${ack.id})`,
      };
    } catch {
      return {
        ok: true,
        pacing: next,
        content: `pacing ${next} locally (gateway task post failed)`,
      };
    }
  }
}

export function findIndexer(root: string): string | null {
  const names =
    process.platform === 'win32'
      ? ['robber-indexer.exe', 'robber-indexer']
      : ['robber-indexer'];
  const dirs = [
    path.join(root, 'indexer', 'target', 'release'),
    path.join(root, 'indexer', 'target', 'debug'),
    path.join(root, '..', 'indexer', 'target', 'release'),
    path.join(root, '..', 'indexer', 'target', 'debug'),
  ];
  for (const dir of dirs) {
    for (const name of names) {
      const full = path.join(dir, name);
      if (existsSync(full)) {
        return full;
      }
    }
  }
  return null;
}

export async function runRustIndexer(root: string): Promise<string> {
  const bin = findIndexer(root);
  if (!bin) {
    throw new Error('robber-indexer binary not built');
  }
  const ssot = path.join(root, '.robber', 'ssot.db');
  return runChild(bin, ['index', '--root', root, '--ssot', ssot], 25_000);
}

function runChild(bin: string, args: string[], timeoutMs: number): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = spawn(bin, args, {windowsHide: true});
    let out = '';
    let err = '';
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error('indexer timed out'));
    }, timeoutMs);
    child.stdout.on('data', (chunk) => {
      out += String(chunk);
      if (out.length > 64 * 1024) {
        out = out.slice(-32 * 1024);
      }
    });
    child.stderr.on('data', (chunk) => {
      err += String(chunk);
    });
    child.on('error', (e) => {
      clearTimeout(timer);
      reject(e);
    });
    child.on('close', (code) => {
      clearTimeout(timer);
      if (code === 0) {
        resolve((out || err).trim());
      } else {
        reject(new Error(err.trim() || `indexer exit ${code}`));
      }
    });
  });
}
