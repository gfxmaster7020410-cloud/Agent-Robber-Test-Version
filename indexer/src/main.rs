use std::env;
use std::path::PathBuf;
use std::process::ExitCode;

use robber_indexer::Indexer;

fn flag(args: &[String], name: &str) -> Option<String> {
    args.windows(2).find_map(|w| {
        if w[0] == name {
            Some(w[1].clone())
        } else {
            None
        }
    })
}

fn usage() -> ExitCode {
    eprintln!(
        "robber-indexer index --root PATH [--ssot PATH]\n\
         robber-indexer search --root PATH --pattern REGEX\n\
         robber-indexer context --root PATH --symbol NAME [--depth N]"
    );
    ExitCode::from(2)
}

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    let cmd = args.first().map(String::as_str).unwrap_or("");
    match cmd {
        "index" => {
            let root = flag(&args, "--root").unwrap_or_else(|| ".".into());
            let ssot = flag(&args, "--ssot").unwrap_or_else(|| {
                let mut p = PathBuf::from(&root);
                p.push(".robber");
                p.push("ssot.db");
                p.to_string_lossy().into_owned()
            });
            match Indexer::new(&root).index_into(std::path::Path::new(&ssot)) {
                Ok(report) => {
                    println!("{}", serde_json::to_string(&report).unwrap_or_else(|_| "{}".into()));
                    ExitCode::SUCCESS
                }
                Err(e) => {
                    eprintln!("{e}");
                    ExitCode::from(1)
                }
            }
        }
        "search" => {
            let root = flag(&args, "--root").unwrap_or_else(|| ".".into());
            let pattern = match flag(&args, "--pattern") {
                Some(p) => p,
                None => return usage(),
            };
            match Indexer::new(&root).search(&pattern, 64) {
                Ok(hits) => {
                    println!("{}", serde_json::to_string(&hits).unwrap_or_else(|_| "[]".into()));
                    ExitCode::SUCCESS
                }
                Err(e) => {
                    eprintln!("{e}");
                    ExitCode::from(1)
                }
            }
        }
        "context" => {
            let root = flag(&args, "--root").unwrap_or_else(|| ".".into());
            let query = flag(&args, "--symbol")
                .or_else(|| flag(&args, "--query"))
                .unwrap_or_default();
            if query.is_empty() {
                return usage();
            }
            let depth: usize = flag(&args, "--depth")
                .and_then(|s| s.parse().ok())
                .unwrap_or(2);
            println!("{}", Indexer::new(&root).get_symbol_context(&query, depth));
            ExitCode::SUCCESS
        }
        _ => usage(),
    }
}
