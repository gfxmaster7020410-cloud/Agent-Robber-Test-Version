export const PASTE_START = '\x1b[200~';
export const PASTE_END = '\x1b[201~';
export const BRACKETED_PASTE_ENABLE = '\x1b[?2004h';
export const BRACKETED_PASTE_DISABLE = '\x1b[?2004l';

export const WORD_LIMIT = 10_000;
export const BYTE_LIMIT = 30 * 1024;
export const CHARS_PER_PAGE = 3_000;
export const CHARS_PER_TOKEN = 4;
export const PASTE_REL_PATH = '.agent_robber/tmp_context/paste_buffer.txt';

export interface PasteOffload {
	offloaded: boolean;
	words: number;
	bytes: number;
	pages: number;
	tokens: number;
	path: string;
	marker: string;
	notice: string;
}

export function sanitizePaste(raw: string): string {
	return (raw || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n').replace(/\u0000/g, '');
}

export function wordCount(text: string): number {
	const hits = (text || '').match(/\S+/g);
	return hits ? hits.length : 0;
}

export function pageCount(text: string): number {
	return Math.max(1, Math.ceil(Math.max((text || '').length, 1) / CHARS_PER_PAGE));
}

export function tokenEstimate(text: string): number {
	return Math.max(1, Math.ceil((text || '').length / CHARS_PER_TOKEN));
}

export function isHugePaste(text: string): boolean {
	const clean = sanitizePaste(text);
	if (/\[Context File Attached:\s*paste_buffer\.txt/i.test(clean) && !isRawHuge(clean)) {
		return false;
	}
	return isRawHuge(clean);
}

function isRawHuge(text: string): boolean {
	const bytes = new TextEncoder().encode(text || '').length;
	return wordCount(text) >= WORD_LIMIT || bytes >= BYTE_LIMIT;
}

export function contextMarker(pages: number): string {
	return `[Context File Attached: paste_buffer.txt (${pages} pages)]`;
}

export function ingestNotice(pages: number, tokens: number): string {
	const pageLabel = pages >= 100 ? `${pages}+ pages` : `${pages} pages`;
	return `✦ Initiating: Ingested large paste payload (${pageLabel} / ${tokens.toLocaleString()} tokens) into active memory context.`;
}

export function localOffloadMeta(text: string): PasteOffload {
	const clean = sanitizePaste(text);
	const pages = pageCount(clean);
	const tokens = tokenEstimate(clean);
	return {
		offloaded: true,
		words: wordCount(clean),
		bytes: new TextEncoder().encode(clean).length,
		pages,
		tokens,
		path: PASTE_REL_PATH,
		marker: contextMarker(pages),
		notice: ingestNotice(pages, tokens),
	};
}

export async function offloadPaste(text: string): Promise<PasteOffload | null> {
	const clean = sanitizePaste(text);
	if (!isHugePaste(clean)) {
		return null;
	}
	try {
		const response = await fetch('/api/context/paste', {
			method: 'POST',
			headers: {'Content-Type': 'application/json'},
			body: JSON.stringify({text: clean}),
		});
		if (response.ok) {
			return (await response.json()) as PasteOffload;
		}
	} catch {
		/* daemon may be down — still swap the textarea to a marker */
	}
	return localOffloadMeta(clean);
}

export function insertAtCursor(value: string, chunk: string, start: number, end: number): string {
	const head = value.slice(0, start);
	const tail = value.slice(end);
	return `${head}${chunk}${tail}`;
}
