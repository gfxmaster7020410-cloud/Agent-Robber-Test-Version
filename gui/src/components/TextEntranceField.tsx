import {useEffect, useRef, useState, type ClipboardEvent, type DragEvent, type FormEvent, type KeyboardEvent} from 'react';
import {cn} from '../lib/cn';
import {insertAtCursor, isHugePaste, offloadPaste, sanitizePaste} from '../lib/pasteBuffer';

const EMPTY_HEIGHT = 40;
const MAX_HEIGHT = 180;
const BINARY_RE = /\0/;
const TEXT_TYPES = /^(text\/|application\/(json|xml|javascript|x-sh|toml|yaml|x-yaml|sql))/i;
const TEXT_EXT =
	/\.(py|ts|tsx|js|jsx|mjs|cjs|json|md|txt|css|html|htm|toml|ya?ml|log|csv|xml|rs|go|sql|sh|bat|ps1|ini|cfg|env|svg)$/i;

export interface TextEntranceFieldProps {
	value: string;
	onChange: (value: string) => void;
	onSubmit: (value: string) => void;
	placeholder?: string;
	className?: string;
	ariaLabel?: string;
	disabled?: boolean;
	onKeyDown?: (event: KeyboardEvent<HTMLTextAreaElement>) => boolean | void;
}

function fitHeight(el: HTMLTextAreaElement, empty: boolean): void {
	el.style.height = `${EMPTY_HEIGHT}px`;
	if (empty) {
		return;
	}
	el.style.height = `${Math.min(MAX_HEIGHT, Math.max(EMPTY_HEIGHT, el.scrollHeight))}px`;
}

function speechCtor(): (new () => SpeechRecognitionLike) | null {
	const w = window as Window & {
		SpeechRecognition?: new () => SpeechRecognitionLike;
		webkitSpeechRecognition?: new () => SpeechRecognitionLike;
	};
	return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

interface SpeechRecognitionLike {
	continuous: boolean;
	interimResults: boolean;
	lang: string;
	onresult: ((event: {results: ArrayLike<ArrayLike<{transcript: string}>>}) => void) | null;
	onerror: (() => void) | null;
	onend: (() => void) | null;
	start: () => void;
	stop: () => void;
}

function isTextFile(file: File): boolean {
	if (TEXT_TYPES.test(file.type)) {
		return true;
	}
	if (file.type.startsWith('image/') || file.type.startsWith('audio/') || file.type.startsWith('video/')) {
		return false;
	}
	return TEXT_EXT.test(file.name) || file.type === '';
}

async function fileToSnippet(file: File): Promise<string> {
	if (file.size > 8_000_000) {
		return `[Attached file too large: ${file.name} (${file.size} bytes)]`;
	}
	if (!isTextFile(file)) {
		return `[Attached file: ${file.name} (${file.size} bytes)]`;
	}
	const raw = await file.text();
	if (BINARY_RE.test(raw)) {
		return `[Attached binary: ${file.name} (${file.size} bytes)]`;
	}
	const chunk = sanitizePaste(raw);
	if (isHugePaste(chunk)) {
		const offload = await offloadPaste(chunk);
		return `### Attached ${file.name}\n${offload?.marker ?? `[Context File Attached: ${file.name}]`}`;
	}
	const fence = fenceLang(file.name);
	return `### Attached ${file.name}\n\`\`\`${fence}\n${chunk}\n\`\`\``;
}

function fenceLang(name: string): string {
	const ext = name.split('.').pop()?.toLowerCase() ?? '';
	if (ext === 'py') {
		return 'python';
	}
	if (ext === 'md') {
		return 'markdown';
	}
	if (ext === 'ts' || ext === 'tsx') {
		return 'typescript';
	}
	if (ext === 'js' || ext === 'mjs' || ext === 'cjs') {
		return 'javascript';
	}
	return ext && ext.length <= 8 ? ext : '';
}

export function TextEntranceField({
	value,
	onChange,
	onSubmit,
	placeholder,
	className,
	ariaLabel,
	disabled,
	onKeyDown,
}: TextEntranceFieldProps) {
	const ref = useRef<HTMLTextAreaElement>(null);
	const fileRef = useRef<HTMLInputElement>(null);
	const recRef = useRef<SpeechRecognitionLike | null>(null);
	const valueRef = useRef(value);
	const pastingRef = useRef(false);
	const [listening, setListening] = useState(false);
	const [dropping, setDropping] = useState(false);
	const voiceOk = Boolean(speechCtor());
	valueRef.current = value;

	useEffect(() => {
		const el = ref.current;
		if (el) {
			fitHeight(el, value.length === 0);
		}
	}, [value]);

	useEffect(() => {
		return () => {
			recRef.current?.stop();
		};
	}, []);

	useEffect(() => {
		if (!disabled) {
			return;
		}
		recRef.current?.stop();
		setListening(false);
	}, [disabled]);

	function writeValue(next: string): void {
		valueRef.current = next;
		onChange(next);
	}

	async function attachFiles(files: FileList | File[]): Promise<void> {
		const list = Array.from(files);
		if (list.length === 0 || disabled) {
			return;
		}
		const parts: string[] = [];
		for (const file of list) {
			parts.push(await fileToSnippet(file));
		}
		const blob = parts.join('\n\n');
		const current = valueRef.current;
		writeValue(current.trim() ? `${current.trimEnd()}\n\n${blob}` : blob);
		ref.current?.focus();
	}

	function toggleVoice(): void {
		if (disabled) {
			return;
		}
		const Ctor = speechCtor();
		if (!Ctor) {
			return;
		}
		if (listening) {
			recRef.current?.stop();
			setListening(false);
			return;
		}
		const rec = new Ctor();
		rec.continuous = true;
		rec.interimResults = false;
		rec.lang = navigator.language || 'en-US';
		rec.onresult = event => {
			const last = event.results[event.results.length - 1];
			const said = last?.[0]?.transcript?.trim();
			if (!said) {
				return;
			}
			const current = valueRef.current;
			const gap = current && !current.endsWith(' ') && !current.endsWith('\n') ? ' ' : '';
			writeValue(`${current}${gap}${said}`);
		};
		rec.onerror = () => {
			setListening(false);
		};
		rec.onend = () => {
			setListening(false);
			recRef.current = null;
		};
		recRef.current = rec;
		try {
			rec.start();
			setListening(true);
		} catch {
			setListening(false);
		}
	}

	async function handlePaste(event: ClipboardEvent<HTMLTextAreaElement>): Promise<void> {
		const files = event.clipboardData?.files;
		if (files && files.length > 0) {
			event.preventDefault();
			await attachFiles(files);
			return;
		}
		const raw = event.clipboardData?.getData('text') ?? '';
		const chunk = sanitizePaste(raw);
		if (!chunk) {
			return;
		}
		event.preventDefault();
		pastingRef.current = true;
		const el = event.currentTarget;
		const start = el.selectionStart ?? value.length;
		const end = el.selectionEnd ?? value.length;
		const next = insertAtCursor(value, chunk, start, end);
		try {
			if (isHugePaste(chunk) || isHugePaste(next)) {
				const offload = await offloadPaste(isHugePaste(chunk) ? chunk : next);
				if (offload) {
					const prefix = isHugePaste(chunk) ? insertAtCursor(value, '', start, end).trimEnd() : '';
					const marked = prefix ? `${prefix}\n${offload.marker}` : offload.marker;
					onChange(marked);
					return;
				}
			}
			onChange(next);
		} finally {
			window.setTimeout(() => {
				pastingRef.current = false;
			}, 0);
		}
	}

	function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>): void {
		const handled = onKeyDown?.(event);
		if (handled === true || event.defaultPrevented) {
			return;
		}
		if (event.key !== 'Enter') {
			return;
		}
		if (
			event.shiftKey ||
			event.altKey ||
			event.repeat ||
			event.nativeEvent.isComposing ||
			pastingRef.current
		) {
			return;
		}
		event.preventDefault();
		const line = value.trim();
		if (line && !disabled) {
			onSubmit(line);
		}
	}

	function onDragOver(event: DragEvent<HTMLDivElement>): void {
		if (![...event.dataTransfer.types].includes('Files')) {
			return;
		}
		event.preventDefault();
		setDropping(true);
	}

	return (
		<div
			className={cn('text-entrance-shell', dropping && 'is-drop')}
			onDragOver={onDragOver}
			onDragLeave={() => setDropping(false)}
			onDrop={event => {
				event.preventDefault();
				setDropping(false);
				void attachFiles(event.dataTransfer.files);
			}}
		>
			<textarea
				ref={ref}
				className={cn('text-entrance', className)}
				value={value}
				placeholder={placeholder}
				aria-label={ariaLabel}
				disabled={disabled}
				rows={1}
				spellCheck={false}
				autoComplete="off"
				autoCorrect="off"
				wrap="soft"
				onPaste={handlePaste}
				onInput={(event: FormEvent<HTMLTextAreaElement>) => {
					fitHeight(event.currentTarget, event.currentTarget.value.length === 0);
				}}
				onChange={event => onChange(event.target.value)}
				onKeyDown={handleKeyDown}
			/>
			<div className="text-entrance-actions">
				<input
					ref={fileRef}
					type="file"
					multiple
					accept="image/*,video/*,audio/*,.pdf,.doc,.docx,.txt,.md,.json,.csv,.py,.ts,.tsx,.js"
					className="sr-only"
					tabIndex={-1}
					onChange={event => {
						const files = event.currentTarget.files;
						if (files) {
							void attachFiles(files);
						}
						event.currentTarget.value = '';
					}}
				/>
				<button
					type="button"
					className="text-entrance-btn"
					aria-label="Attach files"
					title="Attach files"
					disabled={disabled}
					onClick={() => fileRef.current?.click()}
				>
					<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
						<path
							strokeLinecap="round"
							strokeLinejoin="round"
							d="M21.44 11.05l-8.49 8.49a5.25 5.25 0 01-7.43-7.43l8.84-8.84a3.5 3.5 0 014.95 4.95l-8.84 8.85a1.75 1.75 0 01-2.48-2.48l8.14-8.14"
						/>
					</svg>
				</button>
				<button
					type="button"
					className={cn('text-entrance-btn', listening && 'is-live')}
					aria-label={listening ? 'Stop voice input' : 'Voice input'}
					aria-pressed={listening}
					title={voiceOk ? (listening ? 'Stop listening' : 'Voice input') : 'Voice input is not available in this browser'}
					disabled={disabled || !voiceOk}
					onClick={toggleVoice}
				>
					<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
						<rect x="9" y="3" width="6" height="11" rx="3" />
						<path strokeLinecap="round" d="M5 11a7 7 0 0014 0M12 18v3" />
					</svg>
				</button>
			</div>
		</div>
	);
}
