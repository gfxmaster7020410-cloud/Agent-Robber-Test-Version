import type {BatchClass} from '../lib/types';
import {BATCH_OPTIONS, batchLabel} from '../lib/providers';

interface BatchSwitchProps {
	value: BatchClass;
	onChange: (batch: BatchClass) => void;
}

export function BatchSwitch({value, onChange}: BatchSwitchProps) {
	return (
		<div className="batch-switch" role="group" aria-label="Workload batch">
			{BATCH_OPTIONS.map(option => (
				<button
					key={option.id}
					type="button"
					className={`batch-switch__btn batch-switch__btn--${option.id}${value === option.id ? ' is-active' : ''}`}
					title={option.blurb}
					onClick={() => onChange(option.id)}
				>
					{batchLabel(option.id).toUpperCase()}
				</button>
			))}
		</div>
	);
}
