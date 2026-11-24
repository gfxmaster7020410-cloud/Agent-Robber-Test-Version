export const DEFAULT_CONTEXT_WINDOW = 200_000;

export const WINDOW_PRESETS = [
	{id: '128k', label: '128K', value: 128_000},
	{id: '200k', label: '200K', value: DEFAULT_CONTEXT_WINDOW},
	{id: '1m', label: '1M', value: 1_000_000},
	{id: '1b', label: '1B', value: 1_000_000_000},
	{id: '1t', label: '1T', value: 1_000_000_000_000},
] as const;

const MULTIPLIER: Record<string, number> = {
	k: 1_000,
	m: 1_000_000,
	b: 1_000_000_000,
	g: 1_000_000_000,
	t: 1_000_000_000_000,
};

export function parseWindowInput(raw: string): number | null {
	const text = raw.trim().toUpperCase().replace(/,/g, '').replace(/_/g, '').replace(/\s/g, '');
	if (!text) {
		return null;
	}

	const match = text.match(/^(\d+(?:\.\d+)?(?:E[+-]?\d+)?)([KMBTG])?$/);
	let parsed: number;
	if (match) {
		parsed = Number(match[1]);
		const unit = match[2]?.toLowerCase();
		if (unit && unit in MULTIPLIER) {
			parsed *= MULTIPLIER[unit];
		}
	} else {
		parsed = Number(text);
	}

	if (!Number.isFinite(parsed) || parsed <= 0) {
		return null;
	}

	return Math.trunc(parsed);
}

export function formatWindow(value: number): string {
	const n = Math.trunc(value);
	if (!Number.isFinite(n) || n < 0) {
		return '—';
	}

	if (n === 0) {
		return '0';
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
