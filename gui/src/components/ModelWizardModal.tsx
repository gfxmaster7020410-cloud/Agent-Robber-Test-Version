import {useState} from 'react';
import type {AddModelPayload} from '../lib/types';

interface ModelWizardModalProps {
    onClose: () => void;
    onSubmit: (payload: AddModelPayload) => Promise<boolean>;
}

export function ModelWizardModal({onClose, onSubmit: _onSubmit}: ModelWizardModalProps) {
    const [step, setStep] = useState(1);
    
    // Minimal implementation as requested for component structure
    return (
        <div className="mem0-modal-overlay open">
            <div className="mem0-modal glass-card">
                <h3>Enterprise Model Wizard - Step {step}</h3>
                <div className="cfg-actions">
                    {step > 1 && <button onClick={() => setStep(s => s - 1)}>Back</button>}
                    {step < 3 ? (
                        <button onClick={() => setStep(s => s + 1)}>Continue</button>
                    ) : (
                        <button onClick={onClose}>Finish</button>
                    )}
                </div>
            </div>
        </div>
    );
}
