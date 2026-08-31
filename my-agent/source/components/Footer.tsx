import React from 'react';
import {Box, Text} from 'ink';
import type {ConnectionStatus} from '../types.js';
import {inkProps} from '../utils/typography.js';

interface FooterProps {
	memoryActive: boolean;
	columns: number;
	connection: ConnectionStatus;
}

const STATUS_COLOR: Record<ConnectionStatus, string> = {
	connecting: 'yellow',
	online: 'green',
	offline: 'red',
};

function statusFor(
	connection: ConnectionStatus,
	memoryActive: boolean,
): string {
	if (connection === 'offline') {
		return 'daemon offline — run python server.py';
	}

	if (connection === 'connecting') {
		return 'daemon linking...';
	}

	return memoryActive ? 'daemon linked · mem0 sync ✓' : 'daemon linked';
}

interface KeyHint {
	key: string;
	action: string;
}

const KEY_HINTS: KeyHint[] = [
	{key: 'Ctrl+C', action: 'Exit'},
	{key: 'Ctrl+L', action: 'Clear'},
	{key: '/memory', action: 'Inspect'},
	{key: '/window', action: 'Context'},
	{key: '/canvas', action: 'Pane'},
];

export function Footer({memoryActive, columns, connection}: FooterProps) {
	const statusLabel = statusFor(connection, memoryActive);
	const available = Math.max(24, columns - statusLabel.length - 6);

	const hints: KeyHint[] = [];
	let used = 0;
	for (const hint of KEY_HINTS) {
		const cost = hint.key.length + hint.action.length + 5;
		if (hints.length > 0 && used + cost > available) {
			break;
		}

		hints.push(hint);
		used += cost;
	}

	return (
		<Box paddingX={1} justifyContent="space-between">
			<Text>
				{hints.map((hint, index) => (
					<Text key={hint.key}>
						{index > 0 ? (
							<Text {...inkProps('micro')}> │ </Text>
						) : null}
						<Text bold>{hint.key}</Text>
						<Text {...inkProps('micro')}> {hint.action}</Text>
					</Text>
				))}
			</Text>
			<Text {...inkProps('micro')} color={STATUS_COLOR[connection]}>
				{statusLabel}
			</Text>
		</Box>
	);
}
