export const THINK_BLOCK =
	/<(think|thinking|thought|reasoning)\b[^>]*>[\s\S]*?<\/\1\s*>/gi;
export const THINK_FENCE = /```(?:think|thinking|thought|reasoning)\s*\n[\s\S]*?```/gi;
export const THINK_RED = /◁think▷[\s\S]*?◁\/think▷/gi;
export const THINK_OPEN = /<(think|thinking|thought|reasoning)\b[^>]*>/i;

export function stripThoughts(text: string): string {
	let cleaned = text || '';
	cleaned = cleaned.replace(THINK_BLOCK, '');
	cleaned = cleaned.replace(THINK_FENCE, '');
	cleaned = cleaned.replace(THINK_RED, '');
	const open = cleaned.search(THINK_OPEN);
	const red = cleaned.toLowerCase().indexOf('◁think▷');
	const cuts = [open, red].filter(index => index >= 0);
	if (cuts.length > 0) {
		cleaned = cleaned.slice(0, Math.min(...cuts));
	}
	return cleaned.replace(/\n{3,}/g, '\n\n').replace(/^\n+|\n+$/g, '');
}
