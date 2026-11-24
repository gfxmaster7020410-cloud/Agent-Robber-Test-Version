import type {LayoutMode} from './stream';
import {BrandLogo} from './Vectors';
import {desktopWindow} from './desktop';

interface AppHeaderProps {
	workspace: string;
	layout: LayoutMode;
	onLayout: (mode: LayoutMode) => void;
}

export function AppHeader({workspace, layout, onLayout}: AppHeaderProps) {
	return (
		<header
			className="wb-header wb-drag flex items-center justify-between gap-4 px-5"
			data-tauri-drag-region
			onMouseDown={event => {
				if ((event.target as HTMLElement).closest('.wb-no-drag')) {
					return;
				}
				void desktopWindow().then(win => win?.startDragging());
			}}
		>
			<div className="flex min-w-0 items-center gap-3">
				<BrandLogo />
				<p className="min-w-0 truncate font-mono text-[12px] font-medium text-wb-muted" title={workspace}>
					{workspace}
				</p>
			</div>
			<div className="wb-no-drag flex items-center gap-2">
				<div className="flex overflow-hidden border border-wb-border font-ui-medium text-[11px]">
					{(
						[
							['standard', '1'],
							['wide', '2'],
							['diff', '3'],
						] as const
					).map(([mode, label]) => (
						<button
							key={mode}
							type="button"
							className={`px-2 py-1 ${layout === mode ? 'bg-wb-card text-wb-yellow' : 'text-wb-muted'}`}
							onClick={() => onLayout(mode)}
							title={mode === 'standard' ? 'Sample 1' : mode === 'wide' ? 'Ultra-wide sample 2' : 'Diff sample 3'}
						>
							{label}
						</button>
					))}
				</div>
				<WindowControls />
			</div>
		</header>
	);
}

function WindowControls() {
	return (
		<div className="wb-win-group flex items-center">
			<button type="button" className="wb-win" aria-label="Minimize" onClick={() => void run(win => win.minimize())}>
				—
			</button>
			<button
				type="button"
				className="wb-win"
				aria-label="Maximize"
				onClick={() => void run(win => win.toggleMaximize())}
			>
				□
			</button>
			<button type="button" className="wb-win wb-win--close" aria-label="Close" onClick={() => void run(win => win.close())}>
				✕
			</button>
		</div>
	);
}

async function run(action: (win: NonNullable<Awaited<ReturnType<typeof desktopWindow>>>) => Promise<void>): Promise<void> {
	const win = await desktopWindow();
	if (win) {
		await action(win);
		return;
	}
}
