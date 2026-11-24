type DesktopWindow = {
	minimize: () => Promise<void>;
	toggleMaximize: () => Promise<void>;
	close: () => Promise<void>;
	startDragging: () => Promise<void>;
};

export function isDesktopShell(): boolean {
	return Boolean((window as {__TAURI_INTERNALS__?: unknown}).__TAURI_INTERNALS__);
}

export async function desktopWindow(): Promise<DesktopWindow | null> {
	if (!isDesktopShell()) {
		return null;
	}
	try {
		const {getCurrentWindow} = await import('@tauri-apps/api/window');
		return getCurrentWindow();
	} catch {
		return null;
	}
}
