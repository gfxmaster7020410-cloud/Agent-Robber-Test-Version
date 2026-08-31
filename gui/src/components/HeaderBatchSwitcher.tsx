import type {BatchClass} from '../lib/types';

interface HeaderBatchSwitcherProps {
	value: BatchClass;
	onChange: (batch: BatchClass) => void;
}

const TIERS: readonly {id: BatchClass; label: string; title: string}[] = [
	{id: 'eco', label: 'Eco', title: 'Fast / cheap pool'},
	{id: 'normal', label: 'Balanced', title: 'Mid-tier daily routing'},
	{id: 'heavy', label: 'Best', title: 'Frontier / max quality'},
];

export function HeaderBatchSwitcher({value, onChange}: HeaderBatchSwitcherProps) {
	return (
		<div className="batch-switch" role="group" aria-label="Routing tier">
			{TIERS.map(tier => (
				<button
					key={tier.id}
					type="button"
					className={`batch-switch__btn batch-switch__btn--${tier.id}${value === tier.id ? ' is-active' : ''}`}
					title={tier.title}
					onClick={() => onChange(tier.id)}
				>
					{tier.label.toUpperCase()}
				</button>
			))}
		</div>
	);
}
