export const FONT_FAMILY = 'Base Neue';

export type InkTextTreatment =
	| 'headline'
	| 'emphasis'
	| 'regular'
	| 'muted';

export interface TypeToken {
	cssWeight: 300 | 400 | 500 | 700;
	trackingCss: 'normal' | '0.02em';
	inkTreatment: InkTextTreatment;
}

const HEADLINE: TypeToken = {
	cssWeight: 700,
	trackingCss: 'normal',
	inkTreatment: 'headline',
};

const STATUS: TypeToken = {
	cssWeight: 500,
	trackingCss: 'normal',
	inkTreatment: 'emphasis',
};

const BODY: TypeToken = {
	cssWeight: 400,
	trackingCss: 'normal',
	inkTreatment: 'regular',
};

const METADATA: TypeToken = {
	cssWeight: 300,
	trackingCss: 'normal',
	inkTreatment: 'muted',
};

const MICRO: TypeToken = {
	cssWeight: 300,
	trackingCss: '0.02em',
	inkTreatment: 'muted',
};

export const typography = {
	headline: HEADLINE,
	status: STATUS,
	body: BODY,
	metadata: METADATA,
	micro: MICRO,
} as const;

export type TypeRole = keyof typeof typography;

export function resolveType(role: TypeRole): TypeToken {
	return typography[role];
}

export interface InkTypeProps {
	bold?: boolean;
	dimColor?: boolean;
}

export function inkProps(role: TypeRole): InkTypeProps {
	const {inkTreatment} = resolveType(role);
	if (inkTreatment === 'headline' || inkTreatment === 'emphasis') {
		return {bold: true};
	}

	if (inkTreatment === 'muted') {
		return {dimColor: true};
	}

	return {};
}
