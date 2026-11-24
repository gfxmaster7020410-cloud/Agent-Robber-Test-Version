import type {NativeCommand, ParsedInput} from './types.ts';

export interface CommandSpec {
  name: NativeCommand;
  aliases: readonly string[];
  description: string;
  usage: string;
  category: string;
}

export const COMMANDS: readonly CommandSpec[] = [
  {
    name: 'audit',
    aliases: ['index', 'reindex'],
    description: 'Precision AST symbol graph via the Rust engine; re-index when no query is given',
    usage: '/audit [query]',
    category: 'System',
  },
  {
    name: 'sync',
    aliases: ['ssot'],
    description: 'Force SSOT snapshot reload and WebSocket resubscribe',
    usage: '/sync',
    category: 'Context',
  },
  {
    name: 'graph',
    aliases: ['dag', 'tree'],
    description: 'Show hypothesis tree and task DAG',
    usage: '/graph',
    category: 'Planning',
  },
  {
    name: 'resolve',
    aliases: ['acrm', 'merge'],
    description: 'Resolve pending merge or decision conflicts',
    usage: '/resolve [choice]',
    category: 'Execution',
  },
  {
    name: 'pause',
    aliases: [],
    description: 'Pause the Python pacing / long-horizon loop',
    usage: '/pause',
    category: 'Execution',
  },
  {
    name: 'resume',
    aliases: [],
    description: 'Resume the Python pacing / long-horizon loop',
    usage: '/resume',
    category: 'Execution',
  },
  {
    name: 'snapshots',
    aliases: ['snapshot'],
    description: 'List point-in-time SSOT and workspace checkpoints',
    usage: '/snapshots',
    category: 'System',
  },
  {
    name: 'restore',
    aliases: [],
    description: 'Restore SSOT and workspace files from a checkpoint',
    usage: '/restore [snapshot_id]',
    category: 'System',
  },
  {
    name: 'help',
    aliases: ['h', '?'],
    description: 'List native slash commands',
    usage: '/help [command]',
    category: 'System',
  },
  {
    name: 'clear',
    aliases: [],
    description: 'Clear the local execution log',
    usage: '/clear',
    category: 'System',
  },
  {
    name: 'exit',
    aliases: ['quit'],
    description: 'Quit the TUI',
    usage: '/exit',
    category: 'System',
  },
];

const ALIAS = new Map<string, NativeCommand>();
for (const spec of COMMANDS) {
  ALIAS.set(spec.name, spec.name);
  for (const alias of spec.aliases) {
    ALIAS.set(alias, spec.name);
  }
}

export class SlashCommandParser {
  parse(raw: string): ParsedInput {
    const text = (raw ?? '').replace(/\r/g, '').trim();
    if (!text) {
      return empty(raw ?? '');
    }
    if (!text.startsWith('/')) {
      return {
        kind: 'prompt',
        text,
        name: '',
        args: [],
        raw: raw ?? '',
        suggestions: [],
      };
    }
    const body = text.slice(1).trim();
    const parts = body.split(/\s+/).filter(Boolean);
    let token = (parts[0] ?? '').toLowerCase();
    let args = parts.slice(1);

    if (token === 'horizon' && args[0]) {
      const action = args[0].toLowerCase();
      if (action === 'pause' || action === 'resume') {
        token = action;
        args = args.slice(1);
      }
    }

    const mapped = ALIAS.get(token);
    if (mapped) {
      return {
        kind: 'command',
        text,
        name: mapped,
        args,
        raw: raw ?? '',
        suggestions: [],
      };
    }
    return {
      kind: 'unknown',
      text,
      name: token,
      args,
      raw: raw ?? '',
      suggestions: this.suggest(token),
    };
  }

  suggest(query: string, limit = 5): string[] {
    return fuzzyCommands(query, limit).map((row) => row.name);
  }

  help(name?: string): string {
    if (name) {
      const key = ALIAS.get(name.replace(/^\//, '').toLowerCase());
      const spec = COMMANDS.find((c) => c.name === key);
      if (!spec) {
        return `unknown command /${name.replace(/^\//, '')}`;
      }
      return `${spec.usage}\n${spec.description}`;
    }
    const lines = ['Native commands:'];
    for (const spec of COMMANDS) {
      lines.push(`  ${spec.usage.padEnd(22)} ${spec.description}`);
    }
    lines.push('Other input is sent as an agent task on POST /api/v1/agent/task.');
    return lines.join('\n');
  }
}

export function parseSlash(raw: string): ParsedInput {
  return new SlashCommandParser().parse(raw);
}

function empty(raw: string): ParsedInput {
  return {kind: 'empty', text: '', name: '', args: [], raw, suggestions: []};
}

function subsequence(needle: string, hay: string): boolean {
  let index = 0;
  for (const char of needle) {
    index = hay.indexOf(char, index);
    if (index < 0) {
      return false;
    }
    index += 1;
  }
  return true;
}

export function fuzzyCommands(query: string, limit = 5): CommandSpec[] {
  const needle = query.trim().replace(/^\//, '').split(/\s+/)[0]?.toLowerCase() ?? '';
  const scored: {item: CommandSpec; score: number}[] = [];
  for (const item of COMMANDS) {
    const haystacks = [item.name, ...item.aliases, item.description.toLowerCase()];
    let best = needle ? 0 : 1;
    for (const hay of haystacks) {
      const text = hay.toLowerCase();
      if (!needle) {
        break;
      }
      if (text === needle) {
        best = 200;
      } else if (text.startsWith(needle)) {
        best = Math.max(best, 100 - (text.length - needle.length));
      } else if (text.includes(needle)) {
        best = Math.max(best, 70 - text.indexOf(needle));
      } else if (subsequence(needle, text)) {
        best = Math.max(best, 40);
      }
    }
    if (best > 0) {
      scored.push({item, score: best});
    }
  }
  scored.sort((a, b) => b.score - a.score || a.item.name.localeCompare(b.item.name));
  return scored.slice(0, Math.max(1, limit)).map((row) => row.item);
}
