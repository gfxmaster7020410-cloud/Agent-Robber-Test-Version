//! Multi-language tree-sitter AST extraction (Python, TypeScript, Go, Rust).

use std::path::Path;

use tree_sitter::{Language, Node, Parser, Tree};

use crate::hashutil::sha3_hex;

pub const MAX_PARSE_BYTES: u64 = 1_048_576;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Lang {
    Python,
    TypeScript,
    Tsx,
    JavaScript,
    Go,
    Rust,
}

impl Lang {
    pub fn from_path(path: &Path) -> Option<Self> {
        match path
            .extension()
            .and_then(|e| e.to_str())
            .map(|s| s.to_ascii_lowercase())
            .as_deref()
        {
            Some("py") => Some(Lang::Python),
            Some("ts") => Some(Lang::TypeScript),
            Some("tsx") => Some(Lang::Tsx),
            Some("js") | Some("mjs") | Some("cjs") => Some(Lang::JavaScript),
            Some("jsx") => Some(Lang::Tsx),
            Some("go") => Some(Lang::Go),
            Some("rs") => Some(Lang::Rust),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Lang::Python => "python",
            Lang::TypeScript => "typescript",
            Lang::Tsx => "tsx",
            Lang::JavaScript => "javascript",
            Lang::Go => "go",
            Lang::Rust => "rust",
        }
    }

    pub(crate) fn language(self) -> Language {
        match self {
            Lang::Python => tree_sitter_python::LANGUAGE.into(),
            Lang::TypeScript | Lang::JavaScript => tree_sitter_typescript::LANGUAGE_TYPESCRIPT.into(),
            Lang::Tsx => tree_sitter_typescript::LANGUAGE_TSX.into(),
            Lang::Go => tree_sitter_go::LANGUAGE.into(),
            Lang::Rust => tree_sitter_rust::LANGUAGE.into(),
        }
    }
}

#[derive(Clone, Debug, serde::Serialize)]
pub struct SymbolHit {
    pub name: String,
    pub kind: String,
    pub line: u32,
    pub end_line: u32,
    pub hash: String,
    #[serde(default)]
    pub signature: String,
}

#[derive(Clone, Debug, serde::Serialize)]
pub struct Import {
    pub module: String,
    pub names: Vec<String>,
}

#[derive(Clone, Debug, serde::Serialize, Default)]
pub struct FileAst {
    pub path: String,
    pub language: String,
    pub size_bytes: u64,
    pub file_hash: String,
    pub classes: Vec<SymbolHit>,
    pub functions: Vec<SymbolHit>,
    pub imports: Vec<Import>,
    pub docs: Vec<String>,
    pub heritage: Vec<(String, String)>,
    pub calls: Vec<(String, String)>,
}

impl FileAst {
    pub fn all_symbols(&self) -> impl Iterator<Item = &SymbolHit> {
        self.classes.iter().chain(self.functions.iter())
    }

    pub fn symbol_index(&self) -> String {
        let names: Vec<&str> = self.all_symbols().map(|s| s.name.as_str()).collect();
        let joined = names.join(",");
        clip(&joined, 1500)
    }

    pub fn ast_summary(&self) -> String {
        let payload = serde_json::json!({
            "classes": self.classes.iter().map(|s| &s.name).collect::<Vec<_>>(),
            "functions": self.functions.iter().map(|s| &s.name).collect::<Vec<_>>(),
            "imports": self.imports.iter().map(|i| &i.module).collect::<Vec<_>>(),
            "docs": self.docs,
        });
        clip(&payload.to_string(), 2000)
    }
}

/// Thread-local tree-sitter parsers (Parser is not Sync).
pub struct AstParser {
    parser: Parser,
}

impl AstParser {
    pub fn new() -> Self {
        Self {
            parser: Parser::new(),
        }
    }

    pub fn parse_source(&mut self, lang: Lang, src: &[u8], path: &str) -> Option<FileAst> {
        if src.len() as u64 > MAX_PARSE_BYTES {
            return None;
        }
        self.parser.set_language(&lang.language()).ok()?;
        let tree = self.parser.parse(src, None)?;
        Some(extract(lang, src, &tree, path))
    }

    /// True when tree-sitter reports ERROR or MISSING nodes (pre-apply gate).
    pub fn has_syntax_error(&mut self, lang: Lang, src: &[u8]) -> bool {
        !self.syntax_errors(lang, src).is_empty()
    }

    /// Line-level ERROR / MISSING nodes for in-memory pre-apply validation.
    pub fn syntax_errors(&mut self, lang: Lang, src: &[u8]) -> Vec<AstErrorHit> {
        if src.len() as u64 > MAX_PARSE_BYTES {
            return vec![AstErrorHit {
                line: 1,
                column: 1,
                kind: "ERROR".into(),
                node_type: "overflow".into(),
                snippet: String::new(),
            }];
        }
        if self.parser.set_language(&lang.language()).is_err() {
            return vec![AstErrorHit {
                line: 1,
                column: 1,
                kind: "ERROR".into(),
                node_type: "language".into(),
                snippet: String::new(),
            }];
        }
        match self.parser.parse(src, None) {
            Some(tree) => {
                let mut out = Vec::new();
                collect_error_nodes(tree.root_node(), src, &mut out, 0);
                out
            }
            None => vec![AstErrorHit {
                line: 1,
                column: 1,
                kind: "ERROR".into(),
                node_type: "parse".into(),
                snippet: String::new(),
            }],
        }
    }
}

#[derive(Clone, Debug, serde::Serialize)]
pub struct AstErrorHit {
    pub line: u32,
    pub column: u32,
    pub kind: String,
    pub node_type: String,
    pub snippet: String,
}

fn collect_error_nodes(node: Node, src: &[u8], out: &mut Vec<AstErrorHit>, depth: u32) {
    if out.len() >= 8 || depth > 256 {
        return;
    }
    if node.is_missing() || node.is_error() || node.kind() == "ERROR" {
        let pos = node.start_position();
        let kind = if node.is_missing() { "MISSING" } else { "ERROR" };
        out.push(AstErrorHit {
            line: pos.row as u32 + 1,
            column: pos.column as u32 + 1,
            kind: kind.into(),
            node_type: node.kind().to_string(),
            snippet: error_snippet(src, pos.row),
        });
    }
    if node.has_error() {
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            collect_error_nodes(child, src, out, depth + 1);
        }
    }
}

fn error_snippet(src: &[u8], row: usize) -> String {
    let text = String::from_utf8_lossy(src);
    text.lines()
        .nth(row)
        .unwrap_or("")
        .chars()
        .take(120)
        .collect()
}

impl Default for AstParser {
    fn default() -> Self {
        Self::new()
    }
}

fn extract(lang: Lang, src: &[u8], tree: &Tree, path: &str) -> FileAst {
    let mut out = FileAst {
        path: path.to_string(),
        language: lang.as_str().to_string(),
        size_bytes: src.len() as u64,
        file_hash: sha3_hex(src),
        ..FileAst::default()
    };
    walk(tree.root_node(), src, lang, &mut out, None, 0);
    if out.docs.len() > 3 {
        out.docs.truncate(3);
    }
    out
}

fn walk(
    node: Node,
    src: &[u8],
    lang: Lang,
    out: &mut FileAst,
    current_fn: Option<&str>,
    depth: u32,
) {
    if depth > 256 {
        return;
    }
    let kind = node.kind();
    let mut next_fn = current_fn.map(|s| s.to_string());

    match lang {
        Lang::Python => visit_python(node, src, out, &mut next_fn),
        Lang::TypeScript | Lang::Tsx | Lang::JavaScript => {
            visit_ts(node, src, out, &mut next_fn)
        }
        Lang::Go => visit_go(node, src, out, &mut next_fn),
        Lang::Rust => visit_rust(node, src, out, &mut next_fn),
    }

    if is_call(kind) {
        if let Some(callee) = callee_name(node, src) {
            if let Some(caller) = next_fn.as_deref().or(current_fn) {
                if caller != callee {
                    out.calls.push((caller.to_string(), callee));
                }
            }
        }
    }

    let child_fn = next_fn.as_deref().or(current_fn);
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        walk(child, src, lang, out, child_fn, depth + 1);
    }
}

fn visit_python(node: Node, src: &[u8], out: &mut FileAst, current_fn: &mut Option<String>) {
    match node.kind() {
        "function_definition" => {
            if let Some(hit) = named_symbol(node, src, "function") {
                *current_fn = Some(hit.name.clone());
                maybe_doc(node, src, out);
                out.functions.push(hit);
            }
        }
        "class_definition" => {
            if let Some(hit) = named_symbol(node, src, "class") {
                maybe_doc(node, src, out);
                if let Some(supers) = node.child_by_field_name("superclasses") {
                    for parent in ident_tokens(supers, src) {
                        out.heritage.push((hit.name.clone(), parent));
                    }
                }
                out.classes.push(hit);
            }
        }
        "import_statement" | "import_from_statement" => {
            out.imports.push(import_from_node(node, src));
        }
        _ => {}
    }
}

fn visit_ts(node: Node, src: &[u8], out: &mut FileAst, current_fn: &mut Option<String>) {
    match node.kind() {
        "function_declaration" | "generator_function_declaration" => {
            if let Some(hit) = named_symbol(node, src, "function") {
                *current_fn = Some(hit.name.clone());
                out.functions.push(hit);
            }
        }
        "method_definition" => {
            if let Some(hit) = named_symbol(node, src, "method") {
                *current_fn = Some(hit.name.clone());
                out.functions.push(hit);
            }
        }
        "class_declaration" => {
            if let Some(hit) = named_symbol(node, src, "class") {
                collect_heritage(node, src, &hit.name, out);
                out.classes.push(hit);
            }
        }
        "interface_declaration" => {
            if let Some(hit) = named_symbol(node, src, "interface") {
                out.classes.push(hit);
            }
        }
        "import_statement" => out.imports.push(import_from_node(node, src)),
        _ => {}
    }
}

fn visit_go(node: Node, src: &[u8], out: &mut FileAst, current_fn: &mut Option<String>) {
    match node.kind() {
        "function_declaration" | "method_declaration" => {
            let kind = if node.kind() == "method_declaration" {
                "method"
            } else {
                "function"
            };
            if let Some(hit) = named_symbol(node, src, kind) {
                *current_fn = Some(hit.name.clone());
                out.functions.push(hit);
            }
        }
        "type_spec" => {
            if let Some(hit) = named_symbol(node, src, "type") {
                out.classes.push(hit);
            }
        }
        "import_spec" => out.imports.push(import_from_node(node, src)),
        _ => {}
    }
}

fn visit_rust(node: Node, src: &[u8], out: &mut FileAst, current_fn: &mut Option<String>) {
    match node.kind() {
        "function_item" => {
            if let Some(hit) = named_symbol(node, src, "function") {
                *current_fn = Some(hit.name.clone());
                out.functions.push(hit);
            }
        }
        "struct_item" => {
            if let Some(hit) = named_symbol(node, src, "struct") {
                out.classes.push(hit);
            }
        }
        "enum_item" => {
            if let Some(hit) = named_symbol(node, src, "enum") {
                out.classes.push(hit);
            }
        }
        "trait_item" => {
            if let Some(hit) = named_symbol(node, src, "trait") {
                out.classes.push(hit);
            }
        }
        "impl_item" => {
            let ty = node
                .child_by_field_name("type")
                .map(|n| node_text(n, src).to_string());
            let tr = node
                .child_by_field_name("trait")
                .map(|n| node_text(n, src).to_string());
            if let (Some(ty), Some(tr)) = (ty, tr) {
                out.heritage.push((ty, tr));
            }
        }
        "use_declaration" => out.imports.push(import_from_node(node, src)),
        _ => {}
    }
}

fn named_symbol(node: Node, src: &[u8], kind: &str) -> Option<SymbolHit> {
    let name_node = node.child_by_field_name("name")?;
    let name = node_text(name_node, src);
    if name.is_empty() {
        return None;
    }
    let slice = &src[node.start_byte()..node.end_byte().min(src.len())];
    Some(SymbolHit {
        name: name.to_string(),
        kind: kind.to_string(),
        line: node.start_position().row as u32 + 1,
        end_line: node.end_position().row as u32 + 1,
        hash: sha3_hex(slice),
        signature: first_line_sig(src, node.start_byte()),
    })
}

fn first_line_sig(src: &[u8], start_byte: usize) -> String {
    let rest = &src[start_byte.min(src.len())..];
    let line = rest.split(|b| *b == b'\n').next().unwrap_or(rest);
    clip(&String::from_utf8_lossy(line).trim(), 160)
}

fn maybe_doc(node: Node, src: &[u8], out: &mut FileAst) {
    let body = match node.child_by_field_name("body") {
        Some(b) => b,
        None => return,
    };
    let mut cursor = body.walk();
    for child in body.children(&mut cursor) {
        if child.kind() == "expression_statement" {
            if let Some(inner) = child.child(0) {
                if inner.kind() == "string" || inner.kind() == "concatenated_string" {
                    let raw = node_text(inner, src).trim();
                    let cleaned = raw.trim_matches(|c| c == '"' || c == '\'');
                    if !cleaned.is_empty() {
                        out.docs.push(clip(cleaned, 200));
                    }
                }
            }
            return;
        }
        if child.kind() != "comment" {
            return;
        }
    }
}

fn collect_heritage(node: Node, src: &[u8], name: &str, out: &mut FileAst) {
    for field in ["heritage", "superclass", "extends_clause"] {
        if let Some(h) = node.child_by_field_name(field) {
            for parent in ident_tokens(h, src) {
                out.heritage.push((name.to_string(), parent));
            }
        }
    }
}

fn import_from_node(node: Node, src: &[u8]) -> Import {
    let raw = clip(node_text(node, src), 240);
    let module = raw
        .split_whitespace()
        .nth(1)
        .unwrap_or(raw.as_str())
        .trim_matches(|c| c == '"' || c == '\'' || c == ';')
        .to_string();
    Import {
        module,
        names: ident_tokens(node, src),
    }
}

fn ident_tokens(node: Node, src: &[u8]) -> Vec<String> {
    let mut out = Vec::new();
    let mut stack = vec![node];
    while let Some(cur) = stack.pop() {
        if cur.kind() == "identifier"
            || cur.kind() == "type_identifier"
            || cur.kind() == "dotted_name"
        {
            let t = node_text(cur, src);
            if !t.is_empty() {
                out.push(t.to_string());
            }
            continue;
        }
        let mut cursor = cur.walk();
        for child in cur.children(&mut cursor) {
            stack.push(child);
        }
    }
    out
}

fn is_call(kind: &str) -> bool {
    matches!(kind, "call" | "call_expression")
}

fn callee_name(node: Node, src: &[u8]) -> Option<String> {
    if let Some(fn_node) = node.child_by_field_name("function") {
        return leaf_ident(fn_node, src);
    }
    node.child(0).and_then(|n| leaf_ident(n, src))
}

fn leaf_ident(node: Node, src: &[u8]) -> Option<String> {
    if matches!(
        node.kind(),
        "identifier" | "type_identifier" | "field_identifier"
    ) {
        let t = node_text(node, src);
        if t.is_empty() {
            return None;
        }
        return Some(t.to_string());
    }
    if let Some(attr) = node.child_by_field_name("attribute") {
        return leaf_ident(attr, src);
    }
    if let Some(prop) = node.child_by_field_name("property") {
        return leaf_ident(prop, src);
    }
    node.child(node.child_count().saturating_sub(1))
        .and_then(|n| {
            if n.id() == node.id() {
                None
            } else {
                leaf_ident(n, src)
            }
        })
}

fn node_text<'a>(node: Node, src: &'a [u8]) -> &'a str {
    node.utf8_text(src).unwrap_or("")
}

fn clip(text: &str, max: usize) -> String {
    if text.len() <= max {
        return text.to_string();
    }
    let mut end = max.saturating_sub(3);
    while end > 0 && !text.is_char_boundary(end) {
        end -= 1;
    }
    format!("{}...", &text[..end])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_python_class_fn_import_doc() {
        let src = br#"
import os
class Animal:
    """base type"""
    def speak(self):
        return self.noise()
def noise():
    return "ok"
"#;
        let mut p = AstParser::new();
        let ast = p.parse_source(Lang::Python, src, "a.py").unwrap();
        assert!(ast.classes.iter().any(|c| c.name == "Animal"));
        assert!(ast.functions.iter().any(|f| f.name == "speak"));
        assert!(ast.functions.iter().any(|f| f.name == "noise"));
        assert!(ast.imports.iter().any(|i| i.module.contains("os") || i.names.iter().any(|n| n == "os")));
        assert!(ast.docs.iter().any(|d| d.contains("base type")));
        assert!(ast.heritage.is_empty() || ast.heritage.iter().all(|(a, _)| a == "Animal"));
        assert!(ast.calls.iter().any(|(_, c)| c == "noise"));
    }

    #[test]
    fn flags_broken_python_syntax() {
        let mut p = AstParser::new();
        assert!(!p.has_syntax_error(Lang::Python, b"def foo():\n    return 1\n"));
        assert!(p.has_syntax_error(Lang::Python, b"def foo(\n"));
        let hits = p.syntax_errors(Lang::Python, b"def foo(\n");
        assert!(hits.iter().any(|h| h.kind == "ERROR" || h.kind == "MISSING"));
        assert!(hits[0].line >= 1);
    }

    #[test]
    fn parse_is_subsecond() {
        let src = "def foo():\n    return 1\n".repeat(200);
        let mut p = AstParser::new();
        let start = std::time::Instant::now();
        let ast = p
            .parse_source(Lang::Python, src.as_bytes(), "big.py")
            .unwrap();
        assert!(start.elapsed().as_secs_f64() < 1.0);
        assert!(ast.functions.iter().any(|f| f.name == "foo"));
    }
}
