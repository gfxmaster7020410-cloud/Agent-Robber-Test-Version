import {lazy, Suspense, useState} from 'react';
import {Prism as SyntaxHighlighter} from 'react-syntax-highlighter';
import type {FileCardPayload} from '../lib/types';
import {
	miniDiffFromCode,
	miniDiffFromFile,
	type MiniDiff,
} from '../lib/miniDiff';
import {HIGHLIGHT, prismLang} from '../workbench/syntaxTheme';

const MonacoDiffView = lazy(async () => {
	const mod = await import('../workbench/MonacoDiffView');
	return {default: mod.MonacoDiffView};
});

interface FileDiffCardProps {
	model?: MiniDiff | null;
	file?: FileCardPayload;
	code?: string;
	language?: string;
	path?: string;
	defaultExpanded?: boolean;
	monacoHeight?: number;
}

export function FileDiffCard({
	model,
	file,
	code,
	language,
	path,
	defaultExpanded = false,
	monacoHeight = 320,
}: FileDiffCardProps) {
	const resolved =
		model ||
		(file ? miniDiffFromFile(file) : null) ||
		(code ? miniDiffFromCode(code, language, path) : null);
	const [expanded, setExpanded] = useState(defaultExpanded);
	const [loaded, setLoaded] = useState(defaultExpanded);
	if (!resolved) {
		return null;
	}
	const highlighterLang = prismLang(looksLikeDiffBody(resolved.full) ? 'diff' : resolved.language);
	const showFull = expanded || loaded;

	function toggle(): void {
		setExpanded(open => {
			const next = !open;
			if (next) {
				setLoaded(true);
			}
			return next;
		});
	}

	return (
		<article className={`fdc border border-neutral-700 bg-neutral-900 ${expanded ? 'is-open' : ''}`}>
			<button
				type="button"
				className="fdc-head"
				aria-expanded={expanded}
				onClick={toggle}
			>
				<span className="fdc-chevron" aria-hidden>
					{expanded ? 'v' : '>'}
				</span>
				<span className="fdc-path" title={resolved.path}>
					[{resolved.marker}] {resolved.path}
				</span>
				<span className="fdc-stats">
					<span className="fdc-add">+{resolved.added}</span>{' '}
					<span className="fdc-del">-{resolved.removed}</span>
				</span>
			</button>
			<div className={`fdc-fold ${expanded ? '' : 'is-open'}`}>
				<div className="fdc-fold-inner">
					<div className="fdc-preview" onClick={() => { setLoaded(true); setExpanded(true); }} role="presentation">
						{resolved.preview.map((line, index) => (
							<div
								key={`${line.kind}-${index}`}
								className={`fdc-preview-line is-${line.kind}`}
							>
								{line.text || '\u00a0'}
							</div>
						))}
					</div>
				</div>
			</div>
			<div className={`fdc-fold ${expanded ? 'is-open' : ''}`}>
				<div className="fdc-fold-inner">
					{showFull && file ? (
						<Suspense fallback={<div className="fdc-loading">Loading diff…</div>}>
							<MonacoDiffView file={file} height={monacoHeight} />
						</Suspense>
					) : showFull ? (
						<div className="fdc-full">
							<SyntaxHighlighter
								language={highlighterLang}
								style={HIGHLIGHT}
								showLineNumbers
								wrapLongLines
								PreTag="pre"
								CodeTag="code"
								lineNumberStyle={{color: '#6b6b78', minWidth: '2.4em', paddingRight: '12px'}}
								customStyle={{
									width: '100%',
									maxWidth: '100%',
									margin: 0,
									background: '#171717',
									padding: '12px 16px',
								}}
							>
								{resolved.full || ' '}
							</SyntaxHighlighter>
						</div>
					) : null}
				</div>
			</div>
		</article>
	);
}

function looksLikeDiffBody(text: string): boolean {
	return /^(diff --git |index |--- a\/|\+\+\+ b\/|@@ )/m.test(text || '');
}
