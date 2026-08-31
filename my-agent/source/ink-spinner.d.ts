declare module 'ink-spinner' {
	import type {ComponentType} from 'react';

	export interface SpinnerProps {
		type?: string;
	}

	const Spinner: ComponentType<SpinnerProps>;
	export default Spinner;
}
