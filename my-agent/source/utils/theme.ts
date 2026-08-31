import chalk from 'chalk';

export const BRAND = {
	name: 'AGENT ROBBER CLI',
	version: 'v1.0',
	defaultModel: 'Hybrid Multi-LLM',
} as const;

export type UiTheme = 'dark' | 'light';

export const ui = {
	title: chalk.cyan,
	muted: chalk.gray,
	ok: chalk.green,
	warn: chalk.yellow,
	danger: chalk.red,
	prompt: chalk.cyanBright('❯ '),
	promptMuted: chalk.gray('❯ '),
} as const;

export function chalkUi(theme: UiTheme) {
	if (theme === 'light') {
		return {
			title: chalk.blue,
			muted: chalk.gray,
			ok: chalk.green,
			warn: chalk.magenta,
			danger: chalk.red,
			prompt: chalk.blue('❯ '),
			promptMuted: chalk.gray('❯ '),
		};
	}

	return ui;
}

export function inkPalette(theme: UiTheme) {
	if (theme === 'light') {
		return {
			border: 'blue' as const,
			accent: 'blue' as const,
			bg: 'white' as const,
			fg: 'black' as const,
			node: 'black' as const,
		};
	}

	return {
		border: 'cyan' as const,
		accent: 'cyan' as const,
		bg: undefined,
		fg: undefined,
		node: 'whiteBright' as const,
	};
}

export function ellipsizeMiddle(value: string, maxLength: number): string {
	if (value.length <= maxLength) {
		return value;
	}

	const half = Math.floor((maxLength - 1) / 2);
	return `${value.slice(0, half)}…${value.slice(-(maxLength - half - 1))}`;
}

export function formatClock(timestamp: number): string {
	const date = new Date(timestamp);
	return [date.getHours(), date.getMinutes(), date.getSeconds()]
		.map(unit => String(unit).padStart(2, '0'))
		.join(':');
}
