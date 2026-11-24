export type SlashCategory = 'System' | 'Execution' | 'Context' | 'Planning' | 'Workspace';

export interface SlashCommandInfo {
	name: string;
	aliases: string[];
	description: string;
	category: SlashCategory;
	usage: string;
	local?: boolean;
}

export const SLASH_COMMANDS: readonly SlashCommandInfo[] = [
	{name: 'help', aliases: ['h', '?'], description: 'List slash commands by category', category: 'System', usage: '/help [command]', local: true},
	{name: 'status', aliases: [], description: 'Show daemon link, mode, model, SSOT window', category: 'System', usage: '/status', local: true},
	{name: 'clear', aliases: [], description: 'Clear this conversation', category: 'System', usage: '/clear', local: true},
	{name: 'canvas', aliases: [], description: 'Toggle the reasoning canvas pane', category: 'System', usage: '/canvas', local: true},
	{name: 'exit', aliases: ['quit'], description: 'Quit Agent Robber', category: 'System', usage: '/exit', local: true},
	{name: 'keys', aliases: [], description: 'BYOK provider status (no secret values)', category: 'System', usage: '/keys'},
	{name: 'purge', aliases: [], description: 'Kill agent-spawned subprocesses', category: 'System', usage: '/purge [pid]'},
	{name: 'theme', aliases: [], description: 'Switch dark | light', category: 'System', usage: '/theme [dark|light|toggle]'},
	{name: 'run', aliases: [], description: 'Start workspace dev server with auto-port detection', category: 'System', usage: '/run [port|script]'},
	{name: 'term', aliases: ['sh', 'shell'], description: 'Run a command in the persistent PTY/ConPTY shell', category: 'Execution', usage: '/term <command>'},
	{name: 'stdin', aliases: [], description: 'Write a line to the persistent terminal for prompts and python -i', category: 'Execution', usage: '/stdin <text>'},
	{name: 'policy', aliases: [], description: 'Command auto-approval policy engine status and dynamic rules', category: 'System', usage: '/policy [status|trust <prefix>|reset]'},
	{name: 'approve', aliases: [], description: 'Approve a pending Tier-2 shell command confirmation request', category: 'System', usage: '/approve [request_id]'},
	{name: 'deny', aliases: [], description: 'Deny a pending Tier-2 shell command confirmation request', category: 'System', usage: '/deny [request_id]'},
	{name: 'notify', aliases: [], description: 'Desktop notification now, or after the next long turn', category: 'System', usage: '/notify [on|off|message]'},
	{name: 'sound-switch', aliases: ['sound'], description: 'Toggle audio cues for build / success / error', category: 'System', usage: '/sound-switch [on|off]'},
	{name: 'fix-dependencies', aliases: ['dep', 'deps'], description: 'Install missing packages from package.json / requirements.txt', category: 'System', usage: '/fix-dependencies'},
	{name: 'structure', aliases: [], description: 'Clean logs, ensure exports/plans, lock/unlock sector dirs', category: 'Workspace', usage: '/structure [status|clean|organize|lock|unlock]'},
	{name: 'compact', aliases: [], description: 'Compress session memory while keeping SSOT state', category: 'Context', usage: '/compact'},
	{name: 'save-session', aliases: ['save'], description: 'Export memory notes and last output to disk', category: 'Context', usage: '/save-session [label]'},
	{name: 'save-output', aliases: [], description: 'Write last AI response to ./exports', category: 'Context', usage: '/save-output [filename]'},
	{name: 'read-research', aliases: ['research'], description: 'Inject local research docs into the next turn', category: 'Context', usage: '/read-research [path]'},
	{name: 'btw', aliases: [], description: 'Side question that does not pollute main history', category: 'Context', usage: '/btw <question>'},
	{name: 'look-deep', aliases: ['deep'], description: 'Multi-pass AST scan of the workspace', category: 'Context', usage: '/look-deep [path]'},
	{name: 'memory', aliases: [], description: 'Query or reindex the SSOT index', category: 'Context', usage: '/memory [query|reindex|watch]'},
	{name: 'window', aliases: ['context'], description: 'Show or set the SSOT context window', category: 'Context', usage: '/window [1t|2m|...]'},
	{name: 'plugin', aliases: ['plugins'], description: 'List, reload, enable, or disable third-party tools', category: 'System', usage: '/plugin [list|reload|enable <name>|disable <name>|run <name>]'},
	{name: 'heal', aliases: [], description: 'Release stale file locks and recover execution deadlocks', category: 'System', usage: '/heal'},
	{name: 'audit', aliases: [], description: 'AMACE scan, or precision AST symbol graph for a query', category: 'System', usage: '/audit [symbol query]'},
	{name: 'snapshots', aliases: ['snapshot'], description: 'List point-in-time SSOT and workspace checkpoints', category: 'System', usage: '/snapshots'},
	{name: 'restore', aliases: [], description: 'Restore SSOT and snapshotted files from a long-horizon checkpoint', category: 'System', usage: '/restore [snapshot_id]'},
	{name: 'local', aliases: ['ollama', 'offline'], description: 'Scan localhost Ollama / LM Studio / vLLM and register zero-cost fallbacks', category: 'Execution', usage: '/local [status|scan]'},
	{name: 'sync', aliases: [], description: 'Atomic SSOT re-index using memory-mapped chunk streaming', category: 'Context', usage: '/sync'},
	{name: 'handoff', aliases: [], description: 'Dump unconsumed inter-model handoff payloads for a specialist', category: 'Context', usage: '/handoff [target_model]'},
	{name: 'graph', aliases: [], description: 'Visualize hypothesis evolution tree and active revision branches', category: 'Planning', usage: '/graph'},
	{name: 'loop', aliases: ['telemetry'], description: 'Live execution-loop telemetry: tokens, micro-locks, state-graph lineage', category: 'System', usage: '/loop'},
	{name: 'horizon', aliases: ['pace', 'long-horizon'], description: 'Long-horizon research: micro-batch, checkpoint, pause, resume', category: 'Execution', usage: '/horizon [path|status|pause|resume]'},
	{name: 'resolve', aliases: [], description: 'Run ACRM on recent parallel specialist outputs', category: 'Execution', usage: '/resolve'},
	{name: 'mode', aliases: ['m'], description: 'Set harness: build | code | terminal | plan | autonomous', category: 'Execution', usage: '/mode <build|code|terminal|plan|autonomous>'},
	{name: 'model', aliases: [], description: 'Route the next prompt to a specific configured model', category: 'Execution', usage: '/model <label>'},
	{name: 'discover', aliases: ['models-scan'], description: 'Auto-discover models from provider /v1/models and refresh the failover pool', category: 'Execution', usage: '/discover [refresh|disable <id>|enable <id>|pin <id>|unpin <id>]'},
	{name: 'mcps', aliases: ['mcp'], description: 'MCP server manager — list / enable / disable', category: 'Execution', usage: '/mcps [list|enable <id>|disable <id>]'},
	{name: 'logic-check', aliases: ['logic'], description: 'Verification pass for algorithm correctness', category: 'Execution', usage: '/logic-check [focus]'},
	{name: 'reasoning', aliases: ['think', 'cot'], description: 'Force high-reasoning routing (Best tier / CoT)', category: 'Execution', usage: '/reasoning [on|off]'},
	{name: 'design-plan', aliases: ['design'], description: 'Generate architecture and interface specs', category: 'Planning', usage: '/design-plan [topic]'},
	{name: 'spec', aliases: ['sdd'], description: 'Store OpenAPI, SQL DDL, and DIP contracts in project_state before code', category: 'Planning', usage: '/spec [goal]'},
	{name: 'profile', aliases: ['profiler'], description: 'Profile sandboxed tests and flag nested-loop bottlenecks', category: 'Execution', usage: '/profile [path]'},
	{name: 'prototype', aliases: [], description: 'Fast-track a lightweight prototype from the active plan', category: 'Planning', usage: '/prototype [note]'},
	{name: 'save-plan', aliases: [], description: 'Export the last plan canvas to ./plans', category: 'Planning', usage: '/save-plan'},
	{name: 'output-analyze-plan', aliases: ['analyze-plan'], description: 'Turn last tool output into an implementation plan', category: 'Planning', usage: '/output-analyze-plan'},
	{name: 'ask-questions', aliases: ['ask'], description: 'Clarify ambiguous requirements before coding', category: 'Planning', usage: '/ask-questions [topic]'},
	{name: 'prepare-next-input', aliases: ['next'], description: 'Draft an optimized follow-up prompt', category: 'Planning', usage: '/prepare-next-input'},
	{name: 'test', aliases: [], description: 'Discover and run pytest / npm test / vitest', category: 'Planning', usage: '/test [pytest|npm|vitest]'},
	{name: 'undo', aliases: [], description: 'Undo the last jailed file mutation', category: 'Workspace', usage: '/undo'},
	{name: 'redo', aliases: [], description: 'Redo the last undone file mutation', category: 'Workspace', usage: '/redo'},
	{name: 'accept', aliases: ['accept-all'], description: 'Accept all pending file mutations', category: 'Workspace', usage: '/accept'},
	{name: 'revert', aliases: ['revert-all'], description: 'Revert all session file mutations to their originals', category: 'Workspace', usage: '/revert'},
];

export const LOCAL_SLASH = new Set(['help', 'status', 'clear', 'canvas', 'exit', 'quit']);

export function completeSlashLine(value: string, name: string): string {
	const body = value.startsWith('/') ? value.slice(1) : value;
	const space = body.indexOf(' ');
	const rest = space >= 0 ? body.slice(space + 1).trim() : '';
	return rest ? `/${name} ${rest}` : `/${name}`;
}

export function slashMenuOpen(value: string, dismissed: boolean): boolean {
	if (dismissed || !value.startsWith('/')) {
		return false;
	}
	return !value.slice(1).includes(' ');
}

export function parseSlash(raw: string): {name: string; args: string[]; token: string} {
	const trimmed = raw.trim();
	const body = trimmed.startsWith('/') ? trimmed.slice(1) : trimmed;
	const parts = body.split(/\s+/).filter(Boolean);
	return {
		name: (parts[0] ?? '').toLowerCase(),
		args: parts.slice(1),
		token: (parts[0] ?? '').toLowerCase(),
	};
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

export function fuzzyCommands(query: string, catalog: readonly SlashCommandInfo[] = SLASH_COMMANDS, limit = 8): SlashCommandInfo[] {
	const needle = query.trim().replace(/^\//, '').split(/\s+/)[0]?.toLowerCase() ?? '';
	const scored: {item: SlashCommandInfo; score: number}[] = [];
	for (const item of catalog) {
		const haystacks = [item.name, ...item.aliases, item.description.toLowerCase(), item.category.toLowerCase()];
		let best = needle ? 0 : 1;
		for (const hay of haystacks) {
			const text = hay.toLowerCase();
			if (!needle) {
				break;
			}
			if (text.startsWith(needle)) {
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
	return scored.slice(0, Math.max(1, limit)).map(row => row.item);
}

export function formatHelp(catalog: readonly SlashCommandInfo[] = SLASH_COMMANDS): string {
	const lines = [
		'Commands:',
		'  /clear     Clear the conversation history',
		'  /canvas    Toggle the reasoning canvas pane',
		'  /keys      Show BYOK provider status (no secret values)',
		'  /purge     Kill agent-spawned subprocesses',
		'  /mode      Switch plan | act | review, or autonomous | manual',
		'  /approve   Apply a pending self-heal patch (manual control)',
		'  /reject    Dismiss a pending self-heal patch',
		'  /memory    Query the SSOT index (/memory reindex, /memory watch, /window 1t)',
		'  /mcp       List / enable / disable / call local MCP tools',
		'  /theme     Switch dark | light (synced with the GUI)',
		'  /status    Show the daemon connection details',
		'  /window    Show or set the SSOT context window',
		'  /exit      Quit Agent Robber',
		'',
		'Type / for autocomplete. Additional commands:',
	];
	let current = '';
	for (const item of catalog) {
		if (item.local && ['clear', 'canvas', 'exit', 'help', 'status'].includes(item.name)) {
			continue;
		}
		if (item.category !== current) {
			current = item.category;
			lines.push(`  [${current}]`);
		}
		lines.push(`  /${item.name.padEnd(22)} ${item.description}`);
	}
	return lines.join('\n');
}

export function categoryColor(category: SlashCategory): string {
	if (category === 'System') {
		return 'cyan';
	}
	if (category === 'Execution') {
		return 'yellow';
	}
	if (category === 'Context') {
		return 'green';
	}
	if (category === 'Planning') {
		return 'magenta';
	}
	return 'blue';
}
