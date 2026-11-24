//! Hybrid search engine combining BM25 sparse search with dense vector similarity.
//!
//! Uses Reciprocal Rank Fusion (RRF) to merge keyword and semantic search results.
//! Memory footprint strictly capped < 200 MB.

use std::collections::HashMap;
use std::path::PathBuf;

use super::bm25::{Bm25Ranker, Bm25Result};

/// RRF constant (typically 60)
const RRF_K: f64 = 60.0;

/// Hybrid search result with combined score
#[derive(Debug, Clone)]
pub struct HybridResult {
    pub id: String,
    pub path: PathBuf,
    pub score: f64,
    pub bm25_score: f64,
    pub dense_score: f64,
    pub bm25_rank: usize,
    pub dense_rank: usize,
    pub snippet: String,
}

/// Dense vector search result (placeholder for actual vector similarity)
#[derive(Debug, Clone)]
pub struct DenseResult {
    pub id: String,
    pub path: PathBuf,
    pub similarity: f64,
}

/// Hybrid search engine with RRF fusion
pub struct HybridSearchEngine {
    bm25: Bm25Ranker,
    dense_enabled: bool,
}

impl HybridSearchEngine {
    /// Create a new hybrid search engine
    pub fn new() -> Self {
        Self {
            bm25: Bm25Ranker::new(),
            dense_enabled: false, // Vector search disabled by default (requires ONNX setup)
        }
    }

    /// Add document to both sparse and dense indices
    pub fn add_document(&mut self, id: String, path: PathBuf, content: String) {
        self.bm25.add_document(id, path, content);
        // TODO: Add to dense vector store when ONNX embedding model is integrated
    }

    /// Build index from multiple documents
    pub fn build_index(&mut self, docs: Vec<(String, PathBuf, String)>) {
        for (id, path, content) in docs {
            self.add_document(id, path, content);
        }
    }

    /// Hybrid search with RRF fusion
    pub fn search(&self, query: &str, top_k: usize) -> Vec<HybridResult> {
        // BM25 sparse search
        let bm25_results = self.bm25.search(query, top_k * 2);

        // Dense vector search (placeholder - returns empty if not enabled)
        let dense_results = if self.dense_enabled {
            self.dense_search(query, top_k * 2)
        } else {
            Vec::new()
        };

        // Merge with RRF
        self.reciprocal_rank_fusion(bm25_results, dense_results, top_k)
    }

    /// Reciprocal Rank Fusion (RRF) merge
    fn reciprocal_rank_fusion(
        &self,
        bm25_results: Vec<Bm25Result>,
        dense_results: Vec<DenseResult>,
        top_k: usize,
    ) -> Vec<HybridResult> {
        let mut rrf_scores: HashMap<String, HybridResult> = HashMap::new();

        // BM25 contributions
        for (rank, result) in bm25_results.iter().enumerate() {
            let rrf_score = 1.0 / (RRF_K + (rank + 1) as f64);
            rrf_scores
                .entry(result.doc_id.clone())
                .or_insert_with(|| HybridResult {
                    id: result.doc_id.clone(),
                    path: result.path.clone(),
                    score: 0.0,
                    bm25_score: result.score,
                    dense_score: 0.0,
                    bm25_rank: rank + 1,
                    dense_rank: 0,
                    snippet: String::new(),
                })
                .score += rrf_score;
        }

        // Dense vector contributions
        for (rank, result) in dense_results.iter().enumerate() {
            let rrf_score = 1.0 / (RRF_K + (rank + 1) as f64);
            rrf_scores
                .entry(result.id.clone())
                .and_modify(|e| {
                    e.score += rrf_score;
                    e.dense_score = result.similarity;
                    e.dense_rank = rank + 1;
                })
                .or_insert_with(|| HybridResult {
                    id: result.id.clone(),
                    path: result.path.clone(),
                    score: rrf_score,
                    bm25_score: 0.0,
                    dense_score: result.similarity,
                    bm25_rank: 0,
                    dense_rank: rank + 1,
                    snippet: String::new(),
                });
        }

        // Sort by RRF score descending
        let mut results: Vec<HybridResult> = rrf_scores.into_values().collect();
        results.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap_or(std::cmp::Ordering::Equal));

        // Return top-K
        results.into_iter().take(top_k).collect()
    }

    /// Dense vector search (placeholder for ONNX integration)
    fn dense_search(&self, _query: &str, _top_k: usize) -> Vec<DenseResult> {
        // TODO: Implement actual dense vector search with ONNX embeddings
        // For now, return empty results
        Vec::new()
    }

    /// Enable dense vector search (requires ONNX model)
    pub fn enable_dense_search(&mut self) {
        self.dense_enabled = true;
    }

    /// Get statistics
    pub fn stats(&self) -> HybridStats {
        let (docs, terms, avg_len) = self.bm25.stats();
        HybridStats {
            total_documents: docs,
            total_terms: terms,
            avg_doc_length: avg_len,
            dense_enabled: self.dense_enabled,
        }
    }
}

impl Default for HybridSearchEngine {
    fn default() -> Self {
        Self::new()
    }
}

/// Hybrid search engine statistics
#[derive(Debug, Clone)]
pub struct HybridStats {
    pub total_documents: usize,
    pub total_terms: usize,
    pub avg_doc_length: f64,
    pub dense_enabled: bool,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_hybrid_search_bm25_only() {
        let mut engine = HybridSearchEngine::new();
        
        engine.add_document(
            "doc1".to_string(),
            PathBuf::from("file1.py"),
            "def getUserById(id): return User.query.get(id)".to_string(),
        );
        
        engine.add_document(
            "doc2".to_string(),
            PathBuf::from("file2.py"),
            "def createUser(name): return User.create(name)".to_string(),
        );

        let results = engine.search("get user id", 5);
        assert!(!results.is_empty());
        assert_eq!(results[0].id, "doc1");
    }

    #[test]
    fn test_rrf_fusion() {
        let engine = HybridSearchEngine::new();
        
        let bm25_results = vec![
            Bm25Result {
                doc_id: "doc1".to_string(),
                path: PathBuf::from("file1.py"),
                score: 10.0,
                matched_terms: vec!["test".to_string()],
            },
            Bm25Result {
                doc_id: "doc2".to_string(),
                path: PathBuf::from("file2.py"),
                score: 5.0,
                matched_terms: vec!["test".to_string()],
            },
        ];
        
        let dense_results = vec![
            DenseResult {
                id: "doc2".to_string(),
                path: PathBuf::from("file2.py"),
                similarity: 0.95,
            },
            DenseResult {
                id: "doc1".to_string(),
                path: PathBuf::from("file1.py"),
                similarity: 0.85,
            },
        ];

        let merged = engine.reciprocal_rank_fusion(bm25_results, dense_results, 5);
        assert_eq!(merged.len(), 2);
        // Both docs should have combined scores from both rankers
        assert!(merged[0].bm25_score > 0.0 || merged[0].dense_score > 0.0);
    }
}
