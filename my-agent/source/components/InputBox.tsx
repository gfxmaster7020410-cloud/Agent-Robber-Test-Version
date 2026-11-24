import React, {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {Box, Text, useInput} from 'ink';
import {CommandSuggest} from './CommandSuggest.js';
import {
	completeSlashLine,
	fuzzyCommands,
	slashMenuOpen,
} from '../lib/slashCommands.js';
import {
	BRACKETED_PASTE_DISABLE,
	BRACKETED_PASTE_ENABLE,
	PASTE_END,
	PASTE_START,
	contextMarker,
	isHugePaste,
	pageCount,
	sanitizePaste,
} from '../lib/pasteBuffer.js';
import {chalkUi, type UiTheme} from '../utils/theme.js';
import fs from 'node:fs';
import path from 'node:path';

interface InputBoxProps {
	onSubmit: (value: string) => void;
	onClear: () => void;
	busy: boolean;
	theme: UiTheme;
}

function offloadLocal(text: string): string {
	const clean = sanitizePaste(text);
	const dest = path.join(process.cwd(), '.agent_robber', 'tmp_context', 'paste_buffer.txt');
	fs.mkdirSync(path.dirname(dest), {recursive: true});
	fs.writeFileSync(dest, clean, 'utf8');
	void fetch('http://127.0.0.1:8787/api/context/paste', {
		method: 'POST',
		headers: {'Content-Type': 'application/json'},
		body: JSON.stringify({text: clean}),
	}).catch(() => undefined);
	return contextMarker(pageCount(clean));
}

function InputBoxInner({onSubmit, onClear, busy, theme}: InputBoxProps) {
	const [value, setValue] = useState('');
	const [selected, setSelected] = useState(0);
	const [dismissed, setDismissed] = useState(false);
	const pastingRef = useRef(false);
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

	useEffect(() => {
		if (!process.stdout.isTTY) {
			return;
		}
		process.stdout.write(BRACKETED_PASTE_ENABLE);
		return () => {
			process.stdout.write(BRACKETED_PASTE_DISABLE);
		};
	}, []);

	const resetField = useCallback(() => {
		setValue('');
		setDismissed(false);
		setSelected(0);
	}, []);

	const applyChunk = useCallback((next: string) => {
		const clean = sanitizePaste(next);
		if (isHugePaste(clean)) {
			setValue(offloadLocal(clean));
			setDismissed(false);
			return;
		}
		setValue(clean);
		if (!clean.startsWith('/')) {
			setDismissed(false);
		}
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

	useInput((input, key) => {
		if (key.ctrl && input === 'c') {
			return;
		}
		if (busy) {
			return;
		}
		if (key.ctrl && input === 'l') {
			resetField();
			clearRef.current();
			return;
		}

		let chunk = input ?? '';
		if (chunk.includes(PASTE_START)) {
			pastingRef.current = true;
			chunk = chunk.split(PASTE_START).join('');
		}
		if (chunk.includes(PASTE_END)) {
			pastingRef.current = false;
			chunk = chunk.split(PASTE_END).join('');
			applyChunk(value + chunk);
			return;
		}

		if (open && matches.length > 0) {
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
				return;
			}
		}

		if (key.ctrl && key.return) {
			handleSubmit(value);
			return;
		}
		if (key.return) {
			if (pastingRef.current) {
				applyChunk(`${value}${chunk.replace(/\r/g, '\n')}`);
				return;
			}
			if (chunk.length > 1) {
				applyChunk(value + chunk.replace(/\r/g, '\n'));
				return;
			}
			handleSubmit(value);
			return;
		}
		if (key.backspace || key.delete) {
			setValue(current => current.slice(0, -1));
			return;
		}
		if (key.upArrow || key.downArrow || key.leftArrow || key.rightArrow || key.tab || key.escape) {
			return;
		}
		if (chunk) {
			if (pastingRef.current) {
				applyChunk(value + chunk);
				return;
			}
			setValue(current => {
				const next = current + chunk;
				if (!next.startsWith('/')) {
					setDismissed(false);
				}
				return next;
			});
		}
	});

	const preview = value.includes('\n') ? value.split('\n').slice(-6).join('\n') : value;

	return (
		<Box flexDirection="column" marginTop={1}>
			{open ? <CommandSuggest items={matches} selected={selected} theme={theme} /> : null}
			<Box
				borderStyle="round"
				borderColor={busy ? 'gray' : theme === 'light' ? 'blue' : 'cyan'}
				paddingX={1}
				flexDirection="column"
			>
				<Box>
					<Text>{busy ? paint.promptMuted : paint.prompt}</Text>
					<Text>{preview || (busy ? '' : 'Type a prompt... (/help for commands)')}</Text>
				</Box>
			</Box>
		</Box>
	);
}

export const InputBox = React.memo(InputBoxInner);
