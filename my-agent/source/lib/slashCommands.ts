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
	{name: 'mode', aliases: ['m'], description: 'Set harness: build | code | terminal | plan | autonomous', category: 'Execution', usage: '/mode <build|code|terminal|plan|autonomous>'},
	{name: 'model', aliases: [], description: 'Route the next prompt to a specific configured model', category: 'Execution', usage: '/model <label>'},
	{name: 'mcps', aliases: ['mcp'], description: 'MCP server manager — list / enable / disable', category: 'Execution', usage: '/mcps [list|enable <id>|disable <id>]'},
	{name: 'logic-check', aliases: ['logic'], description: 'Verification pass for algorithm correctness', category: 'Execution', usage: '/logic-check [focus]'},
	{name: 'reasoning', aliases: ['think', 'cot'], description: 'Force high-reasoning routing (Best tier / CoT)', category: 'Execution', usage: '/reasoning [on|off]'},
	{name: 'design-plan', aliases: ['design'], description: 'Generate architecture and interface specs', category: 'Planning', usage: '/design-plan [topic]'},
	{name: 'prototype', aliases: [], description: 'Fast-track a lightweight prototype from the active plan', category: 'Planning', usage: '/prototype [note]'},
	{name: 'save-plan', aliases: [], description: 'Export the last plan canvas to ./plans', category: 'Planning', usage: '/save-plan'},
	{name: 'output-analyze-plan', aliases: ['analyze-plan'], description: 'Turn last tool output into an implementation plan', category: 'Planning', usage: '/output-analyze-plan'},
	{name: 'ask-questions', aliases: ['ask'], description: 'Clarify ambiguous requirements before coding', category: 'Planning', usage: '/ask-questions [topic]'},
	{name: 'prepare-next-input', aliases: ['next'], description: 'Draft an optimized follow-up prompt', category: 'Planning', usage: '/prepare-next-input'},
	{name: 'test', aliases: [], description: 'Discover and run pytest / npm test / vitest', category: 'Planning', usage: '/test [pytest|npm|vitest]'},
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

export function fuzzyCommands(query: string, catalog: readonly SlashCommandInfo[] = SLASH_COMMANDS, limit = 6): SlashCommandInfo[] {
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
	lines.push('');
	lines.push('Anything else is sent to the daemon, which runs the turn and streams back.');
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
