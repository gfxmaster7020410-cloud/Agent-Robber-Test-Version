import {useState, type ReactNode} from 'react';
import type {DashboardView} from '../lib/types';
import type {DaemonState} from '../hooks/useDaemon';
import {DEFAULT_CONTEXT_WINDOW, formatWindow} from '../lib/contextWindow';
import {LiveFileMutationsCard} from './LiveFileMutationsCard';
import type {FileMutation} from './stream';
import {AccordionCaret, SidebarToggleIcon} from './Vectors';

interface SSOTSidebarProps {
	daemon: DaemonState | null;
	mutations: FileMutation[];
	expanded: boolean;
	onView: (view: DashboardView) => void;
	onToggle: () => void;
}

const CANVAS = ['#22c55e', '#ef4444', '#f97316', '#38bdf8', '#a855f7', '#ec4899'];

export function SSOTSidebar({daemon, mutations, expanded, onView, onToggle}: SSOTSidebarProps) {
	const packed = daemon?.contextPacked ?? 0;
	const windowSize = daemon?.contextWindow ?? DEFAULT_CONTEXT_WINDOW;
	const mcpOnline = daemon?.mcp?.enabled ?? 0;

	return (
		<aside className={`wb-side ${expanded ? 'is-expanded' : 'is-collapsed'}`}>
			<div className="wb-side-head">
				<button
					type="button"
					className="wb-side-toggle"
					aria-label={expanded ? 'Collapse sidebar' : 'Expand sidebar'}
					onClick={onToggle}
				>
					<SidebarToggleIcon open={expanded} />
				</button>
			</div>
			{expanded ? (
				<div className="wb-side-scroll">
					<Accordion
						title="Unified Context & Memory (SSOT)"
						metric={`Unified Context • ${packed > 0 ? formatWindow(packed) : '0'} / ${formatWindow(windowSize)}`}
						defaultOpen
					>
						<p className="font-mono text-[12px] font-normal text-wb-muted">
							{daemon
								? `${daemon.ssotFiles} files · ${daemon.ssotChunks} chunks · ${daemon.ssotBackend}`
								: 'SSOT waiting for daemon'}
						</p>
					</Accordion>
					<Accordion title="MCP Servers & Tools" metric={`Active MCPs • ${mcpOnline} Online`}>
						<ul className="space-y-1 font-mono text-[12px] font-normal text-wb-muted">
							{(daemon?.mcp?.servers ?? []).slice(0, 8).map(server => (
								<li key={server.id} className="flex justify-between gap-2">
									<span>{server.label || server.id}</span>
									<span className={server.enabled ? 'text-wb-green-text' : ''}>
										{server.enabled ? 'online' : 'off'}
									</span>
								</li>
							))}
							{(daemon?.mcp?.servers.length ?? 0) === 0 ? <li>No MCP servers linked</li> : null}
						</ul>
						<button
							type="button"
							className="mt-2 text-left font-ui-medium text-[12px] text-wb-yellow"
							onClick={() => onView('mcp')}
						>
							Open marketplace
						</button>
					</Accordion>
					<Accordion
						title="Active Workspace & Modified Files"
						metric={`Workspace Index • ${mutations.length} Files Changed`}
						defaultOpen
					>
						<LiveFileMutationsCard files={mutations} />
					</Accordion>
					<Accordion title="Progress Canvas" metric="File online" defaultOpen>
						<div className="flex items-end gap-1 py-2">
							{CANVAS.map((color, index) => (
								<span
									key={color}
									className="h-8 w-8"
									style={{background: color, marginLeft: index === 2 ? 10 : 0}}
								/>
							))}
						</div>
					</Accordion>
					<Accordion title="Token Tracker" metric={`${daemon?.tps ?? 0}/1T     t/s`}>
						<p className="font-mono text-[12px] font-normal text-wb-muted">
							{daemon
								? `${daemon.ttftMs}ms ttft · packed ${formatWindow(packed)}`
								: 'Waiting for daemon metrics'}
						</p>
					</Accordion>
				</div>
			) : (
				<div className="wb-side-collapsed">
					<div className="wb-side-collapsed__fill" />
					<LiveFileMutationsCard files={mutations} />
				</div>
			)}
		</aside>
	);
}

function Accordion({
	title,
	metric,
	defaultOpen = false,
	children,
}: {
	title: string;
	metric: string;
	defaultOpen?: boolean;
	children: ReactNode;
}) {
	const [open, setOpen] = useState(defaultOpen);
	return (
		<div className="py-3">
			<button
				type="button"
				className="flex w-full items-start justify-between gap-2 text-left"
				onClick={() => setOpen(current => !current)}
				aria-expanded={open}
			>
				<span>
					<span className="block font-ui-medium text-[13px] text-wb-fg">{title}</span>
					<span className="mt-1 block font-mono text-[11px] font-normal text-wb-muted">{metric}</span>
				</span>
				<AccordionCaret open={open} />
			</button>
			{open ? <div className="mt-2">{children}</div> : null}
		</div>
	);
}
