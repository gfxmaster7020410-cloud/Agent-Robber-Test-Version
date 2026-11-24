import {Prism as SyntaxHighlighter} from 'react-syntax-highlighter';
import {FileDiffCard} from '../components/FileDiffCard';
import {parseContentSegments} from '../lib/content';
import {
	looksLikeFileDiff,
	looksLikeUnifiedDiff,
	miniDiffFromCode,
	miniDiffFromUnified,
	writePathFromText,
} from '../lib/miniDiff';
import {stripThoughts} from '../lib/thoughts';
import {parseFileCard} from './stream';
import {HIGHLIGHT, prismLang} from './syntaxTheme';

function diffClass(line: string): string {
	if (/^(diff |index |--- |\+\+\+ |@@ )/.test(line)) {
		return 'wb-diff-meta';
	}
	if (line.startsWith('+')) {
		return 'wb-diff-add';
	}
	if (line.startsWith('-')) {
		return 'wb-diff-del';
	}
	return '';
}

export function StreamMarkdown({text}: {text: string}) {
	const cleaned = stripThoughts(text);
	const trimmed = cleaned.trim();
	if (trimmed.startsWith('{')) {
		const file = parseFileCard(trimmed);
		if (file) {
			return (
				<div className="wb-log__body wb-log__body--md">
					<FileDiffCard file={file} />
				</div>
			);
		}
	}
	const segments = parseContentSegments(cleaned);
	if (segments.length === 0) {
		return <div className="wb-log__body">{cleaned}</div>;
	}
	let writePath: string | undefined;
	return (
		<div className="wb-log__body wb-log__body--md">
			{segments.map((segment, index) => {
				if (segment.type === 'code') {
					const path = segment.path || writePath;
					const asCard = looksLikeFileDiff(segment.value, segment.language, path);
					const model = asCard ? miniDiffFromCode(segment.value, segment.language, path) : null;
					if (model) {
						writePath = undefined;
						return <FileDiffCard key={index} model={model} />;
					}
					const lang = prismLang(segment.language || '');
					return (
						<div key={index} className="wb-log__code-wrap">
							<SyntaxHighlighter
								language={lang}
								style={HIGHLIGHT}
								wrapLongLines
								PreTag="pre"
								CodeTag="code"
								customStyle={{
									width: '100%',
									maxWidth: '100%',
									margin: 0,
									background: '#1e1e1e',
								}}
							>
								{segment.value || ' '}
							</SyntaxHighlighter>
						</div>
					);
				}
				writePath = writePathFromText(segment.value) || writePath;
				if (looksLikeUnifiedDiff(segment.value)) {
					return <FileDiffCard key={index} model={miniDiffFromUnified(segment.value)} />;
				}
				return (
					<div key={index} className="wb-md-prose">
						{segment.value.split('\n').map((line, lineIndex) => {
							if (/^#{4,6}\s+/.test(line)) {
								return (
									<div key={lineIndex} className="wb-md-h4">
										{line.replace(/^#{4,6}\s+/, '')}
									</div>
								);
							}
							if (/^#{1,3}\s+/.test(line)) {
								return (
									<div key={lineIndex} className="wb-md-h">
										{line.replace(/^#{1,3}\s+/, '')}
									</div>
								);
							}
							const diff = diffClass(line);
							return (
								<div key={lineIndex} className={diff ? `wb-md-p ${diff}` : 'wb-md-p'}>
									{line || '\u00a0'}
								</div>
							);
						})}
					</div>
				);
			})}
		</div>
	);
}
