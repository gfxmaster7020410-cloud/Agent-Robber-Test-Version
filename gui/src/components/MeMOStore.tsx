import { useState, useEffect, type MouseEvent } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { IconSearch } from '../components/Icons';
import { ContextWindowPanel } from './ContextWindowPanel';
import type { DaemonState } from '../hooks/useDaemon';

// Local inline SVG Icons to prevent import breakage and ensure 100% self-containment
function IconCopy({ size = 16, checked = false }) {
    return checked ? (
        <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="#00ff88" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="20 6 9 17 4 12" />
        </svg>
    ) : (
        <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
            <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
        </svg>
    );
}

function IconEdit({ size = 16 }) {
    return (
        <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
            <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
        </svg>
    );
}

function IconTrash({ size = 16 }) {
    return (
        <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="3 6 5 6 21 6" />
            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
        </svg>
    );
}

// Initial Mock Vector Data
const INITIAL_MOCK_CHUNKS = Array.from({ length: 12 }, (_, i) => ({
    id: `chk_${100 + i}`,
    content: `Lorem ipsum dolor sit amet, consectetur adipiscing elit. Memory vector space preview ${i}...`,
    raw: `Full raw text metadata for vectorized memory chunk ${100 + i}...`,
    tag: ['System', 'UI', 'User Prefs', 'Scripts'][i % 4],
    tokens: 45 + (i * 12),
    date: new Date(Date.now() - i * 100000000).toLocaleDateString(),
    metadata: JSON.stringify({ source: "CLI Daemon", priority: i % 2 === 0 ? "high" : "low" }, null, 2)
}));

const API_BASE_URL = 'http://127.0.0.1:8787/api/memo';

interface MemoChunk {
	id: string;
	content: string;
	raw: string;
	tag: string;
	tokens: number;
	date: string;
	metadata?: string;
}

interface MemoToast {
	id: number;
	message: string;
	type: string;
}

interface MeMOStoreInspectorProps {
	daemon?: DaemonState | null;
	online?: boolean;
	onSetWindow?: (window: string | number) => boolean;
}

export function MeMOStoreInspector({
	daemon,
	online = false,
	onSetWindow,
}: MeMOStoreInspectorProps) {
    const [chunks, setChunks] = useState<MemoChunk[]>(() => {
        const stored = localStorage.getItem('robber_memo_chunks');
        if (!stored) {
            return INITIAL_MOCK_CHUNKS;
        }

        try {
            const parsed: unknown = JSON.parse(stored);
            return Array.isArray(parsed) ? (parsed as MemoChunk[]) : INITIAL_MOCK_CHUNKS;
        } catch {
            return INITIAL_MOCK_CHUNKS;
        }
    });

    const [search, setSearch] = useState('');
    const [selectedTag, setSelectedTag] = useState('All');
    const [sortBy, setSortBy] = useState('Newest');
    const [expandedId, setExpandedId] = useState<string | null>(null);
    const [copiedId, setCopiedId] = useState<string | null>(null);

    // Modals State
    const [isAddOpen, setIsAddOpen] = useState(false);
    const [isEditOpen, setIsEditOpen] = useState(false);
    const [isPurgeOpen, setIsPurgeOpen] = useState(false);

    // Form States
    const [formContent, setFormContent] = useState('');
    const [formTag, setFormTag] = useState('System');
    const [formMetadata, setFormMetadata] = useState('{\n  "source": "Manual Entry"\n}');
    const [editingChunkId, setEditingChunkId] = useState<string | null>(null);

    // Toast Notifications State
    const [toasts, setToasts] = useState<MemoToast[]>([]);

    // Persist local state
    useEffect(() => {
        localStorage.setItem('robber_memo_chunks', JSON.stringify(chunks));
    }, [chunks]);

    // Fetch initial list from API
    useEffect(() => {
        const fetchChunks = async () => {
            try {
                const response = await fetch(`${API_BASE_URL}/list`);
                if (response.ok) {
                    const data = await response.json();
                    if (Array.isArray(data) && data.length > 0) {
                        setChunks(data as MemoChunk[]);
                        showToast('Connected to MeMO Store API', 'success');
                    }
                }
            } catch {
                showToast('API Offline — Loaded Offline Cache', 'warn');
            }
        };
        fetchChunks();
    }, []);

    const showToast = (message: string, type = 'success') => {
        const id = Date.now();
        setToasts(prev => [...prev, { id, message, type }]);
        setTimeout(() => {
            setToasts(prev => prev.filter(t => t.id !== id));
        }, 3000);
    };

    // Copy to clipboard helper
    const handleCopy = (id: string, text: string, event: MouseEvent) => {
        event.stopPropagation(); // Avoid expanding card
        navigator.clipboard.writeText(text);
        setCopiedId(id);
        showToast('Copied raw chunk to clipboard', 'success');
        setTimeout(() => setCopiedId(null), 2000);
    };

    // Add Chunk Operation
    const handleAddChunk = async () => {
        if (!formContent.trim()) {
            showToast('Chunk content cannot be empty', 'error');
            return;
        }

        let parsedMeta = {};
        try {
            parsedMeta = JSON.parse(formMetadata);
        } catch {
            showToast('Invalid JSON metadata format', 'error');
            return;
        }

        const newChunk = {
            id: `chk_${Date.now().toString().slice(-4)}`,
            content: formContent.slice(0, 100) + (formContent.length > 100 ? '...' : ''),
            raw: formContent,
            tag: formTag,
            tokens: Math.ceil(formContent.length / 4),
            date: new Date().toLocaleDateString(),
            metadata: JSON.stringify(parsedMeta, null, 2)
        };

        // Try API
        try {
            const res = await fetch(`${API_BASE_URL}/add`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(newChunk)
            });
            if (res.ok) {
                const added = (await res.json()) as MemoChunk;
                setChunks(prev => [added, ...prev]);
                showToast('Chunk synchronized with active Vector index', 'success');
            } else {
                throw new Error('Fallback to local');
            }
        } catch {
            // Local fallback
            setChunks(prev => [newChunk, ...prev]);
            showToast('Added chunk locally (Offline)', 'success');
        }

        // Reset
        setFormContent('');
        setFormMetadata('{\n  "source": "Manual Entry"\n}');
        setIsAddOpen(false);
    };

    // Edit Chunk Trigger
    const openEditModal = (chunk: MemoChunk, event: MouseEvent) => {
        event.stopPropagation();
        setEditingChunkId(chunk.id);
        setFormContent(chunk.raw);
        setFormTag(chunk.tag);
        setFormMetadata(chunk.metadata || '{\n  "source": "Manual Entry"\n}');
        setIsEditOpen(true);
    };

    // Save Edit Chunk Operation
    const handleEditChunk = async () => {
        if (!formContent.trim()) {
            showToast('Chunk content cannot be empty', 'error');
            return;
        }

        let parsedMeta = {};
        try {
            parsedMeta = JSON.parse(formMetadata);
        } catch {
            showToast('Invalid JSON metadata format', 'error');
            return;
        }

        const updated = {
            content: formContent.slice(0, 100) + (formContent.length > 100 ? '...' : ''),
            raw: formContent,
            tag: formTag,
            tokens: Math.ceil(formContent.length / 4),
            metadata: JSON.stringify(parsedMeta, null, 2)
        };

        try {
            const res = await fetch(`${API_BASE_URL}/update/${editingChunkId}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(updated)
            });
            if (res.ok) {
                const data = (await res.json()) as MemoChunk;
                setChunks(prev => prev.map(c => c.id === editingChunkId ? data : c));
                showToast('Chunk updated in Vector index', 'success');
            } else {
                throw new Error('Fallback');
            }
        } catch {
            setChunks(prev => prev.map(c => c.id === editingChunkId ? { ...c, ...updated } : c));
            showToast('Chunk updated locally (Offline)', 'success');
        }

        setIsEditOpen(false);
    };

    // Delete Chunk Operation
    const handleDeleteChunk = async (id: string, event: MouseEvent) => {
        event.stopPropagation();
        try {
            const res = await fetch(`${API_BASE_URL}/delete/${id}`, { method: 'DELETE' });
            if (res.ok) {
                setChunks(prev => prev.filter(c => c.id !== id));
                showToast('Chunk deleted from Vector index', 'success');
            } else {
                throw new Error();
            }
        } catch {
            setChunks(prev => prev.filter(c => c.id !== id));
            showToast('Chunk deleted locally (Offline)', 'success');
        }
    };

    // Purge Store Operation
    const handlePurgeStore = async () => {
        try {
            const res = await fetch(`${API_BASE_URL}/purge`, { method: 'POST' });
            if (res.ok) {
                setChunks([]);
                showToast('Vector index cleared completely', 'success');
            } else {
                throw new Error();
            }
        } catch {
            setChunks([]);
            showToast('Local cache cleared completely', 'success');
        }
        setIsPurgeOpen(false);
    };

    // Filters and sorting logic
    const filteredChunks = chunks
        .filter(c => {
            const matchesSearch = c.raw.toLowerCase().includes(search.toLowerCase()) || c.id.toLowerCase().includes(search.toLowerCase());
            const matchesTag = selectedTag === 'All' || c.tag === selectedTag;
            return matchesSearch && matchesTag;
        })
        .sort((a, b) => {
            if (sortBy === 'Tokens') return b.tokens - a.tokens;
            // Default newest / chronological logic
            return b.id.localeCompare(a.id);
        });

    const totalTokens = chunks.reduce((acc, c) => acc + c.tokens, 0);

    return (
        <div className="view view--scroll" style={{ position: 'relative' }}>
            {/* TOAST SYSTEM */}
            <div className="toast-container">
                {toasts.map(t => (
                    <div key={t.id} className={`toast toast--${t.type}`}>
                        <div className="toast__dot" />
                        <span>{t.message}</span>
                    </div>
                ))}
            </div>

            <ContextWindowPanel
                compact
                windowTokens={daemon?.contextWindow}
                packedTokens={daemon?.contextPacked}
                hitCount={daemon?.contextHits}
                online={online}
                onApply={raw => onSetWindow?.(raw) === true}
            />

            {/* 1. Stats Bar */}
            <div className="bento-grid" style={{ marginBottom: '20px' }}>
                <div className="glass-card" style={{ padding: '20px' }}>
                    <div className="stat-val font-mono font-bold">{chunks.length}</div>
                    <div className="stat-lbl font-display">Total Chunks</div>
                </div>
                <div className="glass-card" style={{ padding: '20px' }}>
                    <div className="stat-val font-mono font-bold accent-text">Synced</div>
                    <div className="stat-lbl font-display">Store Status</div>
                </div>
                <div className="glass-card" style={{ padding: '20px' }}>
                    <div className="stat-val font-mono font-bold" style={{ color: 'var(--accent2)' }}>{totalTokens.toLocaleString()}</div>
                    <div className="stat-lbl font-display">Memory Tokens</div>
                </div>
                <div className="glass-card" style={{ padding: '20px', display: 'flex', gap: '10px', alignItems: 'center', justifyContent: 'center' }}>
                    <button type="button" className="mem0-action mem0-action--inject" onClick={() => setIsAddOpen(true)}>
                        Add New Chunk
                    </button>
                    <button type="button" className="mem0-action mem0-action--purge" onClick={() => setIsPurgeOpen(true)}>
                        Purge Store
                    </button>
                </div>
            </div>

            {/* 2. Filters */}
            <div className="mem0-topbar" style={{ borderRadius: 'var(--radius)', marginBottom: '16px' }}>
                <div className="mem0-search-wrap" style={{ flex: 1 }}>
                    <IconSearch />
                    <input className="mem0-search" placeholder="Search memory blocks..." value={search} onChange={e => setSearch(e.target.value)} />
                </div>
                <select className="glass-select" style={{ width: '150px' }} value={selectedTag} onChange={e => setSelectedTag(e.target.value)}>
                    <option value="All">All Tags</option>
                    <option value="System">System</option>
                    <option value="UI">UI</option>
                    <option value="User Prefs">User Prefs</option>
                    <option value="Scripts">Scripts</option>
                </select>
                <select className="glass-select" style={{ width: '120px' }} value={sortBy} onChange={e => setSortBy(e.target.value)}>
                    <option value="Newest">Newest</option>
                    <option value="Tokens">Token Size</option>
                </select>
            </div>

            {/* 3. Grid Display */}
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: '16px' }}>
                <AnimatePresence>
                    {filteredChunks.map(chunk => (
                        <motion.div
                            key={chunk.id}
                            layout
                            initial={{ opacity: 0, scale: 0.95 }}
                            animate={{ opacity: 1, scale: 1 }}
                            exit={{ opacity: 0, scale: 0.95 }}
                            className="glass-card"
                            style={{ cursor: 'pointer' }}
                            onClick={() => setExpandedId(expandedId === chunk.id ? null : chunk.id)}
                        >
                            <div className="card__head">
                                <span className="card__title font-mono font-bold">{chunk.id}</span>
                                <span className="card__badge">{chunk.tag}</span>
                            </div>
                            <div className="card__body">
                                <p style={{ fontSize: '12px', color: 'var(--text-2)' }}>{chunk.content}</p>
                                <AnimatePresence>
                                    {expandedId === chunk.id && (
                                        <motion.div
                                            initial={{ height: 0, opacity: 0 }}
                                            animate={{ height: 'auto', opacity: 1 }}
                                            exit={{ height: 0, opacity: 0 }}
                                            style={{ marginTop: '10px', paddingTop: '10px', borderTop: '1px solid var(--border)', overflow: 'hidden' }}
                                        >
                                            <div style={{ marginBottom: '8px' }}>
                                                <span style={{ fontSize: '10px', color: 'var(--text-3)' }}>Raw Text:</span>
                                                <pre style={{ fontSize: '11px', background: 'rgba(0,0,0,0.2)', padding: '8px', borderRadius: '4px', whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
                                                    {chunk.raw}
                                                </pre>
                                            </div>
                                            {chunk.metadata && (
                                                <div style={{ marginBottom: '12px' }}>
                                                    <span style={{ fontSize: '10px', color: 'var(--text-3)' }}>Metadata:</span>
                                                    <pre style={{ fontSize: '10px', background: 'rgba(0,0,0,0.3)', padding: '8px', borderRadius: '4px', color: 'var(--accent2)', overflowX: 'auto' }}>
                                                        {chunk.metadata}
                                                    </pre>
                                                </div>
                                            )}
                                            <div style={{ display: 'flex', gap: '8px' }}>
                                                <button type="button" className="ghost-btn" onClick={(e) => handleCopy(chunk.id, chunk.raw, e)}>
                                                    <IconCopy size={14} checked={copiedId === chunk.id} />
                                                </button>
                                                <button type="button" className="ghost-btn" onClick={(e) => openEditModal(chunk, e)}>
                                                    <IconEdit size={14} />
                                                </button>
                                                <button type="button" className="ghost-btn" style={{ color: 'var(--purge-text)' }} onClick={(e) => handleDeleteChunk(chunk.id, e)}>
                                                    <IconTrash size={14} />
                                                </button>
                                            </div>
                                        </motion.div>
                                    )}
                                </AnimatePresence>
                            </div>
                            <div className="card__head" style={{ padding: '8px 18px', fontSize: '10px', background: 'rgba(0,0,0,0.1)' }}>
                                <span>{chunk.tokens} tokens</span>
                                <span>{chunk.date}</span>
                            </div>
                        </motion.div>
                    ))}
                </AnimatePresence>
            </div>

            {/* ADD CHUNK MODAL */}
            <div className={`mem0-modal-overlay${isAddOpen ? ' open' : ''}`}>
                <div className="mem0-modal glass-card">
                    <div className="card__head">
                        <span className="card__title font-display">Add Memory Chunk</span>
                        <button type="button" className="mem0-modal-close" onClick={() => setIsAddOpen(false)}>×</button>
                    </div>
                    <div className="card__body" style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                        <div className="cfg-field">
                            <label>Raw Text Content</label>
                            <textarea className="glass-textarea" value={formContent} onChange={e => setFormContent(e.target.value)} placeholder="Enter information to be vectorized..." />
                        </div>
                        <div className="cfg-field">
                            <label>Tag Category</label>
                            <select className="glass-select" value={formTag} onChange={e => setFormTag(e.target.value)}>
                                <option value="System">System</option>
                                <option value="UI">UI</option>
                                <option value="User Prefs">User Prefs</option>
                                <option value="Scripts">Scripts</option>
                            </select>
                        </div>
                        <div className="cfg-field">
                            <label>Metadata (JSON)</label>
                            <textarea className="glass-textarea" style={{ fontFamily: 'var(--mono)', fontSize: '12px' }} value={formMetadata} onChange={e => setFormMetadata(e.target.value)} />
                        </div>
                        <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end', marginTop: '8px' }}>
                            <button type="button" className="mem0-action" onClick={() => setIsAddOpen(false)}>Cancel</button>
                            <button type="button" className="mem0-action mem0-action--inject" onClick={handleAddChunk}>Confirm Inject</button>
                        </div>
                    </div>
                </div>
            </div>

            {/* EDIT CHUNK MODAL */}
            <div className={`mem0-modal-overlay${isEditOpen ? ' open' : ''}`}>
                <div className="mem0-modal glass-card">
                    <div className="card__head">
                        <span className="card__title font-display">Edit Memory Chunk ({editingChunkId})</span>
                        <button type="button" className="mem0-modal-close" onClick={() => setIsEditOpen(false)}>×</button>
                    </div>
                    <div className="card__body" style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                        <div className="cfg-field">
                            <label>Raw Text Content</label>
                            <textarea className="glass-textarea" value={formContent} onChange={e => setFormContent(e.target.value)} />
                        </div>
                        <div className="cfg-field">
                            <label>Tag Category</label>
                            <select className="glass-select" value={formTag} onChange={e => setFormTag(e.target.value)}>
                                <option value="System">System</option>
                                <option value="UI">UI</option>
                                <option value="User Prefs">User Prefs</option>
                                <option value="Scripts">Scripts</option>
                            </select>
                        </div>
                        <div className="cfg-field">
                            <label>Metadata (JSON)</label>
                            <textarea className="glass-textarea" style={{ fontFamily: 'var(--mono)', fontSize: '12px' }} value={formMetadata} onChange={e => setFormMetadata(e.target.value)} />
                        </div>
                        <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end', marginTop: '8px' }}>
                            <button type="button" className="mem0-action" onClick={() => setIsEditOpen(false)}>Cancel</button>
                            <button type="button" className="mem0-action mem0-action--inject" onClick={handleEditChunk}>Save Changes</button>
                        </div>
                    </div>
                </div>
            </div>

            {/* PURGE STORE CONFIRMATION */}
            <div className={`mem0-modal-overlay${isPurgeOpen ? ' open' : ''}`}>
                <div className="mem0-modal glass-card" style={{ width: '380px' }}>
                    <div className="card__head" style={{ borderBottomColor: 'rgba(239, 68, 68, 0.2)' }}>
                        <span className="card__title font-display" style={{ color: 'var(--purge-text)' }}>Purge Memory Store?</span>
                        <button type="button" className="mem0-modal-close" onClick={() => setIsPurgeOpen(false)}>×</button>
                    </div>
                    <div className="card__body" style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
                        <p style={{ fontSize: '12.5px', color: 'var(--text-2)', lineHeight: '1.6' }}>
                            Are you absolutely sure you want to purge the store? This action resets active local cache and wipes synchronized vector chunks completely from the daemon jail.
                        </p>
                        <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
                            <button type="button" className="mem0-action" onClick={() => setIsPurgeOpen(false)}>Cancel</button>
                            <button type="button" className="mem0-action mem0-action--purge" onClick={handlePurgeStore}>Confirm Purge</button>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    );
}
