import {useMemo, useState} from 'react';
import {SlashField} from '../components/CommandAutocomplete';
import {BRAND} from './brand';
import {HorizontalRule, SlashMark, VectorIcon} from './Vectors';

interface AppFooterProps {
	busy: boolean;
	onSubmit: (text: string) => void;
	fileHints: string[];
	onUndo: () => void;
	onRedo: () => void;
	onAccept: () => void;
	onRevert: () => void;
	mode: string;
	model: string;
	control: string;
	session: string;
	elapsed: string;
	onPlan: () => void;
	onHybrid: () => void;
	onControl: () => void;
	onHistory: () => void;
	onBeast: () => void;
	onKill: () => void;
	historyOpen: boolean;
	hybrid: boolean;
}

export function AppFooter({
	busy,
	onSubmit,
	fileHints,
	onUndo,
	onRedo,
	onAccept,
	onRevert,
	mode,
	model,
	control,
	session,
	elapsed,
	onPlan,
	onHybrid,
	onControl,
	onHistory,
	onBeast,
	onKill,
	historyOpen,
	hybrid,
}: AppFooterProps) {
	const [draft, setDraft] = useState('');
	const atMatch = draft.match(/@([\w./\\-]*)$/);
	const atOpen = Boolean(atMatch) && !draft.startsWith('/');
	const atHits = useMemo(() => {
		const needle = (atMatch?.[1] ?? '').toLowerCase();
		return fileHints.filter(path => path.toLowerCase().includes(needle)).slice(0, 8);
	}, [atMatch, fileHints]);

	function send(text: string): void {
		const line = text.trim();
		if (!line || busy) {
			return;
		}
		setDraft('');
		onSubmit(line);
	}

	return (
		<footer className="wb-foot">
			<div className="wb-foot__timer">{elapsed}</div>
			<HorizontalRule />
			<div className="wb-foot__meta">
				<div className="wb-foot__pills">
					<button type="button" className="hover:text-wb-yellow" onClick={onPlan}>
						[{prettyMode(mode)} Mode]
					</button>
					<button type="button" className="hover:text-wb-yellow" onClick={onHybrid}>
						[{hybrid ? 'Hybrid Multi-LLM' : model || 'Hybrid LLM'}]
					</button>
					<button type="button" className="hover:text-wb-yellow" onClick={onControl}>
						[{control === 'manual' ? 'Manual' : 'Autonomous'}]
					</button>
					<span className="wb-foot__session">session ID-{session}</span>
				</div>
				<div className="wb-foot__tools">
					<button type="button" className="text-wb-yellow" onClick={onBeast}>
						Beast
					</button>
					<button type="button" className={historyOpen ? 'text-wb-yellow' : ''} onClick={onHistory}>
						History
					</button>
					<button type="button" className="text-wb-yellow" onClick={onKill}>
						Kill Task
					</button>
				</div>
			</div>
			<div className="wb-foot__rule" />
			<div className="wb-foot__body">
				<div className="relative min-w-0 w-full">
					{atOpen && atHits.length > 0 ? (
						<div className="absolute bottom-full left-10 z-20 mb-1 w-[min(480px,70vw)] overflow-hidden border border-wb-border bg-wb-card">
							{atHits.map(path => (
								<button
									key={path}
									type="button"
									className="block w-full truncate px-3 py-1.5 text-left font-mono text-[12px] font-normal hover:bg-[#2a2a38]"
									onMouseDown={event => {
										event.preventDefault();
										setDraft(current => current.replace(/@[\w./\\-]*$/, `@${path} `));
									}}
								>
									@{path}
								</button>
							))}
						</div>
					) : null}
					<div className="wb-prompt">
						<SlashMark />
						<SlashField
							value={draft}
							onChange={setDraft}
							onSubmit={send}
							placeholder="Fix bug in pref.tsx and relaunch server"
							className="wb-input"
							ariaLabel="Prompt"
							disabled={busy}
						/>
						<button
							type="button"
							className="wb-submit"
							aria-label="Send prompt"
							disabled={busy || !draft.trim()}
							onClick={() => send(draft)}
						>
							<VectorIcon src={BRAND.accept} />
						</button>
					</div>
				</div>
				<div className="wb-foot__actions">
					<button type="button" className="wb-action" onClick={onUndo}>
						<VectorIcon src={BRAND.undo} />
						<span>[Undo Step]</span>
					</button>
					<button type="button" className="wb-action" onClick={onRedo}>
						<VectorIcon src={BRAND.redo} />
						<span>[Redo Step]</span>
					</button>
					<button type="button" className="wb-action wb-action--accept" onClick={onAccept}>
						<VectorIcon src={BRAND.accept} />
						<span>[Accept All]</span>
					</button>
					<button type="button" className="wb-action wb-action--revert" onClick={onRevert}>
						<VectorIcon src={BRAND.revert} />
						<span>[Revert All]</span>
					</button>
				</div>
			</div>
		</footer>
	);
}

function prettyMode(mode: string): string {
	const raw = (mode || 'plan').trim() || 'plan';
	return raw.charAt(0).toUpperCase() + raw.slice(1);
}
