import type {ChatMessage} from '../lib/types';
import type {BusyPhase} from '../hooks/useDaemon';
import {FileDiffCard} from '../components/FileDiffCard';
import type {LayoutMode} from './stream';
import {toStreamSteps} from './stream';
import {StreamMarkdown} from './StreamMarkdown';
import {useAutoScroll} from './useAutoScroll';

interface MainWorkspaceProps {
	messages: ChatMessage[];
	busy: BusyPhase | null;
	layout: LayoutMode;
}

export function MainWorkspace({messages, busy, layout}: MainWorkspaceProps) {
	const steps = toStreamSteps(messages);
	const fingerprint = steps.map(step => step.id).join('|') + (busy?.label ?? '');
	const {ref, onScroll} = useAutoScroll<HTMLDivElement>([fingerprint, layout]);
	const diffHeight = layout === 'diff' ? 420 : layout === 'wide' ? 360 : 320;

	return (
		<div ref={ref} onScroll={onScroll} className="wb-stream">
			{steps.length === 0 ? (
				<div className="flex min-h-[240px] w-full flex-col justify-center gap-5">
					<QuietStep label="Initiating" title="Waiting for a prompt" />
					<QuietStep label="Writing" title="File writes land here as cards + diffs" />
					<QuietStep label="Terminal" title="Shell output streams into this column" />
				</div>
			) : (
				<div className="flex w-full flex-col gap-4 pb-8">
					{steps.map(step => (
						<section key={step.id} className="flex w-full flex-col gap-1.5">
							<div className={`wb-step-head ${step.error ? 'is-error' : step.kind === 'initiating' ? 'is-initiating' : ''}`}>
								<StepStar />
								<span>{step.label}</span>
							</div>
							{step.file ? (
									<div className="wb-log w-full overflow-hidden bg-transparent !p-0">
										<div className="wb-log__meta">{step.title}</div>
										<FileDiffCard file={step.file} monacoHeight={diffHeight} />
									</div>
							) : (
								<div className={`wb-log w-full ${step.error ? 'is-error' : ''}`}>
									<StreamBody step={step} />
								</div>
							)}
						</section>
					))}
					{busy ? (
						<section className="flex w-full flex-col gap-1">
							<div className="wb-step-head">
								<StepStar />
								<span>{busy.label}</span>
							</div>
							{busy.detail ? (
								<div className="wb-log w-full">
									<div className="wb-log__body text-wb-muted">{busy.detail}</div>
								</div>
							) : null}
						</section>
					) : null}
				</div>
			)}
		</div>
	);
}

function StreamBody({step}: {step: ReturnType<typeof toStreamSteps>[number]}) {
	const text = step.body || step.title || '';
	if (/```|^#{1,6}\s/m.test(text)) {
		return <StreamMarkdown text={text} />;
	}
	if (step.ansi && step.ansi.length > 0) {
		return (
			<div className="wb-log__body">
				{step.ansi.map((span, index) => (
					<span key={`${step.id}-${index}`} style={span.color ? {color: span.color} : undefined}>
						{span.text}
					</span>
				))}
			</div>
		);
	}
	return <StreamMarkdown text={text} />;
}

function QuietStep({label, title}: {label: string; title: string}) {
	return (
		<section className="flex w-full flex-col gap-1.5">
			<div className="wb-step-head">
				<StepStar />
				<span>{label}</span>
			</div>
			<div className="wb-log w-full">
				<div className="wb-log__body text-wb-muted">{title}</div>
			</div>
		</section>
	);
}

function StepStar() {
	return (
		<span className="wb-star" aria-hidden>
			✦
		</span>
	);
}
