import { RefreshCw } from 'lucide-react';

export const SecretsVaultPanel = () => {
  const credentials = [
    { label: 'Primary OpenAI', preset: 'OpenAI', masked: 'sk-omni-...9xA', vault: 'vault://agent-robber/openai', hash: 'a1b2c3d4...' },
    { label: 'Backup Anthropic', preset: 'Anthropic', masked: 'sk-cla-...5bY', vault: 'vault://agent-robber/anthropic', hash: 'e5f6g7h8...' },
  ];

  return (
    <div className="bg-[#0B0F17]/80 backdrop-blur-xl border border-white/10 rounded-2xl p-6">
      <h2 className="text-lg font-bold font-display text-slate-100 uppercase tracking-wider mb-6">Secrets Vault</h2>
      <table className="w-full text-xs font-mono tabular-nums">
        <thead>
          <tr className="text-slate-500 border-b border-white/10">
            <th className="text-left py-3">Label</th>
            <th className="text-left py-3">Reference</th>
            <th className="text-left py-3">Fingerprint</th>
            <th className="text-right py-3">Actions</th>
          </tr>
        </thead>
        <tbody>
          {credentials.map((c, i) => (
            <tr key={i} className="border-b border-white/5 text-slate-300">
              <td className="py-4">{c.label}</td>
              <td className="py-4">{c.vault}</td>
              <td className="py-4">{c.hash}</td>
              <td className="py-4 text-right">
                <button className="text-cyan-400 hover:text-cyan-300"><RefreshCw size={16} /></button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};
