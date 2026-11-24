import {useEffect, useMemo, useState} from 'react';
import type {ChatMessage, DashboardView} from '../lib/types';
import type {BusyPhase, DaemonState} from '../hooks/useDaemon';
import {AppFooter} from './AppFooter';
import {AppHeader} from './AppHeader';
import {MainWorkspace} from './MainWorkspace';
import {SSOTSidebar} from './SSOTSidebar';
import {HorizontalRule, VerticalRule} from './Vectors';
import {
	collectFileHints,
	collectMutations,
	formatElapsed,
	inferLayout,
	sessionId,
	toStreamSteps,
	type LayoutMode,
} from './stream';

interface WorkbenchProps {
	daemon: DaemonState | null;
	messages: ChatMessage[];
	busy: BusyPhase | null;
	onSubmit: (text: string) => void;
	onUndo: () => void;
	onRedo: () => void;
	onAccept: () => void;
	onRevert: () => void;
	onKill: () => void;
	onPlan: () => void;
	onHybrid: () => void;
	onControl: () => void;
	onBeast: () => void;
	onView: (view: DashboardView) => void;
}

export function Workbench({
	daemon,
	messages,
	busy,
	onSubmit,
	onUndo,
	onRedo,
	onAccept,
	onRevert,
	onKill,
	onPlan,
	onHybrid,
	onControl,
	onBeast,
	onView,
}: WorkbenchProps) {
	const [historyOpen, setHistoryOpen] = useState(false);
	const [layout, setLayout] = useState<LayoutMode>('standard');
	const [autoLayout, setAutoLayout] = useState(true);
	const [sidebarOpen, setSidebarOpen] = useState(true);
	const [now, setNow] = useState(() => Date.now());
	const [turnStart, setTurnStart] = useState<number | null>(null);
	const [frozen, setFrozen] = useState(0);
	const sid = useMemo(() => sessionId(), []);
	const hints = useMemo(() => collectFileHints(messages), [messages]);
	const parsedMutations = useMemo(() => collectMutations(messages), [messages]);
	const mutations = daemon?.mutations ?? parsedMutations;
	const users = messages.filter(message => message.role === 'user');
	const working = busy !== null || daemon?.busy === true;

	useEffect(() => {
		const timer = window.setInterval(() => setNow(Date.now()), 250);
		return () => window.clearInterval(timer);
	}, []);

	useEffect(() => {
		if (working && turnStart === null) {
			setTurnStart(Date.now());
			return;
		}
		if (!working && turnStart !== null) {
			setFrozen(Date.now() - turnStart);
			setTurnStart(null);
		}
	}, [turnStart, working]);

	useEffect(() => {
		if (!autoLayout) {
			return;
		}
		const next = inferLayout(toStreamSteps(messages));
		setLayout(next);
		if (next === 'wide') {
			setSidebarOpen(false);
		}
	}, [autoLayout, messages]);

	const elapsed = formatElapsed(
		working && turnStart ? now - turnStart : daemon?.elapsedMs || frozen,
	);

	function applyLayout(mode: LayoutMode): void {
		setAutoLayout(false);
		setLayout(mode);
		setSidebarOpen(mode !== 'wide');
	}

	function toggleSidebar(): void {
		setAutoLayout(false);
		setSidebarOpen(open => !open);
	}

	return (
		<div className="wb flex h-screen min-h-0 flex-col overflow-hidden text-wb-fg">
			<AppHeader
				workspace={daemon?.workspace || 'workspace'}
				layout={sidebarOpen ? layout : 'wide'}
				onLayout={applyLayout}
			/>
			<HorizontalRule />
			<div className="wb-split">
				<div className="wb-pane wb-pane--main relative min-h-0 min-w-0">
					<MainWorkspace messages={messages} busy={busy} layout={layout} />
					{historyOpen ? (
						<div className="absolute inset-y-3 left-6 z-10 w-[min(420px,70%)] overflow-hidden border border-wb-border bg-wb-card">
							<div className="flex items-center justify-between px-3 py-2 font-ui-medium text-[13px]">
								<span>History</span>
								<button type="button" className="font-mono text-[11px] font-medium text-wb-yellow" onClick={() => onView('config')}>
									Control Center
								</button>
							</div>
							<HorizontalRule />
							<ul className="wb-scroll max-h-[70vh] p-2">
								{users.length === 0 ? (
									<li className="px-2 py-3 font-mono text-[12px] font-normal text-wb-muted">No prompts yet</li>
								) : (
									users
										.slice()
										.reverse()
										.map(message => (
											<li key={message.id}>
												<button
													type="button"
													className="w-full truncate px-2 py-1.5 text-left font-mono text-[12px] font-medium hover:bg-[#2a2a38]"
													onClick={() => {
														onSubmit(message.content);
														setHistoryOpen(false);
													}}
												>
													{message.content}
												</button>
											</li>
										))
								)}
							</ul>
						</div>
					) : null}
					<AppFooter
						busy={working}
						onSubmit={onSubmit}
						fileHints={hints}
						onUndo={onUndo}
						onRedo={onRedo}
						onAccept={onAccept}
						onRevert={onRevert}
						mode={daemon?.mode ?? 'plan'}
						model={daemon?.model || 'Hybrid LLM'}
						control={daemon?.controlMode ?? 'autonomous'}
						session={sid}
						elapsed={elapsed}
						onPlan={onPlan}
						onHybrid={onHybrid}
						onControl={onControl}
						onHistory={() => setHistoryOpen(open => !open)}
						onBeast={onBeast}
						onKill={onKill}
						historyOpen={historyOpen}
						hybrid={Boolean(daemon?.hybrid)}
					/>
				</div>
				<VerticalRule />
				<div className="wb-pane wb-pane--side">
					<SSOTSidebar
						daemon={daemon}
						mutations={mutations}
						expanded={sidebarOpen}
						onView={onView}
						onToggle={toggleSidebar}
					/>
				</div>
			</div>
		</div>
	);
}
