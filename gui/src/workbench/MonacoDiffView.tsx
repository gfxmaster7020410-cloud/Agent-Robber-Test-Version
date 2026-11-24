import {lazy, Suspense, useRef} from 'react';
import type {FileCardPayload} from '../lib/types';
import './monacoSetup';
import {languageFor} from './stream';

const MonacoDiff = lazy(() =>
	import('@monaco-editor/react').then(mod => ({default: mod.DiffEditor})),
);
const MonacoEditor = lazy(() =>
	import('@monaco-editor/react').then(mod => ({default: mod.Editor})),
);

interface MonacoDiffViewProps {
	file: FileCardPayload;
	height?: number;
}

function applyTheme(monaco: typeof import('monaco-editor')): void {
	monaco.editor.defineTheme('robber-dark', {
		base: 'vs-dark',
		inherit: true,
		rules: [],
		colors: {
			'editor.background': '#232330',
			'editor.foreground': '#e8e8ed',
			'diffEditor.insertedTextBackground': '#22c55e33',
			'diffEditor.removedTextBackground': '#ef444433',
			'diffEditor.insertedLineBackground': '#22c55e22',
			'diffEditor.removedLineBackground': '#ef444422',
		},
	});
}

const OPTIONS = {
	readOnly: true,
	minimap: {enabled: false},
	scrollBeyondLastLine: false,
	fontFamily: 'BogitaMono, ui-monospace, Consolas, monospace',
	fontSize: 12,
	fontWeight: '400',
	automaticLayout: true,
	renderSideBySide: true,
	wordWrap: 'off' as const,
	scrollbar: {
		horizontal: 'auto' as const,
		vertical: 'auto' as const,
		horizontalScrollbarSize: 4,
		verticalScrollbarSize: 4,
	},
};

export function MonacoDiffView({file, height = 320}: MonacoDiffViewProps) {
	const themed = useRef(false);
	const language = file.language || languageFor(file.path);
	const create = file.action === 'create';

	function onBeforeMount(monaco: typeof import('monaco-editor')): void {
		if (!themed.current) {
			applyTheme(monaco);
			themed.current = true;
		}
	}

	return (
		<div className="wb-log overflow-hidden bg-wb-card" style={{height}}>
			<Suspense fallback={<FallbackDiff file={file} height={height} />}>
				{create ? (
					<MonacoEditor
						height={height}
						language={language}
						theme="robber-dark"
						value={file.after || ''}
						beforeMount={onBeforeMount}
						options={OPTIONS}
					/>
				) : (
					<MonacoDiff
						height={height}
						language={language}
						theme="robber-dark"
						original={file.before || ''}
						modified={file.after || ''}
						beforeMount={onBeforeMount}
						options={OPTIONS}
					/>
				)}
			</Suspense>
		</div>
	);
}

function FallbackDiff({file, height}: {file: FileCardPayload; height: number}) {
	const left = (file.before || '').split('\n');
	const right = (file.after || '').split('\n');
	const rows = Math.max(left.length, right.length, 1);
	return (
		<div className="wb-code-scroll grid grid-cols-2 gap-px bg-wb-border font-mono text-[12px]" style={{height}}>
			<div className="bg-wb-card p-2 text-wb-red-text">
				{Array.from({length: rows}, (_, index) => (
					<div key={`l${index}`} className="whitespace-pre">
						{left[index] ? `- ${left[index]}` : ' '}
					</div>
				))}
			</div>
			<div className="bg-wb-card p-2 text-wb-green-text">
				{Array.from({length: rows}, (_, index) => (
					<div key={`r${index}`} className="whitespace-pre">
						{right[index] ? `+ ${right[index]}` : ' '}
					</div>
				))}
			</div>
		</div>
	);
}
