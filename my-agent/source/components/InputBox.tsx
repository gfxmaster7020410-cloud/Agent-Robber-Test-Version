import React, {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {Box, Text, useInput} from 'ink';
import TextInput from 'ink-text-input';
import {CommandSuggest} from './CommandSuggest.js';
import {
	completeSlashLine,
	fuzzyCommands,
	slashMenuOpen,
} from '../lib/slashCommands.js';
import {chalkUi, type UiTheme} from '../utils/theme.js';

interface InputBoxProps {
	onSubmit: (value: string) => void;
	onClear: () => void;
	busy: boolean;
	theme: UiTheme;
}

function InputBoxInner({onSubmit, onClear, busy, theme}: InputBoxProps) {
	const [value, setValue] = useState('');
	const [selected, setSelected] = useState(0);
	const [dismissed, setDismissed] = useState(false);
	const submitRef = useRef(onSubmit);
	const clearRef = useRef(onClear);
	submitRef.current = onSubmit;
	clearRef.current = onClear;
	const paint = chalkUi(theme);
	const open = !busy && slashMenuOpen(value, dismissed);
	const matches = useMemo(
		() => (open ? fuzzyCommands(value, undefined, 5) : []),
		[open, value],
	);

	useEffect(() => {
		setSelected(0);
	}, [value]);

	useEffect(() => {
		if (matches.length === 0) {
			return;
		}
		setSelected(current => Math.min(current, matches.length - 1));
	}, [matches.length]);

	const resetField = useCallback(() => {
		setValue('');
		setDismissed(false);
		setSelected(0);
	}, []);

	const completeSelected = useCallback(
		(submit: boolean) => {
			const item = matches[selected];
			if (!item) {
				return;
			}
			const next = submit ? completeSlashLine(value, item.name) : `${completeSlashLine(value, item.name)} `;
			if (submit) {
				submitRef.current(next);
				resetField();
				return;
			}
			setValue(next);
			setDismissed(false);
		},
		[matches, resetField, selected, value],
	);

	useInput((input, key) => {
		if (key.ctrl && input === 'l') {
			resetField();
			clearRef.current();
			return;
		}
		if (!open || matches.length === 0) {
			return;
		}
		if (key.upArrow) {
			setSelected(current => (current - 1 + matches.length) % matches.length);
			return;
		}
		if (key.downArrow) {
			setSelected(current => (current + 1) % matches.length);
			return;
		}
		if (key.tab) {
			completeSelected(false);
			return;
		}
		if (key.escape) {
			setDismissed(true);
		}
	});

	const handleSubmit = useCallback(
		(raw: string) => {
			if (busy) {
				return;
			}
			const item = open ? matches[selected] : undefined;
			const line = item ? completeSlashLine(raw, item.name) : raw;
			submitRef.current(line);
			resetField();
		},
		[busy, matches, open, resetField, selected],
	);

	const handleChange = useCallback((next: string) => {
		setValue(next);
		if (!next.startsWith('/')) {
			setDismissed(false);
		}
	}, []);

	return (
		<Box flexDirection="column" marginTop={1}>
			{open ? <CommandSuggest items={matches} selected={selected} theme={theme} /> : null}
			<Box
				borderStyle="round"
				borderColor={busy ? 'gray' : theme === 'light' ? 'blue' : 'cyan'}
				paddingX={1}
			>
				<Text>{busy ? paint.promptMuted : paint.prompt}</Text>
				<TextInput
					value={value}
					onChange={handleChange}
					onSubmit={handleSubmit}
					placeholder="Type a prompt... (/help for commands)"
					focus={!busy}
					showCursor={!open}
				/>
			</Box>
		</Box>
	);
}

export const InputBox = React.memo(InputBoxInner);
