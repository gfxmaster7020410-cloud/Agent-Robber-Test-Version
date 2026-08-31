import React from 'react';
import {Box, Text} from 'ink';
import {
	categoryColor,
	type SlashCommandInfo,
} from '../lib/slashCommands.js';
import type {UiTheme} from '../utils/theme.js';

interface CommandSuggestProps {
	items: readonly SlashCommandInfo[];
	selected: number;
	theme: UiTheme;
}

function CommandSuggestInner({items, selected, theme}: CommandSuggestProps) {
	if (items.length === 0) {
		return null;
	}

	const current = items[selected] ?? items[0];
	const border = theme === 'light' ? 'blue' : 'cyan';

	return (
		<Box flexDirection="column" borderStyle="round" borderColor={border} paddingX={1}>
			{items.map((item, index) => {
				const active = index === selected;
				return (
					<Box key={item.name} gap={1}>
						<Text
							color={active ? 'black' : undefined}
							backgroundColor={active ? border : undefined}
							bold={active}
						>
							{active ? '›' : ' '} /{item.name}
						</Text>
						<Text color={categoryColor(item.category)}>[{item.category}]</Text>
						<Text dimColor wrap="truncate">
							{item.description}
						</Text>
					</Box>
				);
			})}
			{current ? (
				<Text dimColor wrap="truncate">
					{current.usage}
				</Text>
			) : null}
		</Box>
	);
}

export const CommandSuggest = React.memo(CommandSuggestInner);
