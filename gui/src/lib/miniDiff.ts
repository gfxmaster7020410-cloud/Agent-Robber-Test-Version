import type {FileCardPayload} from './types';

export type DiffLineKind = 'add' | 'del' | 'ctx' | 'meta';

export interface DiffPreviewLine {
	kind: DiffLineKind;
	text: string;
}

export interface MiniDiff {
	path: string;
	added: number;
	removed: number;
	preview: DiffPreviewLine[];
	full: string;
	language: string;
	marker: '+' | '~' | '-';
	action: 'create' | 'edit' | 'delete' | 'diff';
}

export const PREVIEW_LIMIT = 4;

const META_LINE = /^(diff |index |--- |\+\+\+ |@@ )/;
const WRITE_PATH = /\b(?:Wrote to|Edit~to|Deleted|created|modified)\s+([^\s:]+)(?::)?/i;
const EXT_LANG: Record<string, string> = {
	py: 'python',
	ts: 'typescript',
	tsx: 'tsx',
	js: 'javascript',
	jsx: 'jsx',
	mjs: 'javascript',
	json: 'json',
	css: 'css',
	html: 'markup',
	htm: 'markup',
	md: 'markdown',
	diff: 'diff',
	patch: 'diff',
	sh: 'bash',
	bash: 'bash',
	rs: 'rust',
	go: 'go',
	toml: 'toml',
	yaml: 'yaml',
	yml: 'yaml',
};

export function parseFenceHeader(raw: string): {language?: string; path?: string} {
	const meta = (raw || '').trim();
	if (!meta) {
		return {};
	}
	const pathEq = meta.match(/(?:^|\s)path=(\S+)/i);
	const tokens = meta.split(/\s+/);
	const first = tokens[0] ?? '';
	let language = first;
	let path: string | undefined;
	if (first.includes(':') && !/^https?:/i.test(first) && !/^[A-Za-z]:[\\/]/.test(first)) {
		const split = first.indexOf(':');
		language = first.slice(0, split) || first;
		const rest = first.slice(split + 1);
		if (rest) {
			path = rest;
		}
	}
	if (pathEq?.[1]) {
		path = pathEq[1];
	}
	const leftover = tokens
		.slice(1)
		.filter(token => !/^path=/i.test(token))
		.find(token => /[./\\]/.test(token));
	if (!path && leftover) {
		path = leftover;
	}
	if (language === 'diff' || language === 'patch' || language === 'text' || language === 'txt') {
		return {language: language === 'patch' ? 'diff' : language, path};
	}
	return {language: language || undefined, path};
}

export function languageFromPath(path: string, fallback = 'text'): string {
	const suffix = (path.split('.').pop() || '').toLowerCase();
	return EXT_LANG[suffix] || fallback;
}

export function looksLikeUnifiedDiff(text: string): boolean {
	const raw = text || '';
	return /^(diff --git |index |--- a\/|\+\+\+ b\/|@@ )/m.test(raw);
}

export function looksLikeFileDiff(code: string, language?: string, path?: string): boolean {
	const lang = (language || '').toLowerCase();
	if (lang === 'diff' || lang === 'patch') {
		return true;
	}
	if (path) {
		return true;
	}
	return looksLikeUnifiedDiff(code);
}

export function writePathFromText(text: string): string | undefined {
	const match = (text || '').match(WRITE_PATH);
	return match?.[1];
}

export function countDiffStats(text: string): {added: number; removed: number} {
	let added = 0;
	let removed = 0;
	for (const line of (text || '').split('\n')) {
		if (line.startsWith('+++') || line.startsWith('---')) {
			continue;
		}
		if (line.startsWith('+')) {
			added += 1;
		} else if (line.startsWith('-')) {
			removed += 1;
		}
	}
	return {added, removed};
}

export function pathFromDiff(text: string): string | undefined {
	const plus = (text || '').match(/^\+\+\+ (?:[ab]\/)?(.+)$/m);
	if (plus?.[1] && plus[1] !== '/dev/null') {
		return plus[1].trim();
	}
	const minus = (text || '').match(/^--- (?:[ab]\/)?(.+)$/m);
	if (minus?.[1] && minus[1] !== '/dev/null') {
		return minus[1].trim();
	}
	const git = (text || '').match(/^diff --git a\/(.+) b\/(.+)$/m);
	return git?.[2];
}

export function previewLines(text: string, limit = PREVIEW_LIMIT): DiffPreviewLine[] {
	const out: DiffPreviewLine[] = [];
	for (const line of (text || '').split('\n')) {
		if (!line || META_LINE.test(line)) {
			continue;
		}
		if (line.startsWith('+')) {
			out.push({kind: 'add', text: line});
		} else if (line.startsWith('-')) {
			out.push({kind: 'del', text: line});
		} else {
			continue;
		}
		if (out.length >= limit) {
			break;
		}
	}
	if (out.length === 0) {
		for (const line of (text || '').split('\n')) {
			if (!line.trim() || META_LINE.test(line)) {
				continue;
			}
			out.push({kind: 'ctx', text: line});
			if (out.length >= Math.min(3, limit)) {
				break;
			}
		}
	}
	return out;
}

export function markerFor(added: number, removed: number, action?: string): '+' | '~' | '-' {
	if (action === 'delete' || (removed > 0 && added === 0)) {
		return '-';
	}
	if (action === 'create' || (added > 0 && removed === 0)) {
		return '+';
	}
	return '~';
}

function bagStats(before: string, after: string): {added: number; removed: number} {
	const beforeBag = new Map<string, number>();
	for (const line of before.split('\n')) {
		beforeBag.set(line, (beforeBag.get(line) || 0) + 1);
	}
	let added = 0;
	for (const line of after.split('\n')) {
		const have = beforeBag.get(line) || 0;
		if (have > 0) {
			beforeBag.set(line, have - 1);
		} else {
			added += 1;
		}
	}
	let removed = 0;
	for (const leftover of beforeBag.values()) {
		removed += leftover;
	}
	return {added, removed};
}

function prefixedPreview(lines: string[], kind: 'add' | 'del', mark: '+' | '-'): DiffPreviewLine[] {
	return lines
		.filter(line => line.length > 0)
		.slice(0, PREVIEW_LIMIT)
		.map(line => ({
			kind,
			text: line.startsWith(mark) ? line : `${mark}${line}`,
		}));
}

export function miniDiffFromUnified(
	code: string,
	opts?: {language?: string; path?: string},
): MiniDiff {
	const stats = countDiffStats(code);
	const path = opts?.path || pathFromDiff(code) || 'file';
	const langHint = (opts?.language || '').toLowerCase();
	const language =
		langHint && langHint !== 'diff' && langHint !== 'patch' && langHint !== 'text'
			? langHint
			: languageFromPath(path, 'diff');
	const action: MiniDiff['action'] =
		stats.removed > 0 && stats.added > 0 ? 'diff' : stats.removed > 0 ? 'delete' : 'create';
	return {
		path,
		added: stats.added,
		removed: stats.removed,
		preview: previewLines(code),
		full: code,
		language,
		marker: markerFor(stats.added, stats.removed, action),
		action,
	};
}

export function miniDiffFromFile(file: FileCardPayload): MiniDiff {
	const after = file.after || '';
	const before = file.before || '';
	if (looksLikeUnifiedDiff(after) || looksLikeUnifiedDiff(before)) {
		return miniDiffFromUnified(after || before, {language: file.language, path: file.path});
	}
	const action = (file.action || 'edit') as MiniDiff['action'];
	if (action === 'delete') {
		const removed = Math.max(1, before.split('\n').length);
		return {
			path: file.path,
			added: 0,
			removed,
			preview: prefixedPreview(before.split('\n'), 'del', '-'),
			full: before,
			language: file.language || languageFromPath(file.path),
			marker: '-',
			action: 'delete',
		};
	}
	if (action === 'create' || !before) {
		const added = Math.max(1, after.split('\n').length);
		return {
			path: file.path,
			added,
			removed: 0,
			preview: prefixedPreview(after.split('\n'), 'add', '+'),
			full: after,
			language: file.language || languageFromPath(file.path),
			marker: '+',
			action: 'create',
		};
	}
	const stats = bagStats(before, after);
	const dels = prefixedPreview(before.split('\n'), 'del', '-').slice(0, 2);
	const adds = prefixedPreview(after.split('\n'), 'add', '+').slice(0, 2);
	return {
		path: file.path,
		added: stats.added,
		removed: stats.removed,
		preview: [...dels, ...adds].slice(0, PREVIEW_LIMIT),
		full: after,
		language: file.language || languageFromPath(file.path),
		marker: markerFor(stats.added, stats.removed, 'edit'),
		action: 'edit',
	};
}

export function miniDiffFromCode(code: string, language?: string, path?: string): MiniDiff | null {
	if (!looksLikeFileDiff(code, language, path)) {
		return null;
	}
	if (looksLikeUnifiedDiff(code) || (language || '').toLowerCase() === 'diff' || (language || '').toLowerCase() === 'patch') {
		return miniDiffFromUnified(code, {language, path});
	}
	const lines = (code || '').split('\n');
	return {
		path: path || 'file',
		added: Math.max(1, lines.length),
		removed: 0,
		preview: prefixedPreview(lines, 'add', '+'),
		full: code,
		language: language || languageFromPath(path || ''),
		marker: '+',
		action: 'create',
	};
}
