export const PASTE_START = '\x1b[200~';
export const PASTE_END = '\x1b[201~';
export const BRACKETED_PASTE_ENABLE = '\x1b[?2004h';
export const BRACKETED_PASTE_DISABLE = '\x1b[?2004l';

export const WORD_LIMIT = 10_000;
export const BYTE_LIMIT = 30 * 1024;
export const CHARS_PER_PAGE = 3_000;
export const CHARS_PER_TOKEN = 4;
export const PASTE_REL_PATH = '.agent_robber/tmp_context/paste_buffer.txt';

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
	if (/\[Context File Attached:\s*paste_buffer\.txt/i.test(clean)) {
		return false;
	}
	const bytes = Buffer.byteLength(clean, 'utf8');
	return wordCount(clean) >= WORD_LIMIT || bytes >= BYTE_LIMIT;
}

export function contextMarker(pages: number): string {
	return `[Context File Attached: paste_buffer.txt (${pages} pages)]`;
}

export function ingestNotice(pages: number, tokens: number): string {
	const pageLabel = pages >= 100 ? `${pages}+ pages` : `${pages} pages`;
	return `✦ Initiating: Ingested large paste payload (${pageLabel} / ${tokens.toLocaleString()} tokens) into active memory context.`;
}
