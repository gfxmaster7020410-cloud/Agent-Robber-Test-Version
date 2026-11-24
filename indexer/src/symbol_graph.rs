//! Cross-file symbol graph: imports, inheritance, and approximate calls.

use std::collections::{HashMap, HashSet, VecDeque};
use std::path::{Path, PathBuf};
use std::time::Instant;

use serde::Serialize;

use crate::ast_parser::{AstParser, FileAst, Lang, MAX_PARSE_BYTES};
use crate::hashutil::sha3_hex_str;
use crate::mmap_search::{slice_lines, MmapSearcher};

const MAX_PARSE_FILES: usize = 12;
const MAX_GRAPH_NODES: usize = 40;
const MAX_SLICE_BYTES: usize = 900;
const MAX_SLICES: usize = 8;
const MAX_CALLSITES: usize = 16;
const DEPTH_CAP: usize = 4;
const SEARCH_HITS: usize = 48;

const STOPWORDS: &[&str] = &[
    "Where", "What", "When", "Why", "How", "The", "This", "That", "These", "Those",
    "Used", "Across", "Codebase", "Instantiated", "And", "For", "With", "From", "Into",
    "Is", "Are", "Was", "Were", "Does", "Did", "Not", "Any", "All",
];

#[derive(Clone, Debug, Serialize)]
pub struct SymbolNode {
    pub id: String,
    pub name: String,
    pub kind: String,
    pub path: String,
    pub line: u32,
    #[serde(default)]
    pub end_line: u32,
    #[serde(default)]
    pub signature: String,
    pub hash: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct SymbolEdge {
    pub from: String,
    pub to: String,
    pub kind: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct SymbolLoc {
    pub id: String,
    pub name: String,
    pub kind: String,
    pub path: String,
    pub line: u32,
    pub signature: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct CallSite {
    pub path: String,
    pub line: u32,
    pub snippet: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct CodeSlice {
    pub path: String,
    pub name: String,
    pub kind: String,
    pub line: u32,
    pub end_line: u32,
    pub signature: String,
    pub snippet: String,
}

/// Compact AST neighborhood returned as JSON to Python.
#[derive(Clone, Debug, Serialize)]
pub struct SymbolContext {
    pub query: String,
    pub depth: usize,
    pub engine: &'static str,
    pub elapsed_ms: f64,
    pub elapsed_us: u128,
    pub definitions: Vec<SymbolLoc>,
    pub callers: Vec<SymbolLoc>,
    pub callees: Vec<SymbolLoc>,
    pub imports: Vec<String>,
    pub heritage: Vec<HeritageLink>,
    pub callsites: Vec<CallSite>,
    pub slices: Vec<CodeSlice>,
    pub edges: Vec<SymbolEdge>,
    pub tokens: usize,
}

#[derive(Clone, Debug, Serialize)]
pub struct HeritageLink {
    pub child: String,
    pub parent: String,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct SymbolGraph {
    pub symbols: Vec<SymbolNode>,
    pub edges: Vec<SymbolEdge>,
    #[serde(skip)]
    by_name: HashMap<String, Vec<usize>>,
    #[serde(skip)]
    by_id: HashMap<String, usize>,
}

impl SymbolGraph {
    pub fn ingest(&mut self, file: &FileAst) {
        for hit in file.all_symbols() {
            let id = format!("{}::{}", file.path, hit.name);
            if self.by_id.contains_key(&id) {
                continue;
            }
            let idx = self.symbols.len();
            self.symbols.push(SymbolNode {
                id: id.clone(),
                name: hit.name.clone(),
                kind: hit.kind.clone(),
                path: file.path.clone(),
                line: hit.line,
                end_line: hit.end_line,
                signature: hit.signature.clone(),
                hash: hit.hash.clone(),
            });
            self.by_id.insert(id, idx);
            self.by_name.entry(hit.name.clone()).or_default().push(idx);
        }
        for (child, parent) in &file.heritage {
            self.push_edge(
                &format!("{}::{}", file.path, child),
                parent,
                "inherit",
                file,
            );
        }
        for (caller, callee) in &file.calls {
            self.push_edge(
                &format!("{}::{}", file.path, caller),
                callee,
                "call",
                file,
            );
        }
        for import in &file.imports {
            let from = format!("{}::__module__", file.path);
            let target = import.module.clone();
            if !self.by_id.contains_key(&from) {
                let idx = self.symbols.len();
                self.symbols.push(SymbolNode {
                    id: from.clone(),
                    name: file.path.clone(),
                    kind: "module".into(),
                    path: file.path.clone(),
                    line: 1,
                    end_line: 1,
                    signature: String::new(),
                    hash: file.file_hash.clone(),
                });
                self.by_id.insert(from.clone(), idx);
            }
            self.edges.push(SymbolEdge {
                from,
                to: target,
                kind: "import".into(),
            });
        }
    }

    fn push_edge(&mut self, from: &str, target_name: &str, kind: &str, file: &FileAst) {
        let to = self
            .resolve(target_name, &file.path)
            .unwrap_or_else(|| target_name.to_string());
        self.edges.push(SymbolEdge {
            from: from.to_string(),
            to,
            kind: kind.to_string(),
        });
    }

    fn resolve(&self, name: &str, prefer_path: &str) -> Option<String> {
        let idxs = self.by_name.get(name)?;
        let mut best = None;
        for &i in idxs {
            let node = &self.symbols[i];
            if node.path == prefer_path {
                return Some(node.id.clone());
            }
            best = Some(node.id.clone());
        }
        best
    }

    /// Link import modules to files whose stem matches the last path segment.
    pub fn link_files(&mut self) {
        let mut stem_to_path: HashMap<String, String> = HashMap::new();
        for node in &self.symbols {
            if node.kind == "module" {
                if let Some(stem) = file_stem(&node.path) {
                    stem_to_path.insert(stem, node.path.clone());
                }
            }
        }
        for edge in &mut self.edges {
            if edge.kind != "import" {
                continue;
            }
            let key = edge
                .to
                .rsplit(['/', '.', '\\'])
                .next()
                .unwrap_or(edge.to.as_str())
                .to_string();
            if let Some(path) = stem_to_path.get(&key) {
                edge.to = format!("{path}::__module__");
            }
        }
    }

    pub fn fingerprint(&self) -> String {
        let mut blob = String::new();
        for node in &self.symbols {
            blob.push_str(&node.hash);
        }
        sha3_hex_str(&blob)
    }

    pub fn mutated_since(&self, previous: &HashMap<String, String>) -> Vec<&SymbolNode> {
        self.symbols
            .iter()
            .filter(|n| previous.get(&n.id).map(|h| h != &n.hash).unwrap_or(true))
            .collect()
    }

    pub fn seed_ids(&self, query: &str) -> Vec<String> {
        let mut out = Vec::new();
        if query.is_empty() {
            return out;
        }
        let suffix = format!("::{query}");
        if let Some(idxs) = self.by_name.get(query) {
            for &i in idxs {
                out.push(self.symbols[i].id.clone());
            }
        }
        for node in &self.symbols {
            if node.name == query || node.id.ends_with(&suffix) {
                out.push(node.id.clone());
            }
        }
        out.sort();
        out.dedup();
        out
    }

    /// Bidirectional BFS over call / inherit / import edges.
    pub fn traverse(&self, seeds: &[String], depth: usize) -> HashSet<String> {
        let mut seen = HashSet::new();
        let mut q = VecDeque::new();
        for seed in seeds {
            if seen.insert(seed.clone()) {
                q.push_back((seed.clone(), 0usize));
            }
        }
        while let Some((id, hop)) = q.pop_front() {
            if hop >= depth {
                continue;
            }
            for edge in &self.edges {
                let nxt = if edge.from == id {
                    Some(edge.to.as_str())
                } else if edge.to == id {
                    Some(edge.from.as_str())
                } else {
                    None
                };
                if let Some(n) = nxt {
                    if seen.insert(n.to_string()) {
                        q.push_back((n.to_string(), hop + 1));
                    }
                    if seen.len() >= MAX_GRAPH_NODES {
                        return seen;
                    }
                }
            }
        }
        seen
    }

    fn loc(&self, id: &str) -> Option<SymbolLoc> {
        let idx = *self.by_id.get(id)?;
        let n = &self.symbols[idx];
        Some(SymbolLoc {
            id: n.id.clone(),
            name: n.name.clone(),
            kind: n.kind.clone(),
            path: n.path.clone(),
            line: n.line,
            signature: n.signature.clone(),
        })
    }
}

fn file_stem(path: &str) -> Option<String> {
    let name = path.rsplit('/').next()?.rsplit('\\').next()?;
    let stem = name.rsplit_once('.').map(|(s, _)| s).unwrap_or(name);
    if stem.is_empty() {
        None
    } else {
        Some(stem.to_string())
    }
}

fn is_stop(tok: &str) -> bool {
    STOPWORDS.iter().any(|s| s.eq_ignore_ascii_case(tok))
}

fn is_ident(tok: &str) -> bool {
    let mut chars = tok.chars();
    match chars.next() {
        Some(c) if c.is_ascii_alphabetic() || c == '_' => {}
        _ => return false,
    }
    chars.all(|c| c.is_ascii_alphanumeric() || c == '_')
}

fn ident_score(tok: &str) -> i32 {
    if tok.is_empty() {
        return -1;
    }
    let n = tok.len() as i32;
    let internals = tok.chars().skip(1).filter(|c| c.is_ascii_uppercase()).count() as i32;
    let first_upper = tok
        .chars()
        .next()
        .map(|c| c.is_ascii_uppercase())
        .unwrap_or(false);
    if first_upper && internals >= 1 {
        return 2000 + n + internals * 10;
    }
    if tok.contains('_') {
        return 800 + n;
    }
    if first_upper
        && tok
            .chars()
            .skip(1)
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
    {
        return 20 + n;
    }
    n
}

fn quoted_ident(raw: &str) -> Option<String> {
    let bytes = raw.as_bytes();
    for (i, open) in bytes.iter().enumerate() {
        if !matches!(open, b'`' | b'\'' | b'"') {
            continue;
        }
        let close = *open;
        if let Some(rel) = bytes[i + 1..].iter().position(|b| *b == close) {
            let inner = &raw[i + 1..i + 1 + rel];
            if is_ident(inner) && !inner.is_empty() {
                return Some(inner.to_string());
            }
        }
    }
    None
}

fn ident_iter(raw: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut cur = String::new();
    for c in raw.chars() {
        if c.is_ascii_alphanumeric() || c == '_' {
            cur.push(c);
        } else if !cur.is_empty() {
            if is_ident(&cur) {
                out.push(std::mem::take(&mut cur));
            } else {
                cur.clear();
            }
        }
    }
    if !cur.is_empty() && is_ident(&cur) {
        out.push(cur);
    }
    out
}

pub fn extract_query_symbol(raw: &str) -> String {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return String::new();
    }
    if let Some(q) = quoted_ident(trimmed) {
        return q;
    }
    if is_ident(trimmed) && !is_stop(trimmed) {
        return trimmed.to_string();
    }
    let mut best = String::new();
    let mut best_score = -1i32;
    for tok in ident_iter(trimmed) {
        if is_stop(&tok) {
            continue;
        }
        let score = ident_score(&tok);
        if score > best_score {
            best_score = score;
            best = tok;
        }
    }
    best
}

fn abs_from_rel(root: &Path, rel: &str) -> PathBuf {
    let mut p = root.to_path_buf();
    for part in rel.split(['/', '\\']) {
        if part.is_empty() || part == "." {
            continue;
        }
        p.push(part);
    }
    p
}

fn prune_snippet(raw: &str, max_bytes: usize) -> String {
    let mut lines: Vec<&str> = raw.lines().collect();
    while let Some(first) = lines.first() {
        let t = first.trim();
        if t.is_empty() || t.starts_with('#') || t.starts_with("//") {
            lines.remove(0);
            continue;
        }
        if t.starts_with("\"\"\"") || t.starts_with("'''") {
            let opener = if t.starts_with("\"\"\"") { "\"\"\"" } else { "'''" };
            let one_line = t.matches(opener).count() >= 2 && t.len() > 6;
            lines.remove(0);
            if !one_line {
                while let Some(next) = lines.first() {
                    let done = next.contains(opener);
                    lines.remove(0);
                    if done {
                        break;
                    }
                }
            }
            continue;
        }
        break;
    }
    let joined = lines.join("\n");
    if joined.len() <= max_bytes {
        return joined;
    }
    let mut end = max_bytes;
    while end > 0 && !joined.is_char_boundary(end) {
        end -= 1;
    }
    joined[..end].to_string()
}

fn estimate_tokens(text: &str) -> usize {
    (text.len() + 3) / 4
}

fn parse_rel(root: &Path, rel: &str, parser: &mut AstParser) -> Option<FileAst> {
    let path = abs_from_rel(root, rel);
    let lang = Lang::from_path(&path)?;
    let mapped = crate::mmap_search::map_or_read(&path)?;
    if mapped.len() as u64 > MAX_PARSE_BYTES {
        return None;
    }
    parser.parse_source(lang, &mapped, rel)
}

/// PyO3-shaped API: compact JSON AST neighborhood for `query_symbol`.
pub fn get_symbol_context(root: &Path, query_symbol: &str, depth: usize) -> String {
    let started = Instant::now();
    let query = extract_query_symbol(query_symbol);
    let depth = depth.min(DEPTH_CAP);
    let empty = |engine: &'static str| {
        let elapsed = started.elapsed();
        let ctx = SymbolContext {
            query: query.clone(),
            depth,
            engine,
            elapsed_ms: elapsed.as_secs_f64() * 1000.0,
            elapsed_us: elapsed.as_micros(),
            definitions: Vec::new(),
            callers: Vec::new(),
            callees: Vec::new(),
            imports: Vec::new(),
            heritage: Vec::new(),
            callsites: Vec::new(),
            slices: Vec::new(),
            edges: Vec::new(),
            tokens: 0,
        };
        serde_json::to_string(&ctx).unwrap_or_else(|_| "{}".into())
    };
    if query.is_empty() {
        return empty("rust");
    }
    let pattern = format!(r"\b{}\b", regex::escape(&query));
    let searcher = MmapSearcher {
        max_hits: SEARCH_HITS,
    };
    let hits = match searcher.search_tree_limited(
        root,
        &pattern,
        query.as_bytes(),
        MAX_PARSE_FILES,
    ) {
        Ok(h) => h,
        Err(_) => return empty("rust"),
    };
    let mut files: Vec<String> = Vec::new();
    for hit in &hits {
        if !files.iter().any(|p| p == &hit.path) {
            files.push(hit.path.clone());
        }
        if files.len() >= MAX_PARSE_FILES {
            break;
        }
    }
    let mut parser = AstParser::new();
    let mut graph = SymbolGraph::default();
    for rel in &files {
        if let Some(ast) = parse_rel(root, rel, &mut parser) {
            graph.ingest(&ast);
        }
    }
    graph.link_files();
    let seeds = graph.seed_ids(&query);
    let keep = graph.traverse(&seeds, depth);
    let seed_set: HashSet<String> = seeds.iter().cloned().collect();

    let mut definitions = Vec::new();
    for id in &seeds {
        if let Some(loc) = graph.loc(id) {
            if loc.kind != "module" {
                definitions.push(loc);
            }
        }
    }
    let mut callers = Vec::new();
    let mut callees = Vec::new();
    let mut heritage = Vec::new();
    let mut imports = Vec::new();
    let mut kept_edges: Vec<SymbolEdge> = Vec::new();
    for edge in &graph.edges {
        let from_keep = keep.contains(&edge.from) || seed_set.contains(&edge.from);
        let to_keep = keep.contains(&edge.to) || seed_set.contains(&edge.to);
        if !from_keep && !to_keep {
            continue;
        }
        if from_keep && to_keep {
            kept_edges.push(edge.clone());
        }
        if edge.kind == "call" {
            if seed_set.contains(&edge.to) || edge.to == query || edge.to.ends_with(&format!("::{query}")) {
                if let Some(loc) = graph.loc(&edge.from) {
                    callers.push(loc);
                }
            }
            if seed_set.contains(&edge.from) {
                if let Some(loc) = graph.loc(&edge.to) {
                    callees.push(loc);
                } else {
                    callees.push(SymbolLoc {
                        id: edge.to.clone(),
                        name: edge.to.clone(),
                        kind: "call".into(),
                        path: String::new(),
                        line: 0,
                        signature: String::new(),
                    });
                }
            }
        }
        if edge.kind == "inherit"
            && (seed_set.contains(&edge.from)
                || seed_set.contains(&edge.to)
                || edge.from.ends_with(&format!("::{query}"))
                || edge.to == query)
        {
            heritage.push(HeritageLink {
                child: edge.from.clone(),
                parent: edge.to.clone(),
            });
        }
        if edge.kind == "import" && (from_keep || to_keep) {
            imports.push(edge.to.clone());
        }
    }
    callers.sort_by(|a, b| a.id.cmp(&b.id));
    callers.dedup_by(|a, b| a.id == b.id);
    callees.sort_by(|a, b| a.id.cmp(&b.id));
    callees.dedup_by(|a, b| a.id == b.id);
    imports.sort();
    imports.dedup();
    callers.truncate(12);
    callees.truncate(12);
    imports.truncate(16);
    heritage.truncate(8);
    kept_edges.truncate(32);

    let def_lines: HashSet<(String, u32)> = definitions
        .iter()
        .map(|d| (d.path.clone(), d.line))
        .collect();
    let mut callsites: Vec<CallSite> = Vec::new();
    for hit in &hits {
        if def_lines.contains(&(hit.path.clone(), hit.line)) {
            continue;
        }
        callsites.push(CallSite {
            path: hit.path.clone(),
            line: hit.line,
            snippet: hit.snippet.clone(),
        });
        if callsites.len() >= MAX_CALLSITES {
            break;
        }
    }

    let mut slices: Vec<CodeSlice> = Vec::new();
    let mut seen_slice = HashSet::new();
    let mut candidates: Vec<&SymbolNode> = graph
        .symbols
        .iter()
        .filter(|n| n.kind != "module" && (seed_set.contains(&n.id) || keep.contains(&n.id)))
        .collect();
    candidates.sort_by_key(|n| {
        (
            if n.name == query { 0u8 } else { 1u8 },
            n.path.clone(),
            n.line,
        )
    });
    for node in candidates {
        let key = format!("{}:{}:{}", node.path, node.line, node.name);
        if !seen_slice.insert(key) {
            continue;
        }
        let end = if node.end_line >= node.line {
            node.end_line
        } else {
            node.line.saturating_add(24)
        };
        let raw = slice_lines(
            &abs_from_rel(root, &node.path),
            node.line,
            end,
            MAX_SLICE_BYTES,
        );
        let snippet = prune_snippet(&raw, MAX_SLICE_BYTES);
        if snippet.trim().is_empty() {
            continue;
        }
        slices.push(CodeSlice {
            path: node.path.clone(),
            name: node.name.clone(),
            kind: node.kind.clone(),
            line: node.line,
            end_line: end,
            signature: node.signature.clone(),
            snippet,
        });
        if slices.len() >= MAX_SLICES {
            break;
        }
    }

    let elapsed = started.elapsed();
    let mut ctx = SymbolContext {
        query,
        depth,
        engine: "rust",
        elapsed_ms: elapsed.as_secs_f64() * 1000.0,
        elapsed_us: elapsed.as_micros(),
        definitions,
        callers,
        callees,
        imports,
        heritage,
        callsites,
        slices,
        edges: kept_edges,
        tokens: 0,
    };
    let mut json = serde_json::to_string(&ctx).unwrap_or_else(|_| "{}".into());
    while json.len() > 12_000 && ctx.slices.len() > 1 {
        ctx.slices.pop();
        json = serde_json::to_string(&ctx).unwrap_or_else(|_| "{}".into());
    }
    ctx.tokens = estimate_tokens(&json);
    serde_json::to_string(&ctx).unwrap_or(json)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ast_parser::{AstParser, Lang};
    use std::fs;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn tracks_call_and_mutation_hash() {
        let src = b"def inner():\n    return 1\ndef outer():\n    return inner()\n";
        let mut p = AstParser::new();
        let ast = p.parse_source(Lang::Python, src, "mod.py").unwrap();
        let mut g = SymbolGraph::default();
        g.ingest(&ast);
        assert!(g.edges.iter().any(|e| e.kind == "call"));
        let fp = g.fingerprint();
        assert_eq!(fp.len(), 64);
        let prev: HashMap<String, String> = g
            .symbols
            .iter()
            .map(|s| (s.id.clone(), s.hash.clone()))
            .collect();
        assert!(g.mutated_since(&prev).is_empty());
        let mut dirty = prev.clone();
        if let Some(v) = dirty.values_mut().next() {
            v.push('x');
        }
        assert!(!g.mutated_since(&dirty).is_empty());
    }

    #[test]
    fn extracts_pascal_symbol_from_sentence() {
        let q = extract_query_symbol(
            "Where is APIKeyRotator instantiated and used across the codebase?",
        );
        assert_eq!(q, "APIKeyRotator");
    }

    #[test]
    fn symbol_context_neighborhood_under_50ms() {
        let n = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!("robber-symctx-{n}"));
        fs::create_dir_all(&dir).unwrap();
        fs::write(
            dir.join("rotator.py"),
            concat!(
                "class APIKeyRotator:\n",
                "    \"\"\"heavy docstring that should be pruned from slices\"\"\"\n",
                "    def __init__(self):\n",
                "        self.keys = []\n",
                "    def next_key(self):\n",
                "        return self.keys[0] if self.keys else \"\"\n",
            ),
        )
        .unwrap();
        fs::write(
            dir.join("user.py"),
            "from rotator import APIKeyRotator\n\ndef boot():\n    rot = APIKeyRotator()\n    return rot.next_key()\n",
        )
        .unwrap();
        let json = get_symbol_context(&dir, "Where is APIKeyRotator instantiated and used across the codebase?", 2);
        fs::remove_dir_all(&dir).ok();
        let v: serde_json::Value = serde_json::from_str(&json).expect("json");
        assert_eq!(v["query"], "APIKeyRotator");
        assert_eq!(v["engine"], "rust");
        let ms = v["elapsed_ms"].as_f64().unwrap_or(999.0);
        assert!(ms < 50.0, "elapsed_ms {ms}");
        let tokens = v["tokens"].as_u64().unwrap_or(9999);
        assert!(tokens < 1500, "tokens {tokens}");
        let defs = v["definitions"].as_array().cloned().unwrap_or_default();
        assert!(
            defs.iter()
                .any(|d| d["name"] == "APIKeyRotator"),
            "missing definition: {json}"
        );
        let callsites = v["callsites"].as_array().cloned().unwrap_or_default();
        let callers = v["callers"].as_array().cloned().unwrap_or_default();
        assert!(
            !callsites.is_empty() || !callers.is_empty(),
            "expected instantiation/use: {json}"
        );
    }
}
