import React from 'react';
import {Box, Text} from 'ink';
import {countWrappedLines} from '../utils/content.js';
import {inkProps} from '../utils/typography.js';

interface ReasoningCanvasProps {
	body: string;
	engine: string;
	width: number;
	height: number;
	live: boolean;
}

function visibleLines(body: string, width: number, budget: number): string[] {
	const inner = Math.max(8, width - 4);
	const rows: string[] = [];
	for (const line of (body || 'awaiting prompt — chain-of-thought lands here').split('\n')) {
		if (line.length <= inner) {
			rows.push(line);
			continue;
		}

		for (let offset = 0; offset < line.length; offset += inner) {
			rows.push(line.slice(offset, offset + inner));
		}
	}

	if (rows.length <= budget) {
		return rows;
	}

	return rows.slice(rows.length - budget);
}

export function ReasoningCanvas({
	body,
	engine,
	width,
	height,
	live,
}: ReasoningCanvasProps) {
	const source = live ? 'live' : 'local';
	const tab = ` REASONING CANVAS · ${engine} · ${source} `;
	const innerWidth = Math.max(tab.length + 2, width - 2);
	const fill = Math.max(1, innerWidth - tab.length - 1);
	const lineBudget = Math.max(1, height - 3);
	const lines = visibleLines(body, innerWidth, lineBudget);

	return (
		<Box flexDirection="column" width={width} height={height}>
			<Text>
				<Text color="yellow">╭─</Text>
				<Text backgroundColor="yellow" color="black" bold>
					{tab}
				</Text>
				<Text color="yellow">{'─'.repeat(fill)}╮</Text>
			</Text>
			<Box
				flexDirection="column"
				borderStyle="round"
				borderColor="yellow"
				borderTop={false}
				paddingX={1}
				height={height - 1}
				overflow="hidden"
				width={Math.min(width, innerWidth + 2)}
			>
				{lines.map((line, index) => (
					<Text key={`${index}:${line.slice(0, 12)}`} {...inkProps('metadata')}>
						{line.length > 0 ? line : ' '}
					</Text>
				))}
			</Box>
		</Box>
	);
}

export function estimateCanvasLines(body: string, width: number): number {
	return countWrappedLines(body, Math.max(8, width - 4));
}
