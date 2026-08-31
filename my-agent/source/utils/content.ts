export interface ContentSegment {
	type: 'text' | 'code';
	language?: string;
	value: string;
}

const FENCE_PATTERN = /```([a-zA-Z0-9_-]*)\r?\n?([\s\S]*?)```/g;

function pushText(segments: ContentSegment[], value: string): void {
	const trimmed = value.replace(/^\n+|\s+$/g, '');
	if (trimmed.length > 0) {
		segments.push({type: 'text', value: trimmed});
	}
}

export function parseContentSegments(content: string): ContentSegment[] {
	const segments: ContentSegment[] = [];
	let cursor = 0;

	for (const match of content.matchAll(FENCE_PATTERN)) {
		const index = match.index ?? 0;
		if (index > cursor) {
			pushText(segments, content.slice(cursor, index));
		}

		segments.push({
			type: 'code',
			language: match[1] || undefined,
			value: match[2].replace(/\n$/, ''),
		});
		cursor = index + match[0].length;
	}

	if (cursor < content.length) {
		pushText(segments, content.slice(cursor));
	}

	return segments;
}

export function countWrappedLines(text: string, width: number): number {
	const effectiveWidth = Math.max(8, width);
	return text
		.split('\n')
		.reduce(
			(total, line) =>
				total + Math.max(1, Math.ceil(line.length / effectiveWidth)),
			0,
		);
}
