import type {ChatMessage, FileCardPayload, FileMutation} from '../lib/types';
import {stripThoughts} from '../lib/thoughts';

export type LayoutMode = 'standard' | 'wide' | 'diff';
export type StepKind = 'initiating' | 'writing' | 'terminal' | 'error' | 'output';

export interface StreamStep {
	id: string;
	kind: StepKind;
	label: string;
	title: string;
	body?: string;
	file?: FileCardPayload;
	error?: boolean;
	ansi?: ChatMessage['ansi'];
}

const WRITE_RE = /\b(write_file|edit_file|str_replace|create_file|Wrote to|Edit~to|wrote)\b/i;
const SHELL_RE = /\b(execute_bash|run_command|npm |node |python |pytest|shell)\b/i;

export function parseFileCard(content: string): FileCardPayload | null {
	const text = (content || '').trim();
	if (!text) {
		return null;
	}
	const candidates = [text];
	const fenced = text.match(/```(?:json)?\s*([\s\S]*?)```/);
	if (fenced?.[1]) {
		candidates.push(fenced[1].trim());
	}
	if (!text.startsWith('{')) {
		const start = text.indexOf('{');
		const end = text.lastIndexOf('}');
		if (start >= 0 && end > start) {
			candidates.push(text.slice(start, end + 1));
		}
	}
	for (const candidate of candidates) {
		if (!candidate.startsWith('{')) {
			continue;
		}
		try {
			const row = JSON.parse(candidate) as FileCardPayload;
			if (row && typeof row.path === 'string') {
				return row;
			}
		} catch {
			/* try next */
		}
	}
	return null;
}

export function languageFor(path: string, fallback = 'plaintext'): string {
	const suffix = (path.split('.').pop() || '').toLowerCase();
	const map: Record<string, string> = {
		py: 'python',
		ts: 'typescript',
		tsx: 'typescript',
		js: 'javascript',
		jsx: 'javascript',
		json: 'json',
		md: 'markdown',
		css: 'css',
		html: 'html',
		htm: 'html',
		rs: 'rust',
		go: 'go',
		sql: 'sql',
	};
	return map[suffix] || fallback;
}

export function toStreamSteps(messages: ChatMessage[]): StreamStep[] {
	const steps: StreamStep[] = [];
	for (const message of messages) {
		if (message.role === 'reasoning') {
			continue;
		}
		const content = stripThoughts(message.content);
		if (!content.trim() && message.role === 'assistant') {
			continue;
		}
		if (message.role === 'system' && (/^\s*\[LOOP\]/i.test(message.content) || /Control Center attached/i.test(message.content))) {
			continue;
		}

		if (message.role === 'file') {
			const file = parseFileCard(message.content);
			if (file) {
				const editing = file.action === 'edit' || file.action === 'delete';
				steps.push({
					id: `f-${message.id}`,
					kind: 'writing',
					label: 'Writing',
					title: editing ? `Edit~to ${file.path}` : `Wrote to ${file.path}:`,
					file: {...file, language: file.language || languageFor(file.path)},
				});
				continue;
			}
		}

		if (message.streamKind) {
			const mapped =
				message.streamKind === 'error'
					? {kind: 'error' as const, label: 'return error!', error: true}
					: message.streamKind === 'terminal'
						? {kind: 'terminal' as const, label: 'Terminal', error: false}
						: message.streamKind === 'writing'
							? {kind: 'writing' as const, label: 'Writing', error: false}
							: {kind: 'initiating' as const, label: 'Initiating', error: false};
			steps.push({
				id: `k-${message.id}`,
				kind: mapped.kind,
				label: mapped.label,
				title: firstLine(content),
				body: content,
				error: mapped.error,
				ansi: message.ansi,
			});
			continue;
		}

		if (message.role === 'user') {
			steps.push({
				id: `u-${message.id}`,
				kind: 'initiating',
				label: 'Initiating',
				title: firstLine(message.content),
				body: message.content,
				ansi: message.ansi,
			});
			continue;
		}

		const file = parseFileCard(message.content);
		if (file) {
			const editing = file.action === 'edit' || file.action === 'delete';
			steps.push({
				id: `f-${message.id}`,
				kind: 'writing',
				label: 'Writing',
				title: editing ? `Edit~to ${file.path}` : `Wrote to ${file.path}:`,
				file: {...file, language: file.language || languageFor(file.path)},
			});
			continue;
		}

		if (message.role === 'error') {
			steps.push({
				id: `e-${message.id}`,
				kind: 'error',
				label: 'return error!',
				title: firstLine(message.content),
				body: message.content,
				error: true,
				ansi: message.ansi,
			});
			continue;
		}

		if (message.role === 'tool' || WRITE_RE.test(message.content)) {
			const shell = SHELL_RE.test(message.content);
			steps.push({
				id: `k-${message.id}`,
				kind: shell ? 'terminal' : 'writing',
				label: shell ? 'Terminal' : 'Writing',
				title: firstLine(message.content),
				body: message.content,
				ansi: message.ansi,
			});
			continue;
		}

		if (message.role === 'assistant' || message.role === 'success') {
			steps.push({
				id: `a-${message.id}`,
				kind: 'output',
				label: 'Writing',
				title: firstLine(content),
				body: content,
				ansi: message.ansi,
			});
			continue;
		}

		steps.push({
			id: `s-${message.id}`,
			kind: 'initiating',
			label: 'Initiating',
			title: firstLine(message.content),
			body: message.content,
			ansi: message.ansi,
		});
	}
	return steps;
}

function firstLine(text: string): string {
	return text.split('\n')[0] ?? text;
}

export function collectFileHints(messages: ChatMessage[]): string[] {
	const found = new Set<string>();
	for (const message of messages) {
		const card = parseFileCard(message.content);
		if (card?.path) {
			found.add(card.path);
		}
		for (const hit of message.content.match(/[\w./\\-]+\.[A-Za-z][A-Za-z0-9]+/g) ?? []) {
			if (hit.length < 80) {
				found.add(hit.replace(/\\/g, '/'));
			}
		}
	}
	return [...found].slice(0, 24);
}

export function formatElapsed(ms: number): string {
	const n = Math.max(0, Math.floor(ms));
	const minutes = Math.floor(n / 60_000);
	const rest = n % 60_000;
	return minutes > 0 ? `${minutes}m ${rest}ms` : `${rest}ms`;
}

export function sessionId(): string {
	try {
		const key = 'robber-session-id';
		let value = window.sessionStorage.getItem(key);
		if (!value) {
			value = `random${Math.random().toString(36).slice(2, 8)}`;
			window.sessionStorage.setItem(key, value);
		}
		return value;
	} catch {
		return 'randomtnsrot';
	}
}

export function inferLayout(steps: StreamStep[]): LayoutMode {
	return steps.some(step => step.file && step.file.action !== 'create') ? 'diff' : 'standard';
}

export type {FileMutation};

export function collectMutations(messages: ChatMessage[]): FileMutation[] {
	const byPath = new Map<string, FileMutation>();
	for (const message of messages) {
		const card = parseFileCard(message.content);
		if (card?.path) {
			byPath.set(card.path, mutationFromCard(card));
			continue;
		}
		const wrote = message.content.match(/\b(?:Wrote to|Edit~to|created|modified)\s+([^\s:]+)/i);
		if (wrote?.[1]) {
			const path = wrote[1].replace(/:$/, '');
			if (!byPath.has(path)) {
				const created = /wrote to|created/i.test(message.content);
				byPath.set(path, {
					path,
					marker: created ? '+' : '~',
					detail: created ? '(Created)' : '(Modified)',
				});
			}
		}
	}
	return [...byPath.values()];
}

function mutationFromCard(card: FileCardPayload): FileMutation {
	const afterLines = Math.max(1, (card.after || '').split('\n').length);
	const beforeLines = (card.before || '').split('\n').filter(Boolean).length;
	if (card.action === 'create') {
		return {path: card.path, marker: '+', detail: `+${afterLines} lines (Created)`};
	}
	if (card.action === 'delete') {
		return {path: card.path, marker: '-', detail: 'deleted'};
	}
	const added = Math.max(0, afterLines - beforeLines);
	const removed = Math.max(0, beforeLines - afterLines);
	if (removed > 0) {
		return {
			path: card.path,
			marker: '~',
			detail: `+${added || afterLines}, -${removed}   (Modified)`,
		};
	}
	return {path: card.path, marker: '~', detail: `+${added || afterLines} lines (Modified)`};
}
