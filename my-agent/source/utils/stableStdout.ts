import {stdout as processStdout} from 'node:process';

/** Ignore ConPTY ±1 cell jitter that Windows emits on each keystroke. */
const JITTER_CELLS = 1;
const RESIZE_DEBOUNCE_MS = 120;
/**
 * Ink runs `clearTerminal` when `outputHeight >= stdout.rows`. Report extra
 * rows to Ink so a frame sized to the real window never takes that branch.
 */
const INK_ROW_GUARD = 16;

export interface FrozenSize {
	rows: number;
	columns: number;
}

let frozenRows = processStdout.rows ?? 32;
let frozenColumns = processStdout.columns ?? 80;
const sizeListeners = new Set<() => void>();
let resizeDebounce: ReturnType<typeof setTimeout> | undefined;
let listeningToRawResize = false;

function isJitter(nextRows: number, nextColumns: number): boolean {
	return (
		Math.abs(nextRows - frozenRows) <= JITTER_CELLS &&
		Math.abs(nextColumns - frozenColumns) <= JITTER_CELLS
	);
}

function commitSize(nextRows: number, nextColumns: number): void {
	if (nextRows === frozenRows && nextColumns === frozenColumns) {
		return;
	}

	frozenRows = nextRows;
	frozenColumns = nextColumns;
	for (const listener of sizeListeners) {
		listener();
	}
}

function onRawResize(): void {
	if (resizeDebounce !== undefined) {
		clearTimeout(resizeDebounce);
	}

	resizeDebounce = setTimeout(() => {
		const nextRows = processStdout.rows ?? frozenRows;
		const nextColumns = processStdout.columns ?? frozenColumns;
		if (isJitter(nextRows, nextColumns)) {
			return;
		}

		commitSize(nextRows, nextColumns);
	}, RESIZE_DEBOUNCE_MS);
}

function ensureRawResizeListener(): void {
	if (listeningToRawResize) {
		return;
	}

	listeningToRawResize = true;
	processStdout.on('resize', onRawResize);
}

export function getFrozenSize(): FrozenSize {
	ensureRawResizeListener();
	return {rows: frozenRows, columns: frozenColumns};
}

export function subscribeFrozenSize(listener: () => void): () => void {
	ensureRawResizeListener();
	sizeListeners.add(listener);
	return () => {
		sizeListeners.delete(listener);
	};
}

/**
 * Windows ConPTY often emits a `resize` event on every keystroke. Ink listens
 * to that event and, when the frame fills the terminal, erases the whole
 * screen (`clearTerminal`). This proxy freezes rows/columns during typing and
 * only forwards real window resizes.
 */
export function createStableStdout(
	stream: NodeJS.WriteStream = processStdout,
): NodeJS.WriteStream {
	ensureRawResizeListener();
	const resizeListeners: Array<(...args: unknown[]) => void> = [];

	subscribeFrozenSize(() => {
		for (const listener of resizeListeners) {
			listener();
		}
	});

	return new Proxy(stream, {
		get(target, property, receiver) {
			if (property === 'rows') {
				return frozenRows + INK_ROW_GUARD;
			}

			if (property === 'columns') {
				return frozenColumns;
			}

			if (property === 'on' || property === 'addListener') {
				return (event: string, listener: (...args: unknown[]) => void) => {
					if (event === 'resize') {
						resizeListeners.push(listener);
						return receiver;
					}

					return target.on(event, listener);
				};
			}

			if (property === 'off' || property === 'removeListener') {
				return (event: string, listener: (...args: unknown[]) => void) => {
					if (event === 'resize') {
						const index = resizeListeners.indexOf(listener);
						if (index >= 0) {
							resizeListeners.splice(index, 1);
						}

						return receiver;
					}

					return target.off(event, listener);
				};
			}

			const value = Reflect.get(target, property, target);
			return typeof value === 'function'
				? (value as (...args: unknown[]) => unknown).bind(target)
				: value;
		},
	});
}
