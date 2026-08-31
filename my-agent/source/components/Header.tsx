import React from 'react';
import {Box, Text} from 'ink';
import type {ConnectionStatus} from '../types.js';
import {BRAND, chalkUi, ellipsizeMiddle, type UiTheme} from '../utils/theme.js';
import {inkProps} from '../utils/typography.js';

interface HeaderProps {
	model: string;
	workingDirectory: string;
	memoryActive: boolean;
	connection: ConnectionStatus;
	theme: UiTheme;
	controlMode?: 'autonomous' | 'manual';
}

const LINK_LABEL: Record<ConnectionStatus, string> = {
	connecting: '◌ Linking',
	online: '● Linked',
	offline: '○ Offline',
};

function renderLink(connection: ConnectionStatus, theme: UiTheme): string {
	const label = LINK_LABEL[connection];
	const paint = chalkUi(theme);
	if (connection === 'online') {
		return paint.ok(label);
	}

	return connection === 'connecting' ? paint.warn(label) : paint.danger(label);
}

export function Header({
	model,
	workingDirectory,
	memoryActive,
	connection,
	theme,
	controlMode = 'autonomous',
}: HeaderProps) {
	const paint = chalkUi(theme);
	const border = theme === 'light' ? 'blue' : 'cyan';
	return (
		<Box flexDirection="column">
			<Box
				borderStyle="round"
				borderColor={border}
				flexDirection="column"
				paddingX={1}
			>
				<Box justifyContent="space-between">
					<Text {...inkProps('headline')}>
						{paint.title(BRAND.name)}{' '}
						<Text {...inkProps('metadata')}>{BRAND.version}</Text>
					</Text>
					<Text {...inkProps('metadata')}>
						{theme === 'light' ? 'light theme' : 'local agent runtime'}
					</Text>
				</Box>
				<Box columnGap={2}>
					<Text>
						<Text {...inkProps('metadata')}>{paint.muted('model ')}</Text>
						{paint.ok(model)}
					</Text>
					<Text>
						<Text {...inkProps('metadata')}>{paint.muted('cwd ')}</Text>
						{paint.warn(ellipsizeMiddle(workingDirectory, 48))}
					</Text>
					<Text>
						<Text {...inkProps('metadata')}>{paint.muted('mem0 ')}</Text>
						{memoryActive ? paint.ok('● Active') : paint.warn('○ Offline')}
					</Text>
					<Text>
						<Text {...inkProps('metadata')}>{paint.muted('loop ')}</Text>
						{paint.ok(controlMode)}
					</Text>
					<Text>
						<Text {...inkProps('metadata')}>{paint.muted('daemon ')}</Text>
						{renderLink(connection, theme)}
					</Text>
				</Box>
			</Box>
		</Box>
	);
}
