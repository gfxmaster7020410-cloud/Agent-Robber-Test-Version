export const SettingsPanel = () => {
  return (
    <div className="bg-[#0B0F17]/80 backdrop-blur-xl border border-white/10 rounded-2xl p-6">
      <h2 className="text-lg font-bold font-display text-slate-100 uppercase tracking-wider mb-8">System Settings</h2>
      
      <div className="space-y-6">
        <label className="flex items-center justify-between">
          <span className="text-sm text-slate-300">Custom Header Allowlist</span>
          <input type="checkbox" className="accent-cyan-500" />
        </label>
        
        <label className="flex items-center justify-between">
          <span className="text-sm text-slate-300">SSRF Local Isolation</span>
          <input type="checkbox" className="accent-cyan-500" defaultChecked />
        </label>
      </div>
    </div>
  );
};
