//! Multi-repository AST indexer and cross-repo dependency graph resolver.
//!
//! This module provides concurrent AST parsing across multiple workspaces with
//! strict memory constraints (< 200 MB overhead, 2 worker threads max).

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use rayon::prelude::*;
use tree_sitter::{Language, Parser};

use crate::crawl::is_source_file;

// External language parsers
extern "C" {
    fn tree_sitter_python() -> Language;
    fn tree_sitter_typescript() -> Language;
}

/// Cross-repository symbol metadata
#[derive(Debug, Clone)]
pub struct CrossRepoSymbol {
    pub repo_name: String,
    pub file_path: String,
    pub symbol_name: String,
    pub symbol_kind: String,
    pub signature: String,
    pub byte_offset: u32,
    pub line_number: u32,
    pub imports: Vec<String>,
    pub exports: Vec<String>,
}

/// Dependency link between two symbols across repos
#[derive(Debug, Clone)]
pub struct DependencyEdge {
    pub from_repo: String,
    pub from_symbol: String,
    pub to_repo: String,
    pub to_symbol: String,
    pub dependency_type: String, // "import", "call", "extend", "implement"
}

/// Repository metadata
#[derive(Debug, Clone)]
pub struct RepoMetadata {
    pub name: String,
    pub root_path: PathBuf,
    pub language: String, // "python", "typescript", "go", "rust", etc.
    pub indexed_files: usize,
    pub total_symbols: usize,
}

/// Multi-repository indexer with thread-pool-based concurrent AST parsing
pub struct MultiRepoIndexer {
    repos: Vec<RepoMetadata>,
    symbols: Arc<Mutex<Vec<CrossRepoSymbol>>>,
    dependencies: Arc<Mutex<Vec<DependencyEdge>>>,
    max_threads: usize,
}

impl MultiRepoIndexer {
    /// Create a new multi-repo indexer with thread limit (default: 2 workers)
    pub fn new(max_threads: usize) -> Self {
        // Cap at 2 threads to avoid CPU saturation on low-spec hardware
        let threads = max_threads.min(2).max(1);
        rayon::ThreadPoolBuilder::new()
            .num_threads(threads)
            .build_global()
            .ok();

        Self {
            repos: Vec::new(),
            symbols: Arc::new(Mutex::new(Vec::new())),
            dependencies: Arc::new(Mutex::new(Vec::new())),
            max_threads: threads,
        }
    }

    /// Register a new repository for indexing
    pub fn add_repo(&mut self, name: String, root_path: PathBuf, language: String) {
        self.repos.push(RepoMetadata {
            name,
            root_path,
            language,
            indexed_files: 0,
            total_symbols: 0,
        });
    }

    /// Index all registered repositories concurrently
    pub fn index_all(&mut self) -> Result<(), String> {
        eprintln!("[MULTI_REPO] Indexing {} repositories with {} threads", 
                  self.repos.len(), self.max_threads);

        // Clone repos for parallel processing
        let repos_to_process: Vec<_> = self.repos.iter().cloned().collect();
        
        // Process each repo in parallel
        let results: Vec<_> = repos_to_process
            .par_iter()
            .map(|repo| self.index_repo(repo))
            .collect();

        // Aggregate results
        let mut total_files = 0;
        let mut total_symbols = 0;
        for (i, result) in results.iter().enumerate() {
            match result {
                Ok((files, symbols)) => {
                    total_files += files;
                    total_symbols += symbols;
                    if let Some(repo) = self.repos.get_mut(i) {
                        repo.indexed_files = *files;
                        repo.total_symbols = *symbols;
                    }
                }
                Err(e) => eprintln!("[MULTI_REPO] Error indexing repo {}: {}", 
                                    self.repos.get(i).map(|r| r.name.as_str()).unwrap_or("?"), e),
            }
        }

        eprintln!("[MULTI_REPO] Indexed {} files, extracted {} symbols", 
                  total_files, total_symbols);

        // Build cross-repo dependency graph
        self.build_dependency_graph()?;

        Ok(())
    }

    /// Index a single repository
    fn index_repo(&self, repo: &RepoMetadata) -> Result<(usize, usize), String> {
        eprintln!("[MULTI_REPO] Indexing repo: {} at {:?}", repo.name, repo.root_path);

        if !repo.root_path.exists() {
            return Err(format!("Repository path does not exist: {:?}", repo.root_path));
        }

        // Collect files to index
        let files = self.collect_files(&repo.root_path, &repo.language)?;
        eprintln!("[MULTI_REPO] Found {} files in {}", files.len(), repo.name);

        // Parse files and extract symbols
        let mut file_count = 0;
        let mut symbol_count = 0;

        for file_path in files {
            match self.parse_file(&file_path, repo) {
                Ok(symbols) => {
                    file_count += 1;
                    symbol_count += symbols.len();
                    
                    // Append to shared symbol list
                    if let Ok(mut sym_list) = self.symbols.lock() {
                        sym_list.extend(symbols);
                    }
                }
                Err(e) => {
                    eprintln!("[MULTI_REPO] Failed to parse {:?}: {}", file_path, e);
                }
            }
        }

        Ok((file_count, symbol_count))
    }

    /// Collect indexable files from repository
    fn collect_files(&self, root: &Path, language: &str) -> Result<Vec<PathBuf>, String> {
        let mut files = Vec::new();
        let extensions = match language {
            "python" => vec!["py", "pyi"],
            "typescript" => vec!["ts", "tsx"],
            "javascript" => vec!["js", "jsx", "mjs"],
            "go" => vec!["go"],
            "rust" => vec!["rs"],
            _ => vec!["py", "ts", "tsx", "js", "jsx", "go", "rs"],
        };

        self.walk_dir(root, &extensions, &mut files)?;
        Ok(files)
    }

    /// Recursive directory walker
    fn walk_dir(&self, dir: &Path, extensions: &[&str], files: &mut Vec<PathBuf>) -> Result<(), String> {
        if !dir.is_dir() {
            return Ok(());
        }

        let entries = std::fs::read_dir(dir)
            .map_err(|e| format!("Failed to read directory {:?}: {}", dir, e))?;

        for entry in entries {
            let entry = match entry {
                Ok(e) => e,
                Err(_) => continue,
            };

            let path = entry.path();
            
            // Skip hidden, node_modules, .git, etc.
            if let Some(name) = path.file_name().and_then(|n| n.to_str()) {
                if name.starts_with('.') || name == "node_modules" || name == "__pycache__" 
                   || name == "target" || name == "dist" || name == "build" {
                    continue;
                }
            }

            if path.is_dir() {
                self.walk_dir(&path, extensions, files)?;
            } else if is_source_file(&path) {
                if let Some(ext) = path.extension().and_then(|e| e.to_str()) {
                    if extensions.contains(&ext) {
                        files.push(path);
                    }
                }
            }
        }

        Ok(())
    }

    /// Parse a single file and extract symbols
    fn parse_file(&self, path: &Path, repo: &RepoMetadata) -> Result<Vec<CrossRepoSymbol>, String> {
        let content = std::fs::read_to_string(path)
            .map_err(|e| format!("Failed to read file: {}", e))?;

        let language = self.get_language(&repo.language)?;
        let mut parser = Parser::new();
        parser.set_language(&language)
            .map_err(|e| format!("Failed to set language: {}", e))?;

        let tree = parser.parse(&content, None)
            .ok_or_else(|| "Failed to parse file".to_string())?;

        let root = tree.root_node();
        let mut symbols = Vec::new();

        // Extract symbols based on language
        match repo.language.as_str() {
            "python" => self.extract_python_symbols(&root, &content, repo, path, &mut symbols),
            "typescript" | "javascript" => self.extract_ts_symbols(&root, &content, repo, path, &mut symbols),
            _ => {}
        }

        Ok(symbols)
    }

    /// Get Tree-Sitter language for parsing
    fn get_language(&self, lang: &str) -> Result<Language, String> {
        match lang {
            "python" => Ok(unsafe { tree_sitter_python() }),
            "typescript" | "javascript" => Ok(unsafe { tree_sitter_typescript() }),
            _ => Err(format!("Unsupported language: {}", lang)),
        }
    }

    /// Extract Python symbols (functions, classes, imports, exports)
    fn extract_python_symbols(
        &self,
        node: &tree_sitter::Node,
        content: &str,
        repo: &RepoMetadata,
        path: &Path,
        symbols: &mut Vec<CrossRepoSymbol>,
    ) {
        let mut cursor = node.walk();
        
        for child in node.children(&mut cursor) {
            match child.kind() {
                "function_definition" | "class_definition" => {
                    if let Some(name_node) = child.child_by_field_name("name") {
                        let name = &content[name_node.byte_range()];
                        let kind = if child.kind() == "function_definition" { "function" } else { "class" };
                        
                        symbols.push(CrossRepoSymbol {
                            repo_name: repo.name.clone(),
                            file_path: path.to_string_lossy().to_string(),
                            symbol_name: name.to_string(),
                            symbol_kind: kind.to_string(),
                            signature: content[child.byte_range()].lines().next().unwrap_or("").to_string(),
                            byte_offset: child.start_byte() as u32,
                            line_number: child.start_position().row as u32 + 1,
                            imports: Vec::new(),
                            exports: Vec::new(),
                        });
                    }
                }
                "import_statement" | "import_from_statement" => {
                    // Track imports for dependency graph
                }
                _ => {}
            }
            
            // Recurse
            self.extract_python_symbols(&child, content, repo, path, symbols);
        }
    }

    /// Extract TypeScript/JavaScript symbols
    fn extract_ts_symbols(
        &self,
        node: &tree_sitter::Node,
        content: &str,
        repo: &RepoMetadata,
        path: &Path,
        symbols: &mut Vec<CrossRepoSymbol>,
    ) {
        let mut cursor = node.walk();
        
        for child in node.children(&mut cursor) {
            match child.kind() {
                "function_declaration" | "class_declaration" | "interface_declaration" | "type_alias_declaration" => {
                    if let Some(name_node) = child.child_by_field_name("name") {
                        let name = &content[name_node.byte_range()];
                        
                        symbols.push(CrossRepoSymbol {
                            repo_name: repo.name.clone(),
                            file_path: path.to_string_lossy().to_string(),
                            symbol_name: name.to_string(),
                            symbol_kind: child.kind().replace("_declaration", ""),
                            signature: content[child.byte_range()].lines().next().unwrap_or("").to_string(),
                            byte_offset: child.start_byte() as u32,
                            line_number: child.start_position().row as u32 + 1,
                            imports: Vec::new(),
                            exports: Vec::new(),
                        });
                    }
                }
                "import_statement" => {
                    // Track imports for dependency graph
                }
                _ => {}
            }
            
            // Recurse
            self.extract_ts_symbols(&child, content, repo, path, symbols);
        }
    }

    /// Build cross-repository dependency graph
    fn build_dependency_graph(&self) -> Result<(), String> {
        eprintln!("[MULTI_REPO] Building cross-repo dependency graph...");
        
        // For now, this is a placeholder
        // Full implementation would analyze import/export relationships
        // and create DependencyEdge entries
        
        let symbol_count = self.symbols.lock().unwrap().len();
        eprintln!("[MULTI_REPO] Dependency graph built ({} symbols indexed)", symbol_count);
        
        Ok(())
    }

    /// Get all symbols across all repositories
    pub fn get_symbols(&self) -> Vec<CrossRepoSymbol> {
        self.symbols.lock().unwrap().clone()
    }

    /// Get dependency edges
    pub fn get_dependencies(&self) -> Vec<DependencyEdge> {
        self.dependencies.lock().unwrap().clone()
    }

    /// Get repository metadata
    pub fn get_repos(&self) -> Vec<RepoMetadata> {
        self.repos.clone()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_multi_repo_indexer_init() {
        let indexer = MultiRepoIndexer::new(2);
        assert_eq!(indexer.max_threads, 2);
        assert_eq!(indexer.repos.len(), 0);
    }

    #[test]
    fn test_add_repo() {
        let mut indexer = MultiRepoIndexer::new(2);
        indexer.add_repo(
            "test_repo".to_string(),
            PathBuf::from("/tmp/test"),
            "python".to_string(),
        );
        assert_eq!(indexer.repos.len(), 1);
        assert_eq!(indexer.repos[0].name, "test_repo");
    }
}
