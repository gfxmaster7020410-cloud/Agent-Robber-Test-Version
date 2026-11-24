//! AST symbol search, reference graph extractor, and structural node replacer.

use std::fs;
use std::path::{Path, PathBuf};
use std::time::Instant;

use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use tree_sitter::{Node, Parser};

use crate::ast_parser::{AstParser, Lang, MAX_PARSE_BYTES};
use crate::crawl::collect_sources;
use crate::hashutil::sha3_hex;
use crate::mmap_search::map_or_read;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SymbolReference {
    pub name: String,
    pub kind: String,
    pub path: String,
    pub line: u32,
    pub end_line: u32,
    pub start_byte: usize,
    pub end_byte: usize,
    pub signature: String,
    pub docstring: String,
    pub hash: String,
    pub language: String,
    #[serde(default)]
    pub score: u32,
}

#[derive(Clone, Debug, Serialize, Deserialize, Default)]
pub struct ReferenceGraph {
    pub query: String,
    pub definitions: Vec<SymbolReference>,
    pub callers: Vec<SymbolReference>,
    pub callees: Vec<SymbolReference>,
    pub references: Vec<SymbolReference>,
    pub elapsed_us: u128,
    pub engine: &'static str,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct EditError {
    pub message: String,
    pub kind: String,
}

impl std::fmt::Display for EditError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.message)
    }
}

impl std::error::Error for EditError {}

/// Search workspace source files for symbols matching `query`.
pub fn search_workspace_symbols(
    root: &Path,
    query: &str,
    lang_filter: Option<&str>,
) -> Vec<SymbolReference> {
    let query_trim = query.trim();
    if query_trim.is_empty() {
        return Vec::new();
    }
    let query_lower = query_trim.to_lowercase();
    let sources = collect_sources(root);
    let lang_req = lang_filter
        .map(|s| s.trim().to_lowercase())
        .filter(|s| !s.is_empty() && s != "all");

    let mut hits: Vec<SymbolReference> = sources
        .par_iter()
        .filter_map(|file_path| {
            let lang = Lang::from_path(file_path)?;
            if let Some(ref req) = lang_req {
                let lname = lang.as_str().to_lowercase();
                if &lname != req && !(req == "py" && lname == "python") && !(req == "ts" && (lname == "typescript" || lname == "tsx")) && !(req == "js" && lname == "javascript") {
                    return None;
                }
            }
            let bytes = map_or_read(file_path)?;
            if bytes.len() as u64 > MAX_PARSE_BYTES {
                return None;
            }
            let rel_path = file_path
                .strip_prefix(root)
                .unwrap_or(file_path)
                .to_string_lossy()
                .replace('\\', "/");

            let mut parser = Parser::new();
            parser.set_language(&lang.language()).ok()?;
            let tree = parser.parse(&*bytes, None)?;
            let mut symbols = Vec::new();
            extract_symbols_from_node(tree.root_node(), &*bytes, lang, &rel_path, None, &mut symbols, 0);

            let mut matched = Vec::new();
            for mut sym in symbols {
                let sym_lower = sym.name.to_lowercase();
                let score = fuzzy_score(&query_lower, &sym_lower);
                if score > 0 {
                    sym.score = score;
                    matched.push(sym);
                }
            }
            Some(matched)
        })
        .flatten()
        .collect();

    hits.sort_by(|a, b| b.score.cmp(&a.score).then_with(|| a.path.cmp(&b.path)));
    hits.truncate(64);
    hits
}

/// Find callers, callees, definitions, and references for `symbol_name`.
pub fn find_symbol_references(root: &Path, symbol_name: &str) -> ReferenceGraph {
    let start = Instant::now();
    let name_trim = symbol_name.trim();
    if name_trim.is_empty() {
        return ReferenceGraph {
            query: String::new(),
            engine: "rust-ast",
            ..Default::default()
        };
    }
    let name_lower = name_trim.to_lowercase();
    let sources = collect_sources(root);

    let all_symbols: Vec<(String, Vec<SymbolReference>, Vec<(String, String)>)> = sources
        .par_iter()
        .filter_map(|file_path| {
            let lang = Lang::from_path(file_path)?;
            let bytes = map_or_read(file_path)?;
            if bytes.len() as u64 > MAX_PARSE_BYTES {
                return None;
            }
            let rel_path = file_path
                .strip_prefix(root)
                .unwrap_or(file_path)
                .to_string_lossy()
                .replace('\\', "/");

            let mut parser = Parser::new();
            parser.set_language(&lang.language()).ok()?;
            let tree = parser.parse(&*bytes, None)?;
            let mut symbols = Vec::new();
            let mut calls = Vec::new();
            extract_symbols_and_calls(tree.root_node(), &*bytes, lang, &rel_path, None, &mut symbols, &mut calls, 0);
            Some((rel_path, symbols, calls))
        })
        .collect();

    let mut definitions = Vec::new();
    let mut callers = Vec::new();
    let mut callees = Vec::new();
    let mut references = Vec::new();

    let def_names: Vec<String> = all_symbols
        .iter()
        .flat_map(|(_, syms, _)| syms.iter())
        .filter(|s| s.name.to_lowercase() == name_lower || s.name.ends_with(&format!(".{name_trim}")))
        .map(|s| {
            definitions.push(s.clone());
            s.name.clone()
        })
        .collect();

    for (rel_path, syms, calls) in &all_symbols {
        for (caller_fn, called_fn) in calls {
            if called_fn.to_lowercase() == name_lower || def_names.iter().any(|d| d == called_fn) {
                if let Some(caller_sym) = syms.iter().find(|s| &s.name == caller_fn) {
                    if !callers.iter().any(|c: &SymbolReference| c.name == caller_sym.name && c.path == caller_sym.path) {
                        callers.push(caller_sym.clone());
                    }
                } else {
                    callers.push(SymbolReference {
                        name: caller_fn.clone(),
                        kind: "Caller".into(),
                        path: rel_path.clone(),
                        line: 1,
                        end_line: 1,
                        start_byte: 0,
                        end_byte: 0,
                        signature: String::new(),
                        docstring: String::new(),
                        hash: String::new(),
                        language: String::new(),
                        score: 80,
                    });
                }
            }
            if def_names.iter().any(|d| d == caller_fn) || caller_fn.to_lowercase() == name_lower {
                if let Some(callee_sym) = syms.iter().find(|s| &s.name == called_fn) {
                    if !callees.iter().any(|c: &SymbolReference| c.name == callee_sym.name && c.path == callee_sym.path) {
                        callees.push(callee_sym.clone());
                    }
                } else {
                    callees.push(SymbolReference {
                        name: called_fn.clone(),
                        kind: "Callee".into(),
                        path: rel_path.clone(),
                        line: 1,
                        end_line: 1,
                        start_byte: 0,
                        end_byte: 0,
                        signature: String::new(),
                        docstring: String::new(),
                        hash: String::new(),
                        language: String::new(),
                        score: 70,
                    });
                }
            }
        }
        for sym in syms {
            if sym.name.to_lowercase().contains(&name_lower) && !definitions.iter().any(|d| d.name == sym.name && d.path == sym.path) {
                references.push(sym.clone());
            }
        }
    }

    ReferenceGraph {
        query: name_trim.to_string(),
        definitions,
        callers,
        callees,
        references,
        elapsed_us: start.elapsed().as_micros(),
        engine: "rust-ast",
    }
}

/// Replace the AST node of `target_symbol` in `file_path` with `new_code_block`.
/// Verifies structural syntax validity and returns updated source without touching disk.
pub fn replace_ast_node(
    root: &Path,
    file_path: &str,
    target_symbol: &str,
    new_code_block: &str,
) -> Result<String, EditError> {
    let path = if Path::new(file_path).is_absolute() {
        PathBuf::from(file_path)
    } else {
        root.join(file_path)
    };
    if !path.is_file() {
        return Err(EditError {
            message: format!("Target file does not exist: {file_path}"),
            kind: "file_not_found".into(),
        });
    }
    let source_bytes = fs::read(&path).map_err(|e| EditError {
        message: format!("Failed to read {file_path}: {e}"),
        kind: "io_error".into(),
    })?;
    let source = String::from_utf8_lossy(&source_bytes).to_string();

    let lang = Lang::from_path(&path).ok_or_else(|| EditError {
        message: format!("Unsupported file extension for AST editing: {file_path}"),
        kind: "unsupported_language".into(),
    })?;

    let mut parser = Parser::new();
    parser.set_language(&lang.language()).map_err(|e| EditError {
        message: format!("Parser error: {e}"),
        kind: "parser_error".into(),
    })?;

    let tree = parser.parse(&source_bytes, None).ok_or_else(|| EditError {
        message: format!("Failed to parse AST for {file_path}"),
        kind: "parse_failed".into(),
    })?;

    let target_clean = target_symbol.trim().trim_start_matches("def ").trim_start_matches("class ").trim();
    let (target_parent, target_member) = if let Some(idx) = target_clean.rfind('.') {
        (&target_clean[..idx], &target_clean[idx + 1..])
    } else {
        ("", target_clean)
    };

    let target_node = find_ast_node(tree.root_node(), &source_bytes, lang, target_parent, target_member)
        .ok_or_else(|| EditError {
            message: format!("Symbol `{target_symbol}` not found in AST of {file_path}"),
            kind: "symbol_not_found".into(),
        })?;

    let start_byte = target_node.start_byte();
    let end_byte = target_node.end_byte();

    let prefix = &source[..start_byte];
    let suffix = &source[end_byte..];

    // Format new block indentation if needed
    let replacement = format_code_block(new_code_block, prefix);
    let mut updated = format!("{prefix}{replacement}{suffix}");
    if !updated.ends_with('\n') {
        updated.push('\n');
    }

    // Verify syntax
    let mut validator = AstParser::new();
    let errors = validator.syntax_errors(lang, updated.as_bytes());
    if let Some(err) = errors.into_iter().next() {
        return Err(EditError {
            message: format!(
                "AstValidationError {} {}:{} {} `{}`",
                err.kind, file_path, err.line, err.node_type, err.snippet
            ),
            kind: "ast_validation_error".into(),
        });
    }

    Ok(updated)
}

fn format_code_block(code: &str, _prefix: &str) -> String {
    let mut block = code.trim_end().to_string();
    if !block.ends_with('\n') {
        block.push('\n');
    }
    block
}

fn find_ast_node<'a>(
    root_node: Node<'a>,
    src: &[u8],
    lang: Lang,
    parent_name: &str,
    target_name: &str,
) -> Option<Node<'a>> {
    let mut cursor = root_node.walk();
    for child in root_node.children(&mut cursor) {
        if !parent_name.is_empty() {
            if is_class_node(child, lang) {
                if let Some(cname) = node_name(child, src, lang) {
                    if cname == parent_name {
                        let mut sub_cursor = child.walk();
                        for sub_child in child.children(&mut sub_cursor) {
                            if let Some(target) = match_definition_node(sub_child, src, lang, target_name) {
                                return Some(target);
                            }
                            if sub_child.kind() == "block" || sub_child.kind() == "statement_block" || sub_child.kind() == "class_body" {
                                let mut block_cursor = sub_child.walk();
                                for block_child in sub_child.children(&mut block_cursor) {
                                    if let Some(target) = match_definition_node(block_child, src, lang, target_name) {
                                        return Some(target);
                                    }
                                }
                            }
                        }
                    }
                }
            }
        } else {
            if let Some(target) = match_definition_node(child, src, lang, target_name) {
                return Some(target);
            }
        }
    }
    None
}

fn match_definition_node<'a>(node: Node<'a>, src: &[u8], lang: Lang, target_name: &str) -> Option<Node<'a>> {
    if is_definition_node(node, lang) {
        if let Some(name) = node_name(node, src, lang) {
            if name == target_name {
                return Some(node);
            }
        }
    }
    None
}

fn is_definition_node(node: Node, lang: Lang) -> bool {
    let k = node.kind();
    match lang {
        Lang::Python => matches!(k, "function_definition" | "async_function_definition" | "class_definition"),
        Lang::TypeScript | Lang::Tsx | Lang::JavaScript => matches!(
            k,
            "function_declaration"
                | "class_declaration"
                | "method_definition"
                | "interface_declaration"
                | "type_alias_declaration"
                | "enum_declaration"
        ),
        Lang::Go => matches!(k, "function_declaration" | "method_declaration" | "type_declaration" | "type_spec"),
        Lang::Rust => matches!(
            k,
            "function_item" | "struct_item" | "enum_item" | "impl_item" | "trait_item" | "type_item"
        ),
    }
}

fn is_class_node(node: Node, lang: Lang) -> bool {
    let k = node.kind();
    match lang {
        Lang::Python => k == "class_definition",
        Lang::TypeScript | Lang::Tsx | Lang::JavaScript => k == "class_declaration" || k == "interface_declaration",
        Lang::Go => k == "type_declaration" || k == "type_spec",
        Lang::Rust => k == "struct_item" || k == "enum_item" || k == "impl_item" || k == "trait_item",
    }
}

fn node_name<'a>(node: Node<'a>, src: &'a [u8], lang: Lang) -> Option<&'a str> {
    let k = node.kind();
    let name_field = match lang {
        Lang::Python => {
            if k == "function_definition" || k == "async_function_definition" || k == "class_definition" {
                node.child_by_field_name("name")
            } else {
                None
            }
        }
        Lang::TypeScript | Lang::Tsx | Lang::JavaScript => {
            node.child_by_field_name("name")
        }
        Lang::Go => {
            node.child_by_field_name("name")
        }
        Lang::Rust => {
            node.child_by_field_name("name")
        }
    };
    if let Some(nf) = name_field {
        return std::str::from_utf8(&src[nf.start_byte()..nf.end_byte()]).ok();
    }
    None
}

fn extract_symbols_from_node(
    node: Node,
    src: &[u8],
    lang: Lang,
    file_path: &str,
    parent: Option<&str>,
    out: &mut Vec<SymbolReference>,
    depth: usize,
) {
    if depth > 128 {
        return;
    }
    let k = node.kind();
    let is_sym = is_definition_node(node, lang);
    let current_name = parent;

    if is_sym {
        if let Some(name) = node_name(node, src, lang) {
            let full_name = match parent {
                Some(p) => format!("{p}.{name}"),
                None => name.to_string(),
            };
            let start_pos = node.start_position();
            let end_pos = node.end_position();
            let sig = extract_signature(node, src);
            let kind = map_node_kind(k);
            let hash = sha3_hex(&src[node.start_byte()..node.end_byte()]);

            out.push(SymbolReference {
                name: full_name.clone(),
                kind: kind.to_string(),
                path: file_path.to_string(),
                line: start_pos.row as u32 + 1,
                end_line: end_pos.row as u32 + 1,
                start_byte: node.start_byte(),
                end_byte: node.end_byte(),
                signature: sig,
                docstring: extract_docstring(node, src, lang),
                hash,
                language: lang.as_str().to_string(),
                score: 0,
            });
            if is_class_node(node, lang) {
                let mut cursor = node.walk();
                for child in node.children(&mut cursor) {
                    extract_symbols_from_node(child, src, lang, file_path, Some(&name), out, depth + 1);
                }
                return;
            }
        }
    }

    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        extract_symbols_from_node(child, src, lang, file_path, current_name, out, depth + 1);
    }
}

fn extract_symbols_and_calls(
    node: Node,
    src: &[u8],
    lang: Lang,
    file_path: &str,
    current_fn: Option<&str>,
    symbols: &mut Vec<SymbolReference>,
    calls: &mut Vec<(String, String)>,
    depth: usize,
) {
    if depth > 128 {
        return;
    }
    let k = node.kind();
    let is_fn = matches!(
        k,
        "function_definition"
            | "async_function_definition"
            | "function_declaration"
            | "method_definition"
            | "function_item"
            | "method_declaration"
    );

    let mut fn_name = current_fn;
    if is_fn {
        if let Some(name) = node_name(node, src, lang) {
            fn_name = Some(name);
            let start_pos = node.start_position();
            let end_pos = node.end_position();
            symbols.push(SymbolReference {
                name: name.to_string(),
                kind: "Function".into(),
                path: file_path.to_string(),
                line: start_pos.row as u32 + 1,
                end_line: end_pos.row as u32 + 1,
                start_byte: node.start_byte(),
                end_byte: node.end_byte(),
                signature: extract_signature(node, src),
                docstring: extract_docstring(node, src, lang),
                hash: sha3_hex(&src[node.start_byte()..node.end_byte()]),
                language: lang.as_str().to_string(),
                score: 0,
            });
        }
    } else if is_class_node(node, lang) {
        if let Some(name) = node_name(node, src, lang) {
            let start_pos = node.start_position();
            let end_pos = node.end_position();
            symbols.push(SymbolReference {
                name: name.to_string(),
                kind: "Class".into(),
                path: file_path.to_string(),
                line: start_pos.row as u32 + 1,
                end_line: end_pos.row as u32 + 1,
                start_byte: node.start_byte(),
                end_byte: node.end_byte(),
                signature: extract_signature(node, src),
                docstring: extract_docstring(node, src, lang),
                hash: sha3_hex(&src[node.start_byte()..node.end_byte()]),
                language: lang.as_str().to_string(),
                score: 0,
            });
        }
    }

    if matches!(k, "call" | "call_expression" | "call_item") {
        if let Some(fn_ctx) = fn_name {
            if let Some(fn_child) = node.child_by_field_name("function") {
                if let Ok(called_name) = std::str::from_utf8(&src[fn_child.start_byte()..fn_child.end_byte()]) {
                    let clean = called_name.split('.').last().unwrap_or(called_name).trim();
                    if !clean.is_empty() {
                        calls.push((fn_ctx.to_string(), clean.to_string()));
                    }
                }
            }
        }
    }

    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        extract_symbols_and_calls(child, src, lang, file_path, fn_name, symbols, calls, depth + 1);
    }
}

fn map_node_kind(k: &str) -> &'static str {
    match k {
        "function_definition" | "async_function_definition" | "function_declaration" | "function_item" => "Function",
        "class_definition" | "class_declaration" => "Class",
        "struct_item" => "Struct",
        "interface_declaration" => "Interface",
        "method_definition" | "method_declaration" => "Method",
        "enum_declaration" | "enum_item" => "Enum",
        "type_alias_declaration" | "type_item" | "type_spec" => "TypeAlias",
        "import_statement" | "import_from_statement" | "import_declaration" => "Import",
        _ => "Symbol",
    }
}

fn extract_signature(node: Node, src: &[u8]) -> String {
    let start = node.start_byte();
    let end = (start + 240).min(node.end_byte()).min(src.len());
    let slice = String::from_utf8_lossy(&src[start..end]);
    slice.lines().next().unwrap_or("").trim().to_string()
}

fn extract_docstring(node: Node, src: &[u8], lang: Lang) -> String {
    if lang == Lang::Python {
        if let Some(body) = node.child_by_field_name("body") {
            let mut cursor = body.walk();
            for child in body.children(&mut cursor) {
                if child.kind() == "expression_statement" {
                    if let Some(str_node) = child.child(0) {
                        if str_node.kind() == "string" {
                            let raw = String::from_utf8_lossy(&src[str_node.start_byte()..str_node.end_byte()]);
                            return raw.trim_matches('"').trim_matches('\'').trim().chars().take(240).collect();
                        }
                    }
                }
                break;
            }
        }
    }
    String::new()
}

fn fuzzy_score(query: &str, target: &str) -> u32 {
    if query == target {
        return 100;
    }
    if target.starts_with(query) {
        return 90;
    }
    if target.ends_with(query) {
        return 80;
    }
    if target.contains(query) {
        return 70;
    }
    0
}
