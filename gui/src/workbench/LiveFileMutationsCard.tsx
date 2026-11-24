import type {FileMutation} from './stream';

export function LiveFileMutationsCard({files}: {files?: FileMutation[]}) {
	const rows = files ?? [];
	return (
		<section className="wb-mutations" aria-label="Active file mutations">
			<h2 className="wb-mutations__title">
				Creating and updating {rows.length} files in project root:
			</h2>
			{rows.length === 0 ? (
				<p className="wb-mutations__empty">No file mutations this session</p>
			) : (
				<ul className="wb-mutations__list">
					{rows.map(file => (
						<li key={`${file.marker}-${file.path}`} className="wb-mutations__row">
							<span className="wb-mutations__path">
								[{file.marker}] {file.path}
							</span>
							<span className="wb-mutations__detail">{file.detail}</span>
						</li>
					))}
				</ul>
			)}
		</section>
	);
}
