import {useEffect, useMemo, useRef, useState} from 'react';
import {parseContentSegments, type ContentSegment} from '../lib/content';
import {formatHelp} from '../lib/slashCommands';
import {formatClock, type ChatMessage} from '../lib/types';
import type {BusyPhase} from '../hooks/useDaemon';
import {SlashField} from '../components/CommandAutocomplete';

const HELP = formatHelp();

type TermFilter = 'all' | 'system' | 'tool' | 'error' | 'user';

interface ConsoleViewProps {
	messages: ChatMessage[];
	busy: BusyPhase | null;
	statusLabel: string;
	onSubmit: (text: string) => void;
	onClear: () => void;
	onExport: () => void;
}

export function ConsoleView({
	messages,
	busy,
	statusLabel,
	onSubmit,
	onClear,
	onExport,
}: ConsoleViewProps) {
	const [draft, setDraft] = useState('');
	const [filter, setFilter] = useState<TermFilter>('all');
	const [paused, setPaused] = useState(false);
	const [started] = useState(() => Date.now());
	const [now, setNow] = useState(() => Date.now());
	const scroller = useRef<HTMLDivElement>(null);
	const visible = useMemo(
		() =>
			messages.filter(message => {
				if (message.role === 'reasoning') {
					return false;
				}

				if (filter === 'all') {
					return true;
				}

				if (filter === 'error') {
					return message.role === 'error';
				}

				if (filter === 'tool') {
					return message.role === 'tool';
				}

				if (filter === 'user') {
					return message.role === 'user';
				}

				return message.role === 'system' || message.role === 'success' || message.role === 'memory';
			}),
		[filter, messages],
	);

	useEffect(() => {
		const timer = window.setInterval(() => setNow(Date.now()), 1000);
		return () => window.clearInterval(timer);
	}, []);

	useEffect(() => {
		if (!paused) {
			scroller.current?.scrollTo({top: scroller.current.scrollHeight});
		}
	}, [paused, visible.length, busy]);

	const elapsed = formatElapsed(now - started);

	return (
		<div className="view view--fill">
			<div className="term-layout">
				<div className="term-header">
					<div className="term-header__left">
						<span className="term-header__title font-display">LIVE CLI CONSOLE</span>
						<span className={`term-header__badge${busy ? ' active' : ''}`}>{statusLabel}</span>
					</div>
					<div className="term-header__center">
						<div className="term-ribbon">
							<span className="term-ribbon__item">
								<span className="term-ribbon__lbl font-display">BUFFER</span>{' '}
								<span className="term-ribbon__val font-mono font-bold">{visible.length} Lines</span>
							</span>
							<span className="term-ribbon__sep" />
							<span className="term-ribbon__item">
								<span className="term-ribbon__lbl font-display">AUTOSCROLL</span>{' '}
								<span className={`term-ribbon__val font-mono font-bold${paused ? '' : ' term-ribbon__val--active'}`}>
									{paused ? 'OFF' : 'ON'}
								</span>
							</span>
							<span className="term-ribbon__sep" />
							<span className="term-ribbon__item">
								<span className="term-ribbon__lbl font-display">SESSION</span>{' '}
								<span className="term-ribbon__val font-mono font-bold">{elapsed}</span>
							</span>
						</div>
					</div>
					<div className="term-header__right">
						<div className="term-filter-bar">
							{(['all', 'system', 'tool', 'error', 'user'] as const).map(item => (
								<button
									key={item}
									type="button"
									className={`term-filter${filter === item ? ' active' : ''}`}
									onClick={() => setFilter(item)}
								>
									{item}
								</button>
							))}
						</div>
						<button type="button" className="term-action" onClick={onClear}>
							<span>Clear</span>
						</button>
						<button
							type="button"
							className={`term-action${paused ? ' active-toggle' : ''}`}
							onClick={() => setPaused(value => !value)}
						>
							<span>{paused ? 'Resume' : 'Pause'}</span>
						</button>
						<button type="button" className="term-action" onClick={onExport}>
							<span>Export</span>
						</button>
					</div>
				</div>
				<div className="term-body" ref={scroller}>
					{visible.map(message => (
						<TermLine key={message.id} message={message} />
					))}
					{busy ? (
						<div className="term-line">
							<span className="term-ts">[{formatClock(Date.now())}]</span>
							<span className="term-line--warn">{busy.label}</span>
						</div>
					) : null}
				</div>
				<div className="term-input-bar">
					<span className="term-prompt-char" aria-hidden="true">
						&gt;
					</span>
					<div className="term-input-wrap">
						<SlashField
							className="term-input"
							value={draft}
							placeholder="Type a monitor prompt or /help..."
							ariaLabel="Console prompt"
							onChange={setDraft}
							onSubmit={text => {
								setDraft('');
								onSubmit(text);
							}}
						/>
					</div>
					<button
						type="button"
						className="term-send"
						aria-label="Send"
						onClick={() => {
							const text = draft.trim();
							if (!text) {
								return;
							}

							setDraft('');
							onSubmit(text);
						}}
					>
						<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
							<line x1="22" y1="2" x2="11" y2="13" />
							<polygon points="22 2 15 22 11 13 2 9 22 2" />
						</svg>
					</button>
				</div>
			</div>
		</div>
	);
}

export {HELP};

function TermLine({message}: {message: ChatMessage}) {
	const stamp = formatClock(message.timestamp);

	if (message.role === 'tool' || message.role === 'memory' || message.role === 'success') {
		const badge =
			message.role === 'tool'
				? 'term-badge--tool'
				: message.role === 'memory'
					? 'term-badge--mem0'
					: 'term-badge--success';
		const label =
			message.role === 'tool' ? 'TOOL RUNNING' : message.role === 'memory' ? 'MEM0 RECALL' : 'DONE';
		return (
			<div className="term-entry">
				<div className="term-line">
					<span className="term-ts">[{stamp}]</span>
					<span className={`term-badge ${badge}`}>{label}</span>
					<span className="term-line__body term-line--dim">{message.content}</span>
				</div>
			</div>
		);
	}

	const segments = parseContentSegments(message.content);
	const text = segments.filter(segment => segment.type === 'text');
	const code = segments.filter(segment => segment.type === 'code');
	const loop = message.role === 'system' && /\[LOOP\]/i.test(message.content);
	const bodyClass =
		message.role === 'error'
			? 'term-line--err'
			: message.role === 'user'
				? 'term-line--cmd'
				: message.role === 'assistant'
					? 'term-line--body'
					: 'term-line--info';

	return (
		<div className="term-entry">
			<div className="term-line">
				<span className="term-ts">[{stamp}]</span>
				{loop ? <span className="term-badge term-badge--loop">LOOP</span> : null}
				<span className={`term-line__body ${bodyClass}`}>
					{message.role === 'user' ? <span className="term-prompt-mark">&gt;</span> : null}
					{joinText(text, message.content)}
				</span>
			</div>
			{code.map((segment, index) => (
				<div className="term-code-block" key={`${message.id}-code-${index}`}>
					<div className="term-code-header">
						<span className="term-code-lang">{segment.language || 'code'}</span>
					</div>
					<pre className="term-code-body">{segment.value}</pre>
				</div>
			))}
		</div>
	);
}

function joinText(segments: ContentSegment[], fallback: string): string {
	if (segments.length === 0) {
		return fallback;
	}

	return segments.map(segment => segment.value).join('\n');
}

function formatElapsed(ms: number): string {
	const total = Math.max(0, Math.floor(ms / 1000));
	const hours = String(Math.floor(total / 3600)).padStart(2, '0');
	const minutes = String(Math.floor((total % 3600) / 60)).padStart(2, '0');
	const seconds = String(total % 60).padStart(2, '0');
	return `${hours}:${minutes}:${seconds}`;
}
