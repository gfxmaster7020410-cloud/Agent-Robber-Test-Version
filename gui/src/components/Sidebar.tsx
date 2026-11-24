import type {ReactNode} from 'react';
import type {DashboardView} from '../lib/types';
import {IconFile, IconGear, IconGrid, IconLayers, IconPlug, IconPrompt} from './Icons';

const IconBot = () => (
	<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
		<rect x="3" y="11" width="18" height="10" rx="2" />
		<circle cx="12" cy="5" r="2" />
		<path d="M12 7v4" />
		<line x1="8" y1="16" x2="8.01" y2="16" />
		<line x1="16" y1="16" x2="16.01" y2="16" />
	</svg>
);

interface SidebarProps {
	view: DashboardView;
	onView: (view: DashboardView) => void;
	online: boolean;
	onEngage: () => void;
}

const LINKS: {id: DashboardView; label: string; icon: () => ReactNode}[] = [
	{id: 'workbench', label: 'Workbench', icon: IconPrompt},
	{id: 'overview', label: 'Overview', icon: IconGrid},
	{id: 'config', label: 'Configuration', icon: IconGear},
	{id: 'mcp', label: 'MCP Marketplace', icon: IconPlug},
	{id: 'memo-store', label: 'MeMO Store', icon: IconLayers},
	{id: 'console', label: 'CLI Console', icon: IconPrompt},
	{id: 'logs', label: 'Logs', icon: IconFile},
	{id: 'fb-shark', label: 'FB SHARK Engine', icon: IconBot},
];

export function Sidebar({view, onView, online, onEngage}: SidebarProps) {
	return (
		<aside className="sidebar">
			<nav className="sidebar__nav">
				<span className="sidebar__heading font-display">NAVIGATION</span>
				{LINKS.map(link => (
					<button
						key={link.id}
						type="button"
						className={`sidebar__link${view === link.id ? ' active' : ''}`}
						onClick={() => onView(link.id)}
					>
						{link.icon()}
						<span>{link.label}</span>
					</button>
				))}
			</nav>
			<div className="sidebar__footer">
				<button
					type="button"
					className={`engine-btn${online ? ' running' : ''}`}
					onClick={onEngage}
				>
					<span className="engine-btn__glow" />
				<span className="engine-btn__label font-display">
					{online ? 'DAEMON LINKED' : 'ENGAGE ENGINE'}
				</span>
				</button>
			</div>
		</aside>
	);
}
