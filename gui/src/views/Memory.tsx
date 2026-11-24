import {useMemo, useState} from 'react';
import type {DaemonState} from '../hooks/useDaemon';
import type {MemoryHit, MemoryNote} from '../lib/types';
import {IconSearch} from '../components/Icons';
import {ContextWindowPanel} from '../components/ContextWindowPanel';
import {formatWindow} from '../lib/contextWindow';

const CATEGORIES = ['all', 'system', 'user', 'session', 'vector', 'tool'] as const;

interface MemoryViewProps {
	daemon: DaemonState | null;
	hits: MemoryHit[];
	onSearch: (text?: string) => boolean;
	onInject: (payload: {
		key: string;
		value: string;
		category: string;
		tags: string[];
	}) => boolean;
	onPurgeNotes: () => boolean;
	onSetWindow?: (window: string | number) => boolean;
}

type Node =
	| {kind: 'note'; note: MemoryNote}
	| {kind: 'hit'; hit: MemoryHit};

export function MemoryView({
	daemon,
	hits,
	onSearch,
	onInject,
	onPurgeNotes,
	onSetWindow,
}: MemoryViewProps) {
	const [query, setQuery] = useState('');
	const [category, setCategory] = useState<(typeof CATEGORIES)[number]>('all');
	const [selected, setSelected] = useState<string | null>(null);
	const [modal, setModal] = useState(false);
	const [form, setForm] = useState({key: '', value: '', category: 'user', tags: ''});
	const notes = daemon?.notes ?? [];

	const nodes = useMemo(() => {
		const list: Node[] = [
			...notes.map(note => ({kind: 'note' as const, note})),
			...hits.map(hit => ({kind: 'hit' as const, hit})),
		];
		return list.filter(node => {
			if (category === 'all') {
				return true;
			}

			if (node.kind === 'note') {
				return node.note.category === category;
			}

			return category === 'vector';
		});
	}, [category, hits, notes]);

	const selectedNode = nodes.find(node => nodeId(node) === selected) ?? null;

	return (
		<div className="view view--fill">
			<div className="mem0-layout">
				<ContextWindowPanel
					compact
					windowTokens={daemon?.contextWindow}
					packedTokens={daemon?.contextPacked}
					hitCount={daemon?.contextHits}
					online={Boolean(daemon)}
					onApply={raw => onSetWindow?.(raw) === true}
				/>
				<div className="mem0-topbar">
					<div className="mem0-topbar__left">
						<div className="mem0-search-wrap">
							<IconSearch />
							<input
								className="mem0-search"
								value={query}
								placeholder="Search vectors, keys, tags..."
								onChange={event => setQuery(event.target.value)}
								onKeyDown={event => {
									if (event.key === 'Enter') {
										onSearch(query);
									}
								}}
							/>
						</div>
					</div>
					<div className="mem0-topbar__center">
						{CATEGORIES.map(item => (
							<button
								key={item}
								type="button"
								className={`mem0-pill${category === item ? ' active' : ''}`}
								onClick={() => setCategory(item)}
							>
								{item === 'all' ? 'All' : item}
							</button>
						))}
					</div>
					<div className="mem0-topbar__right">
						<button type="button" className="mem0-action mem0-action--inject" onClick={() => setModal(true)}>
							<span>Inject Memory</span>
						</button>
						<button type="button" className="mem0-action mem0-action--sync" onClick={() => onSearch('reindex')}>
							<span>Sync SSOT</span>
						</button>
						<button type="button" className="mem0-action mem0-action--purge" onClick={onPurgeNotes}>
							<span>Purge All</span>
						</button>
					</div>
				</div>

				<div className="mem0-body">
					<div className="mem0-left">
						{nodes.length === 0 ? (
							<div className="mem-card">
								<div className="mem-card__key">No nodes yet</div>
								<div className="mem-card__val">
									Search SSOT or inject a note. The TUI agent reads the same index.
								</div>
							</div>
						) : (
							nodes.map(node => {
								const id = nodeId(node);
								if (node.kind === 'note') {
									return (
										<button
											type="button"
											key={id}
											className={`mem-card${selected === id ? ' active-glass-card' : ''}`}
											onClick={() => setSelected(id)}
										>
											<div className="mem-card__row">
												<span className="mem-card__key font-mono font-bold">{node.note.key}</span>
												<span className={`mem-card__cat mem-card__cat--${node.note.category}`}>
													{node.note.category}
												</span>
											</div>
											<div className="mem-card__row2">
												<span className="mem-card__val">{node.note.value}</span>
											</div>
										</button>
									);
								}

								return (
									<button
										type="button"
										key={id}
										className={`mem-card${selected === id ? ' active-glass-card' : ''}`}
										onClick={() => setSelected(id)}
									>
										<div className="mem-card__row">
											<span className="mem-card__key font-mono font-bold">{node.hit.path}</span>
											<span className="mem-card__cat mem-card__cat--vector">vector</span>
										</div>
										<div className="mem-card__row2">
											<span className="mem-card__val">{node.hit.preview}</span>
											<div className="mem-card__confidence">
												<div className="mem-card__conf-bar">
													<div
														className="mem-card__conf-fill"
														style={{width: `${Math.round(node.hit.score * 100)}%`}}
													/>
												</div>
												<span className="mem-card__conf-val font-mono font-bold">{node.hit.score}</span>
											</div>
										</div>
									</button>
								);
							})
						)}
					</div>
					<div className="mem0-right">
						{selectedNode ? (
							<Inspector node={selectedNode} />
						) : (
							<div className="mem0-inspector-empty">
								<span>Select a memory node to inspect</span>
							</div>
						)}
					</div>
				</div>

				<div className="mem0-ribbon">
					<div className="mem0-ribbon__item">
						<span className="mem0-ribbon__lbl font-display">TOTAL VECTORS</span>
						<span className="mem0-ribbon__val font-mono font-bold">{daemon?.ssotChunks ?? 0}</span>
					</div>
					<div className="mem0-ribbon__item">
						<span className="mem0-ribbon__lbl font-display">INDEX FILES</span>
						<span className="mem0-ribbon__val font-mono font-bold">{daemon?.ssotFiles ?? 0}</span>
					</div>
					<div className="mem0-ribbon__item">
						<span className="mem0-ribbon__lbl font-display">WINDOW</span>
						<span className="mem0-ribbon__val font-mono font-bold accent-text">
							{daemon ? formatWindow(daemon.contextWindow) : '1T'}
						</span>
					</div>
					<div className="mem0-ribbon__item">
						<span className="mem0-ribbon__lbl font-display">PACKED</span>
						<span className="mem0-ribbon__val font-mono font-bold">{daemon?.contextPacked ?? 0}</span>
					</div>
					<div className="mem0-ribbon__item">
						<span className="mem0-ribbon__lbl font-display">SSOT SYNC</span>
						<span className="mem0-ribbon__val font-mono font-bold mem0-ribbon__val--active">
							{daemon?.ssotWatching ? 'WATCHING' : daemon ? 'IDLE' : 'OFFLINE'}
						</span>
					</div>
				</div>
			</div>

			<div className={`mem0-modal-overlay${modal ? ' open' : ''}`}>
				<div className="mem0-modal glass-card">
					<div className="card__head">
						<span className="card__title font-display">INJECT NEW MEMORY</span>
						<button type="button" className="mem0-modal-close" onClick={() => setModal(false)}>
							×
						</button>
					</div>
					<div className="card__body" style={{display: 'flex', flexDirection: 'column', gap: 12}}>
						<div className="cfg-field">
							<label>Key</label>
							<input
								className="glass-input"
								value={form.key}
								placeholder="e.g. user_preference"
								onChange={event => setForm(current => ({...current, key: event.target.value}))}
							/>
						</div>
						<div className="cfg-field">
							<label>Value</label>
							<input
								className="glass-input"
								value={form.value}
								placeholder="e.g. prefer plan mode first"
								onChange={event => setForm(current => ({...current, value: event.target.value}))}
							/>
						</div>
						<div className="cfg-field">
							<label>Category</label>
							<select
								className="glass-select"
								value={form.category}
								onChange={event => setForm(current => ({...current, category: event.target.value}))}
							>
								{CATEGORIES.filter(item => item !== 'all').map(item => (
									<option key={item} value={item}>
										{item}
									</option>
								))}
							</select>
						</div>
						<div className="cfg-field">
							<label>Tags (comma separated)</label>
							<input
								className="glass-input"
								value={form.tags}
								placeholder="preference, ui"
								onChange={event => setForm(current => ({...current, tags: event.target.value}))}
							/>
						</div>
						<button
							type="button"
							className="mem0-action mem0-action--inject"
							style={{alignSelf: 'flex-end'}}
							onClick={() => {
								onInject({
									key: form.key,
									value: form.value,
									category: form.category,
									tags: form.tags.split(',').map(tag => tag.trim()).filter(Boolean),
								});
								setForm({key: '', value: '', category: 'user', tags: ''});
								setModal(false);
							}}
						>
							<span>Confirm Inject</span>
						</button>
					</div>
				</div>
			</div>
		</div>
	);
}

function nodeId(node: Node): string {
	return node.kind === 'note' ? `note:${node.note.id}` : `hit:${node.hit.path}:${node.hit.start}`;
}

function Inspector({node}: {node: Node}) {
	if (node.kind === 'note') {
		return (
			<div>
				<div className="mem0-inspector-head">
					<span className="mem0-inspector-uuid">{node.note.id}</span>
					<span className="mem0-inspector-ts">{node.note.category}</span>
				</div>
				<div className="mem0-inspector-section">
					<div className="mem0-inspector-section__title">Value</div>
					<pre className="mem0-inspector-json">{node.note.value}</pre>
				</div>
				<div className="mem0-vector-chips">
					{node.note.tags.map(tag => (
						<span className="mem0-vector-chip" key={tag}>
							{tag}
						</span>
					))}
				</div>
			</div>
		);
	}

	return (
		<div>
			<div className="mem0-inspector-head">
				<span className="mem0-inspector-uuid">{node.hit.path}</span>
				<span className="mem0-inspector-ts">
					{node.hit.start}-{node.hit.end}
				</span>
			</div>
			<div className="mem0-inspector-section">
				<div className="mem0-inspector-section__title">Preview</div>
				<pre className="mem0-inspector-json">{node.hit.preview}</pre>
			</div>
			<div className="mem0-inspector-section">
				<div className="mem0-inspector-section__title">Kind</div>
				<div className="mem0-vector-chips">
					<span className="mem0-vector-chip">{node.hit.kind}</span>
					<span className="mem0-vector-chip">score {node.hit.score}</span>
					<span className="mem0-vector-chip">
						@{node.hit.path}#L{node.hit.start}-L{node.hit.end}
					</span>
				</div>
			</div>
		</div>
	);
}
