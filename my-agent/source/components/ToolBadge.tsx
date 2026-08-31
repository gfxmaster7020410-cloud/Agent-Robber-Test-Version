import React from 'react';
import {Text} from 'ink';
import {formatClock} from '../utils/theme.js';
import {inkProps} from '../utils/typography.js';

export type BadgeKind = 'tool' | 'memory' | 'success';

interface BadgeStyle {
	symbol: string;
	tag: string;
	background: 'yellow' | 'magenta' | 'green';
	foreground: 'black' | 'white';
}

const BADGE_STYLES: Record<BadgeKind, BadgeStyle> = {
	tool: {
		symbol: '⚙',
		tag: 'TOOL RUNNING',
		background: 'yellow',
		foreground: 'black',
	},
	memory: {
		symbol: '❖',
		tag: 'MEM0 RECALL',
		background: 'magenta',
		foreground: 'white',
	},
	success: {
		symbol: '✔',
		tag: 'DONE',
		background: 'green',
		foreground: 'black',
	},
};

interface ToolBadgeProps {
	kind: BadgeKind;
	label: string;
	timestamp?: number;
}

export function ToolBadge({kind, label, timestamp}: ToolBadgeProps) {
	const style = BADGE_STYLES[kind];
	return (
		<Text>
			<Text backgroundColor={style.background} color={style.foreground} bold>
				{` ${style.symbol} ${style.tag} `}
			</Text>
			<Text> {label}</Text>
			{timestamp ? (
				<Text {...inkProps('micro')}>
					{` [ TIMESTAMP: ${formatClock(timestamp)} ]`}
				</Text>
			) : null}
		</Text>
	);
}
