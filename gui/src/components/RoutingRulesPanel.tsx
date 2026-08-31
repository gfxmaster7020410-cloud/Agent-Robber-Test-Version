export const RoutingRulesPanel = () => (
  <div className="bg-[#0B0F17]/80 backdrop-blur-xl border border-white/10 rounded-2xl p-6">
    <h3 className="font-bold text-slate-100 uppercase tracking-wider mb-6 text-sm">Ranking Formula</h3>
    
    <div className="bg-[#1A1F2E] p-4 rounded-xl font-mono text-xs text-cyan-400 mb-8 border border-white/5 flex justify-center items-center gap-2">
      Score = <span className="text-slate-300">(P * w_p) + (H * w_h) + (L * w_l) - Cost</span>
    </div>

    <div className="grid grid-cols-2 gap-6 text-[11px] font-mono tabular-nums text-slate-400 mb-8">
      <label className="space-y-2">Priority Weight (w_p) <input type="range" className="w-full accent-cyan-500" /></label>
      <label className="space-y-2">Latency Weight (w_l) <input type="range" className="w-full accent-cyan-500" /></label>
    </div>

    <div className="pt-6 border-t border-white/10">
      <h4 className="font-bold text-slate-100 uppercase tracking-wider text-[10px] mb-4">Fallback Chain Preview</h4>
      <div className="flex items-center gap-2 font-mono text-[10px] text-slate-500">
        <div className="bg-cyan-500/10 text-cyan-400 px-3 py-1 rounded border border-cyan-500/20">Primary</div>
        <span>→</span>
        <div className="bg-amber-500/10 text-amber-400 px-3 py-1 rounded border border-amber-500/20">Fallback</div>
        <span>→</span>
        <div className="bg-rose-500/10 text-rose-400 px-3 py-1 rounded border border-rose-500/20">Emergency</div>
      </div>
    </div>
  </div>
);
