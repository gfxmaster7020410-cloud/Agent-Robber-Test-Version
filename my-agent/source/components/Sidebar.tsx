import React from 'react';
import {Box, Text} from 'ink';
import type {BusyKind} from '../types.js';
import {inkPalette, type UiTheme} from '../utils/theme.js';
import {inkProps} from '../utils/typography.js';

interface SidebarProps {
	width: number;
	busyKind: BusyKind | null;
	memoryActive: boolean;
	theme: UiTheme;
}

interface EngineCard {
	id: string;
	name: string;
	idleLabel: string;
	activeLabel: string;
	offlineLabel: string;
	detail: string;
	accent: 'cyan' | 'yellow' | 'magenta';
	isActive: (busyKind: BusyKind | null, memoryActive: boolean) => boolean;
}

const ENGINE_CARDS: EngineCard[] = [
	{
		id: 'file-scanner',
		name: 'FILE SCANNER',
		idleLabel: 'idle',
		activeLabel: 'scanning workspace',
		offlineLabel: 'idle',
		detail: 'workspace index · 128 paths',
		accent: 'cyan',
		isActive: busyKind => busyKind === 'tool',
	},
	{
		id: 'terminal-shell',
		name: 'TERMINAL SHELL',
		idleLabel: 'ready',
		activeLabel: 'executing command',
		offlineLabel: 'ready',
		detail: 'pwsh · sandboxed runtime',
		accent: 'yellow',
		isActive: busyKind => busyKind === 'tool',
	},
	{
		id: 'mem0-vector',
		name: 'MEM0 VECTOR DB',
		idleLabel: 'synced',
		activeLabel: 'syncing embeddings',
		offlineLabel: 'offline',
		detail: '3 entries · project scope',
		accent: 'magenta',
		isActive: (busyKind, memoryActive) =>
			memoryActive && (busyKind === 'thinking' || busyKind === 'memory'),
	},
];

const MEMORY_GRAPH = [
	{name: 'preference', note: 'concise'},
	{name: 'project', note: 'ink + ts esm'},
	{name: 'convention', note: 'no commits'},
];

function SectionTitle({label}: {label: string}) {
	return (
		<Text>
			<Text {...inkProps('micro')}>── </Text>
			<Text bold>{label}</Text>
		</Text>
	);
}

export function Sidebar({width, busyKind, memoryActive, theme}: SidebarProps) {
	const palette = inkPalette(theme);
	return (
		<Box flexDirection="column" width={width} paddingLeft={1}>
			<SectionTitle label="INSPECTOR" />
			{ENGINE_CARDS.map(card => {
				const active = card.isActive(busyKind, memoryActive);
				const offline = card.id === 'mem0-vector' && !memoryActive;
				const status = offline
					? card.offlineLabel
					: active
						? card.activeLabel
						: card.idleLabel;
				const statusColor = offline
					? 'yellow'
					: active
						? card.accent
						: 'green';
				return (
					<Box
						key={card.id}
						flexDirection="column"
						borderStyle="round"
						borderColor={active ? card.accent : palette.border}
						borderDimColor={!active}
						paddingX={1}
					>
						<Text bold color={active ? card.accent : palette.fg}>
							{`◈ ${card.name}`}
						</Text>
						<Text color={statusColor}>
							<Text bold>{offline ? '○' : '●'}</Text>
							{` ${status}`}
						</Text>
						<Text {...inkProps('micro')}>{card.detail}</Text>
					</Box>
				);
			})}
			<Box flexDirection="column">
				<SectionTitle label="SSOT CONTEXT GRAPH" />
				{MEMORY_GRAPH.map((node, index) => (
					<Text key={node.name}>
						<Text color="gray">
							{index === MEMORY_GRAPH.length - 1 ? '└── ' : '├── '}
						</Text>
						<Text color={palette.node}>{node.name}</Text>
						<Text {...inkProps('micro')}>{` · ${node.note}`}</Text>
					</Text>
				))}
			</Box>
		</Box>
	);
}
