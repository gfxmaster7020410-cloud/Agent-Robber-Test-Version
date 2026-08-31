import {useEffect, useState} from 'react';
import {getFrozenSize, subscribeFrozenSize, type FrozenSize} from '../utils/stableStdout.js';

export type TerminalSize = FrozenSize;

/**
 * Layout size for the Ink tree. Reads the frozen metrics so Windows ConPTY
 * keystroke-resize events cannot change `height` and trigger a full wipe.
 */
export function useTerminalSize(): TerminalSize {
	const [size, setSize] = useState(getFrozenSize);

	useEffect(() => {
		return subscribeFrozenSize(() => {
			setSize(getFrozenSize());
		});
	}, []);

	return size;
}
