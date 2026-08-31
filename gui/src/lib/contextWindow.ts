export const DEFAULT_CONTEXT_WINDOW = 1_000_000_000_000;

export const WINDOW_PRESETS = [
	{id: '1m', label: '1M', value: 1_000_000},
	{id: '1b', label: '1B', value: 1_000_000_000},
	{id: '1t', label: '1T', value: DEFAULT_CONTEXT_WINDOW},
] as const;

export function parseWindowInput(raw: string): number | null {
	const text = raw.trim().toLowerCase().replace(/,/g, '').replace(/_/g, '').replace(/\s/g, '');
	if (!text) {
		return null;
	}

	let multiplier = 1;
	let amount = text;
	const last = text[text.length - 1];
	if (last && 'kmbtg'.includes(last) && text.length > 1) {
		const prev = text[text.length - 2];
		if (prev && (prev === '.' || (prev >= '0' && prev <= '9'))) {
			multiplier =
				last === 'k'
					? 1_000
					: last === 'm'
						? 1_000_000
						: last === 'b' || last === 'g'
							? 1_000_000_000
							: 1_000_000_000_000;
			amount = text.slice(0, -1);
		}
	}

	const parsed = Number(amount);
	if (!Number.isFinite(parsed) || parsed <= 0) {
		return null;
	}

	const value = Math.trunc(parsed * multiplier);
	return value > 0 ? value : null;
}

export function formatWindow(value: number): string {
	const n = Math.trunc(value);
	if (!Number.isFinite(n) || n <= 0) {
		return '—';
	}

	if (n >= 1_000_000_000_000 && n % 1_000_000_000_000 === 0) {
		return `${n / 1_000_000_000_000}T`;
	}

	if (n >= 1_000_000_000_000) {
		return `${Number((n / 1_000_000_000_000).toPrecision(3))}T`;
	}

	if (n >= 1_000_000_000 && n % 1_000_000_000 === 0) {
		return `${n / 1_000_000_000}B`;
	}

	if (n >= 1_000_000_000) {
		return `${Number((n / 1_000_000_000).toPrecision(3))}B`;
	}

	if (n >= 1_000_000 && n % 1_000_000 === 0) {
		return `${n / 1_000_000}M`;
	}

	if (n >= 1_000_000) {
		return `${Number((n / 1_000_000).toPrecision(3))}M`;
	}

	if (n >= 1_000 && n % 1_000 === 0) {
		return `${n / 1_000}K`;
	}

	return n.toLocaleString();
}

export function asContextWindow(value: unknown): number {
	if (typeof value === 'number' && Number.isFinite(value) && value > 0) {
		return Math.trunc(value);
	}

	if (typeof value === 'string') {
		const parsed = parseWindowInput(value);
		if (parsed !== null) {
			return parsed;
		}
	}

	return DEFAULT_CONTEXT_WINDOW;
}
