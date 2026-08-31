import { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { CheckCircle2 } from 'lucide-react';

interface StepProps {
  data: any;
  updateData: (data: Partial<any>) => void;
}

// --- Components ---

const ModalWrapper = ({ children, onClose }: { children: React.ReactNode; onClose: () => void }) => (
  <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50 backdrop-blur-sm" onClick={onClose}>
    <motion.div
      initial={{ opacity: 0, scale: 0.95 }}
      animate={{ opacity: 1, scale: 1 }}
      exit={{ opacity: 0, scale: 0.95 }}
      className="w-full max-w-2xl bg-[#0B0F17]/90 backdrop-blur-2xl border border-white/10 rounded-2xl shadow-2xl overflow-hidden"
      onClick={(e) => e.stopPropagation()}
    >
      {children}
    </motion.div>
  </div>
);

const Step1 = ({ data, updateData }: StepProps) => (
  <div className="space-y-4">
    <h3 className="text-xl font-bold text-slate-100 uppercase tracking-wider">Select Provider</h3>
    <div className="grid grid-cols-3 gap-3">
      {['OpenAI', 'Anthropic', 'DeepSeek', 'Gemini', 'Groq', 'Custom'].map((p) => (
        <button
          key={p}
          onClick={() => updateData({ provider: p })}
          className={`p-4 rounded-xl border transition-all ${data.provider === p ? 'bg-cyan-500/20 border-cyan-500/50' : 'bg-white/5 border-white/10 hover:bg-white/10'}`}
        >
          {p}
        </button>
      ))}
    </div>
    <div className="space-y-2">
      <label className="block text-sm text-slate-400">Protocol</label>
      <select className="w-full bg-[#1A1F2E] border border-white/10 rounded-lg p-2 font-mono tabular-nums" value={data.protocol} onChange={(e) => updateData({ protocol: e.target.value })}>
        <option value="OPENAI_COMPATIBLE">OpenAI Chat Completions</option>
        <option value="ANTHROPIC_COMPATIBLE">Anthropic Messages</option>
        <option value="CUSTOM_HTTP">Custom HTTP</option>
      </select>
    </div>
  </div>
);

const Step2 = ({ data, updateData }: StepProps) => (
  <div className="space-y-4 font-mono tabular-nums">
    <label className="block text-sm text-slate-400">Host / Base URL</label>
    <input className="w-full bg-[#1A1F2E] border border-white/10 rounded-lg p-2" value={data.baseUrl} onChange={(e) => updateData({ baseUrl: e.target.value })} />
    <label className="block text-sm text-slate-400">API Key</label>
    <input type="password" className="w-full bg-[#1A1F2E] border border-white/10 rounded-lg p-2" placeholder="vault://..." />
  </div>
);

const Step3 = ({ data, updateData }: StepProps) => (
  <div className="space-y-4 font-mono tabular-nums">
    <label className="block text-sm text-slate-400">Model ID</label>
    <input className="w-full bg-[#1A1F2E] border border-white/10 rounded-lg p-2" value={data.modelId} onChange={(e) => updateData({ modelId: e.target.value })} />
    <div className="flex gap-4">
      <label className="flex items-center gap-2"><input type="checkbox" /> Streaming</label>
      <label className="flex items-center gap-2"><input type="checkbox" /> Vision</label>
    </div>
  </div>
);

const Step4 = ({ data: _data }: StepProps) => (
  <div className="space-y-4">
    <h3 className="text-xl font-bold text-slate-100 uppercase tracking-wider">Finalize</h3>
    <div className="p-4 bg-green-500/10 border border-green-500/20 rounded-lg flex items-center gap-2 text-green-400">
      <CheckCircle2 size={20} /> Endpoint Reachable & Valid
    </div>
  </div>
);

export const ModelConfigModal = ({ isOpen, onClose }: { isOpen: boolean; onClose: () => void }) => {
  const [step, setStep] = useState(1);
  const [data, setData] = useState({ provider: 'OpenAI', protocol: 'OPENAI_COMPATIBLE', baseUrl: 'https://api.openai.com/v1', modelId: 'gpt-4o' });

  if (!isOpen) return null;

  return (
    <ModalWrapper onClose={onClose}>
      <div className="p-6 border-b border-white/10 flex justify-between items-center">
        <h2 className="text-lg font-bold text-slate-100 uppercase tracking-wider">Model Configuration</h2>
        <span className="text-slate-500 font-mono tabular-nums">Step {step} of 4</span>
      </div>
      
      <div className="p-6 min-h-[300px]">
        <AnimatePresence mode="wait">
          <motion.div key={step} initial={{ opacity: 0, x: 10 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -10 }}>
            {step === 1 && <Step1 data={data} updateData={(d) => setData({...data, ...d})} />}
            {step === 2 && <Step2 data={data} updateData={(d) => setData({...data, ...d})} />}
            {step === 3 && <Step3 data={data} updateData={(d) => setData({...data, ...d})} />}
            {step === 4 && <Step4 data={data} updateData={(d) => setData({...data, ...d})} />}
          </motion.div>
        </AnimatePresence>
      </div>

      <div className="p-6 border-t border-white/10 flex justify-end gap-3">
        <button onClick={onClose} className="px-4 py-2 rounded-lg text-slate-400 hover:text-white">Cancel</button>
        {step > 1 && <button onClick={() => setStep(step - 1)} className="px-4 py-2 rounded-lg bg-white/5 border border-white/10">Back</button>}
        {step < 4 ? (
          <button onClick={() => setStep(step + 1)} className="px-4 py-2 rounded-lg bg-cyan-500/10 border border-cyan-500/40 text-cyan-400 hover:bg-cyan-500/20">Next</button>
        ) : (
          <button className="px-4 py-2 rounded-lg bg-cyan-500/10 border border-cyan-500/40 text-cyan-400 hover:bg-cyan-500/20">Save & Enable</button>
        )}
      </div>
    </ModalWrapper>
  );
};
