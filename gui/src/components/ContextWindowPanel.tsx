import {useEffect, useState} from 'react';
import {
	DEFAULT_CONTEXT_WINDOW,
	WINDOW_PRESETS,
	formatWindow,
	parseWindowInput,
} from '../lib/contextWindow';

interface ContextWindowPanelProps {
	windowTokens?: number;
	packedTokens?: number;
	hitCount?: number;
	online?: boolean;
	compact?: boolean;
	onApply: (raw: string | number) => boolean;
}

export function ContextWindowPanel({
	windowTokens = DEFAULT_CONTEXT_WINDOW,
	packedTokens = 0,
	hitCount = 0,
	online = false,
	compact = false,
	onApply,
}: ContextWindowPanelProps) {
	const [draft, setDraft] = useState(() => String(windowTokens));

	useEffect(() => {
		setDraft(formatWindow(windowTokens));
	}, [windowTokens]);

	function apply(raw: string | number) {
		const parsed = typeof raw === 'number' ? raw : parseWindowInput(raw);
		if (parsed === null || parsed <= 0) {
			return;
		}

		setDraft(formatWindow(parsed));
		onApply(typeof raw === 'string' ? raw.trim() : parsed);
	}

	return (
		<div className={`ssot-window${compact ? ' ssot-window--compact' : ''}`}>
			<div className="ssot-window__meta">
				<span className="ssot-window__title font-display">SSOT context window</span>
				<span className="ssot-window__value font-mono font-bold">
					{formatWindow(windowTokens)}
					<span className="ssot-window__sub">
						{' '}
						· packed {formatWindow(packedTokens)} · {hitCount} hits
					</span>
				</span>
			</div>
			<p className="ssot-window__hint">
				Logical budget shared by every model. Type <code>128K</code>, <code>1M</code>,{' '}
				<code>1B</code>, or <code>1T</code> — no upper cap. Pin a section with{' '}
				<code>@file#Lstart-Lend</code>. Live — works while the agent is running.
			</p>
			<div className="ssot-window__row">
				{WINDOW_PRESETS.map(preset => (
					<button
						key={preset.id}
						type="button"
						className={`ssot-window__preset${windowTokens === preset.value ? ' is-active' : ''}`}
						onClick={() => apply(preset.value)}
					>
						{preset.label}
					</button>
				))}
				<input
					className="glass-input ssot-window__input"
					value={draft}
					placeholder="1M"
					aria-label="SSOT context window"
					onChange={event => setDraft(event.target.value)}
					onKeyDown={event => {
						if (event.key === 'Enter') {
							apply(draft);
						}
					}}
				/>
				<button
					type="button"
					className="mem0-action mem0-action--inject"
					disabled={!online}
					onClick={() => apply(draft)}
				>
					<span>Apply</span>
				</button>
			</div>
		</div>
	);
}
