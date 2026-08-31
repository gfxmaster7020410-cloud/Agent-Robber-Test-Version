import {useEffect, useMemo, useState, type KeyboardEvent} from 'react';
import {
	categoryColor,
	completeSlashLine,
	fuzzyCommands,
	slashMenuOpen,
	type SlashCommandInfo,
} from '../lib/slashCommands';

export interface CommandAutocompleteProps {
	value: string;
	visible: boolean;
	selected: number;
	matches: readonly SlashCommandInfo[];
	onPick?: (item: SlashCommandInfo) => void;
}

export function CommandAutocomplete({
	value,
	visible,
	selected,
	matches,
	onPick,
}: CommandAutocompleteProps) {
	if (!visible || matches.length === 0) {
		return null;
	}

	const current = matches[selected] ?? matches[0];

	return (
		<div className="slash-menu" role="listbox" aria-label="Slash commands">
			{matches.map((item, index) => {
				const active = index === selected;
				return (
					<button
						key={item.name}
						type="button"
						role="option"
						aria-selected={active}
						className={`slash-menu__row${active ? ' is-active' : ''}`}
						tabIndex={-1}
						onMouseDown={event => {
							event.preventDefault();
							onPick?.(item);
						}}
					>
						<span className="slash-menu__name font-mono font-bold">/{item.name}</span>
						<span
							className={`slash-menu__badge slash-menu__badge--${item.category.toLowerCase()}`}
							style={{color: cssCategory(item.category)}}
						>
							{item.category}
						</span>
						<span className="slash-menu__desc">{item.description}</span>
					</button>
				);
			})}
			{current ? (
				<div className="slash-menu__hint">
					<span className="slash-menu__usage font-mono">{current.usage}</span>
					{value.startsWith('/') ? (
						<span className="slash-menu__keys font-display">↑↓ navigate · Tab complete · Enter run · Esc close</span>
					) : null}
				</div>
			) : null}
		</div>
	);
}

function cssCategory(category: SlashCommandInfo['category']): string {
	const color = categoryColor(category);
	if (color === 'cyan') {
		return 'var(--accent, #22d3ee)';
	}
	if (color === 'yellow') {
		return '#fbbf24';
	}
	if (color === 'green') {
		return '#34d399';
	}
	if (color === 'magenta') {
		return '#e879f9';
	}
	return '#60a5fa';
}

export interface SlashFieldProps {
	value: string;
	onChange: (value: string) => void;
	onSubmit: (value: string) => void;
	placeholder?: string;
	className?: string;
	ariaLabel?: string;
}

export function SlashField({
	value,
	onChange,
	onSubmit,
	placeholder,
	className,
	ariaLabel,
}: SlashFieldProps) {
	const [selected, setSelected] = useState(0);
	const [dismissed, setDismissed] = useState(false);
	const open = slashMenuOpen(value, dismissed);
	const matches = useMemo(
		() => (open ? fuzzyCommands(value, undefined, 8) : []),
		[open, value],
	);

	useEffect(() => {
		setSelected(0);
	}, [value]);

	function handleKeyDown(event: KeyboardEvent<HTMLInputElement>): void {
		if (open && matches.length > 0) {
			if (event.key === 'ArrowUp') {
				event.preventDefault();
				setSelected(current => (current - 1 + matches.length) % matches.length);
				return;
			}
			if (event.key === 'ArrowDown') {
				event.preventDefault();
				setSelected(current => (current + 1) % matches.length);
				return;
			}
			if (event.key === 'Tab') {
				event.preventDefault();
				const item = matches[selected];
				if (item) {
					onChange(`${completeSlashLine(value, item.name)} `);
				}
				return;
			}
			if (event.key === 'Escape') {
				event.preventDefault();
				setDismissed(true);
				return;
			}
		}

		if (event.key === 'Enter') {
			event.preventDefault();
			const item = open ? matches[selected] : undefined;
			const line = (item ? completeSlashLine(value, item.name) : value).trim();
			if (line) {
				onSubmit(line);
			}
		}
	}

	return (
		<>
			<CommandAutocomplete
				value={value}
				visible={open}
				selected={selected}
				matches={matches}
				onPick={item => {
					onSubmit(completeSlashLine(value, item.name));
				}}
			/>
			<input
				className={className}
				value={value}
				placeholder={placeholder}
				aria-label={ariaLabel}
				aria-autocomplete="list"
				aria-expanded={open}
				onChange={event => {
					const next = event.target.value;
					onChange(next);
					if (!next.startsWith('/')) {
						setDismissed(false);
					}
				}}
				onKeyDown={handleKeyDown}
			/>
		</>
	);
}
