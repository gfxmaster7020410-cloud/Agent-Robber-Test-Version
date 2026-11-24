import {useCallback, useEffect, useRef, type RefObject, type UIEvent} from 'react';

const EDGE = 48;

export function useAutoScroll<T extends HTMLElement>(deps: unknown[]): {
	ref: RefObject<T>;
	onScroll: (event: UIEvent<T>) => void;
} {
	const ref = useRef<T>(null);
	const pinned = useRef(true);

	const onScroll = useCallback((event: UIEvent<T>) => {
		const node = event.currentTarget;
		pinned.current = node.scrollHeight - node.scrollTop - node.clientHeight <= EDGE;
	}, []);

	useEffect(() => {
		const node = ref.current;
		if (!node || !pinned.current) {
			return;
		}
		node.scrollTop = node.scrollHeight;
	}, deps);

	return {ref, onScroll};
}
