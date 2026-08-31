import { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { X, Play, Loader2, CheckCircle2, AlertCircle } from 'lucide-react';

interface TestResult {
	ok: boolean;
	latency_ms: number;
	response?: string;
	error?: string;
}

interface ModelOption {
	id: string;
	alias: string;
	model_id: string;
	provider: string;
}

export const TestModal = ({
	isOpen,
	onClose,
	models,
}: {
	isOpen: boolean;
	onClose: () => void;
	models: ModelOption[];
}) => {
	const [selectedModel, setSelectedModel] = useState('');
	const [prompt, setPrompt] = useState('Hello, reply with OK.');
	const [result, setResult] = useState<TestResult | null>(null);
	const [loading, setLoading] = useState(false);

	const handleTest = async () => {
		const target = selectedModel || models[0]?.id;
		if (!target) return;
		setLoading(true);
		setResult(null);
		try {
			const resp = await fetch(`/api/v1/models/${target}/test`, { method: 'POST' });
			const data = await resp.json();
			setResult(data);
		} catch (err) {
			setResult({ ok: false, latency_ms: 0, error: String(err) });
		} finally {
			setLoading(false);
		}
	};

	if (!isOpen) return null;

	return (
		<AnimatePresence>
			<motion.div
				initial={{ opacity: 0 }}
				animate={{ opacity: 1 }}
				exit={{ opacity: 0 }}
				className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm p-4"
				onClick={onClose}
			>
				<motion.div
					initial={{ opacity: 0, scale: 0.95 }}
					animate={{ opacity: 1, scale: 1 }}
					exit={{ opacity: 0, scale: 0.95 }}
					className="w-full max-w-lg bg-[#0B0F17]/95 backdrop-blur-2xl border border-white/10 rounded-2xl shadow-2xl overflow-hidden"
					onClick={(e) => e.stopPropagation()}
				>
					<div className="p-6 border-b border-white/10 flex justify-between items-center">
						<h2 className="text-lg font-bold font-display text-slate-100 uppercase tracking-wider">Quick Test / Diagnostic</h2>
						<button onClick={onClose} className="text-slate-400 hover:text-white"><X size={20} /></button>
					</div>

					<div className="p-6 space-y-4">
						<div>
							<label className="block text-sm font-display text-slate-400 mb-1">Model</label>
							<select
								className="w-full bg-[#1A1F2E] border border-white/10 rounded-lg p-2.5 font-mono font-medium text-sm text-slate-200"
								value={selectedModel}
								onChange={(e) => setSelectedModel(e.target.value)}
							>
								{models.length === 0 && <option value="">No models configured</option>}
								{models.map((m) => (
									<option key={m.id} value={m.id}>
										{m.alias} ({m.provider})
									</option>
								))}
							</select>
						</div>

						<div>
							<label className="block text-sm font-display text-slate-400 mb-1">Test Prompt</label>
							<textarea
								className="w-full bg-[#1A1F2E] border border-white/10 rounded-lg p-2.5 font-mono font-medium text-sm text-slate-200 resize-none"
								rows={3}
								value={prompt}
								onChange={(e) => setPrompt(e.target.value)}
								placeholder="Hello, reply with OK."
							/>
						</div>

						<button
							onClick={handleTest}
							disabled={loading || models.length === 0}
							className="w-full flex items-center justify-center gap-2 py-2.5 rounded-lg bg-cyan-500/20 border border-cyan-500/40 text-cyan-400 font-medium hover:bg-cyan-500/30 disabled:opacity-40 disabled:cursor-not-allowed transition-all"
						>
							{loading ? <Loader2 size={16} className="animate-spin" /> : <Play size={16} />}
							{loading ? 'Sending...' : 'Run Test'}
						</button>

						{result && (
							<div className={`p-4 rounded-lg border ${result.ok ? 'bg-emerald-500/10 border-emerald-500/20' : 'bg-rose-500/10 border-rose-500/20'}`}>
								<div className="flex items-center gap-2 mb-2">
									{result.ok ? (
										<CheckCircle2 size={16} className="text-emerald-400" />
									) : (
										<AlertCircle size={16} className="text-rose-400" />
									)}
									<span className={`text-sm font-medium ${result.ok ? 'text-emerald-400' : 'text-rose-400'}`}>
										{result.ok ? 'Connection Successful' : 'Connection Failed'}
									</span>
									<span className="ml-auto text-xs text-slate-500 font-mono font-bold">{result.latency_ms}ms</span>
								</div>
								{result.ok && result.response && (
									<pre className="text-xs text-slate-300 font-mono whitespace-pre-wrap break-all mt-2 p-3 bg-black/30 rounded border border-white/5">
										{JSON.stringify(result.response, null, 2).slice(0, 1000)}
									</pre>
								)}
								{result.error && (
									<p className="text-xs text-rose-300 font-mono mt-2">{result.error}</p>
								)}
							</div>
						)}
					</div>
				</motion.div>
			</motion.div>
		</AnimatePresence>
	);
};
