import { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { X, Save } from 'lucide-react';

const TABS = ['General', 'Capabilities', 'Routing', 'Interception', 'Security'];

export const ModelEditDrawer = ({ isOpen, onClose, model }: { isOpen: boolean; onClose: () => void; model: any }) => {
  const [activeTab, setActiveTab] = useState(0);

  return (
    <AnimatePresence>
      {isOpen && (
        <>
          <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} className="fixed inset-0 bg-black/40 backdrop-blur-sm z-40" />
          <motion.div 
            initial={{ x: '100%' }} animate={{ x: 0 }} exit={{ x: '100%' }}
            className="fixed right-0 top-0 h-full w-[600px] bg-[#0B0F17]/95 backdrop-blur-xl border-l border-white/10 z-50 p-8 shadow-2xl flex flex-col"
          >
            <div className="flex justify-between items-center mb-8">
              <h2 className="text-xl font-bold text-slate-100 uppercase tracking-wider">{model?.name || 'New Model'}</h2>
              <button onClick={onClose} className="text-slate-400 hover:text-white"><X /></button>
            </div>

            <div className="flex gap-1 mb-8 border-b border-white/5 pb-2">
              {TABS.map((tab, i) => (
                <button 
                  key={tab} 
                  onClick={() => setActiveTab(i)}
                  className={`px-4 py-2 text-[11px] uppercase tracking-wider transition-all ${activeTab === i ? 'text-cyan-400 border-b border-cyan-500' : 'text-slate-500 hover:text-slate-300'}`}
                >{tab}</button>
              ))}
            </div>

            <div className="flex-1 overflow-y-auto space-y-6 font-mono tabular-nums text-sm">
              {activeTab === 0 && (
                <div className="space-y-4">
                  <input className="w-full bg-white/5 border border-white/10 p-3 rounded-lg text-slate-100" placeholder="Model Alias" />
                  <input className="w-full bg-white/5 border border-white/10 p-3 rounded-lg text-slate-100" placeholder="Base URL" />
                </div>
              )}
              {/* Other tabs would go here ... */}
              <div className="text-slate-500 italic p-8 text-center border border-dashed border-white/10 rounded-lg">Tab {TABS[activeTab]} content placeholder</div>
            </div>

            <div className="pt-6 border-t border-white/10 flex justify-end gap-3">
              <button onClick={onClose} className="px-6 py-2 text-slate-400 hover:text-white">Cancel</button>
              <button className="px-6 py-2 bg-cyan-500/10 border border-cyan-500/40 text-cyan-400 rounded-lg flex items-center gap-2 hover:bg-cyan-500/20">
                <Save size={16} /> Save Changes
              </button>
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
};
