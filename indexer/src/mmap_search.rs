//! Zero-copy parallel search over mmapped source files.

use std::collections::HashSet;
use std::fs::File;
use std::path::{Path, PathBuf};

use memmap2::Mmap;
use rayon::prelude::*;
use regex::bytes::Regex;
use serde::Serialize;

use crate::crawl::{collect_sources, relative_posix};

const SMALL_READ: u64 = 4096;
const MAX_FILE: u64 = 8_000_000;

#[derive(Clone, Debug, Serialize)]
pub struct SearchHit {
    pub path: String,
    pub line: u32,
    pub column: u32,
    pub snippet: String,
}

pub struct MmapSearcher {
    pub max_hits: usize,
}

impl Default for MmapSearcher {
    fn default() -> Self {
        Self { max_hits: 256 }
    }
}

impl MmapSearcher {
    pub fn search_tree(&self, root: &Path, pattern: &str) -> Result<Vec<SearchHit>, String> {
        let re = Regex::new(pattern).map_err(|e| e.to_string())?;
        let files = collect_sources(root);
        let max_hits = self.max_hits;
        let mut hits: Vec<SearchHit> = files
            .par_iter()
            .flat_map_iter(|path| search_file(root, path, &re, max_hits))
            .collect();
        hits.sort_by(|a, b| a.path.cmp(&b.path).then(a.line.cmp(&b.line)));
        hits.truncate(self.max_hits);
        Ok(hits)
    }

    /// Filename-boosted walk of `robber/` + `tests/`, skip files that lack `needle`.
    pub fn search_tree_limited(
        &self,
        root: &Path,
        pattern: &str,
        needle: &[u8],
        max_unique_files: usize,
    ) -> Result<Vec<SearchHit>, String> {
        let re = Regex::new(pattern).map_err(|e| e.to_string())?;
        let query = std::str::from_utf8(needle).unwrap_or("");
        let listed = context_files(root);
        let mut boosted = Vec::new();
        for path in &listed {
            if name_boost(path, query) <= 1 {
                boosted.push(path.clone());
            }
        }
        let hot_dirs: HashSet<PathBuf> = boosted
            .iter()
            .filter_map(|p| p.parent().map(|d| d.to_path_buf()))
            .collect();
        let mut siblings = Vec::new();
        let mut robber_rest = Vec::new();
        let mut other = Vec::new();
        for path in listed {
            if boosted.iter().any(|b| b == &path) {
                continue;
            }
            if path
                .parent()
                .map(|d| hot_dirs.contains(d))
                .unwrap_or(false)
            {
                siblings.push(path);
            } else if relative_posix(root, &path).starts_with("robber/") {
                robber_rest.push(path);
            } else {
                other.push(path);
            }
        }
        let max_hits = self.max_hits.max(1);
        let file_cap = max_unique_files.max(1);
        let mut hits: Vec<SearchHit> = Vec::new();
        let mut unique: HashSet<String> = HashSet::new();
        for group in [&boosted, &siblings] {
            absorb_group(
                root,
                group,
                needle,
                &re,
                max_hits,
                file_cap,
                &mut hits,
                &mut unique,
            );
        }
        if unique.is_empty() {
            for group in [&robber_rest, &other] {
                absorb_group(
                    root,
                    group,
                    needle,
                    &re,
                    max_hits,
                    file_cap,
                    &mut hits,
                    &mut unique,
                );
            }
        }
        hits.sort_by(|a, b| a.path.cmp(&b.path).then(a.line.cmp(&b.line)));
        hits.truncate(self.max_hits);
        Ok(hits)
    }

    pub fn search_bytes(&self, hay: &[u8], pattern: &str) -> Result<Vec<(u32, u32)>, String> {
        let re = Regex::new(pattern).map_err(|e| e.to_string())?;
        Ok(match_offsets(hay, &re, self.max_hits)
            .into_iter()
            .map(|(line, col, _)| (line, col))
            .collect())
    }
}

fn search_file(root: &Path, path: &PathBuf, re: &Regex, cap: usize) -> Vec<SearchHit> {
    search_file_prefilter(root, path, b"", re, cap)
}

fn search_file_prefilter(
    root: &Path,
    path: &PathBuf,
    needle: &[u8],
    re: &Regex,
    cap: usize,
) -> Vec<SearchHit> {
    let bytes = match map_or_read(path) {
        Some(b) => b,
        None => return Vec::new(),
    };
    if looks_binary(&bytes) {
        return Vec::new();
    }
    if !needle.is_empty() && !contains_bytes(&bytes, needle) {
        return Vec::new();
    }
    let rel = relative_posix(root, path);
    match_offsets(&bytes, re, cap)
        .into_iter()
        .map(|(line, col, span)| SearchHit {
            path: rel.clone(),
            line,
            column: col,
            snippet: snippet(&bytes, span),
        })
        .collect()
}

fn contains_bytes(hay: &[u8], needle: &[u8]) -> bool {
    if needle.is_empty() || needle.len() > hay.len() {
        return false;
    }
    hay.windows(needle.len()).any(|w| w == needle)
}

fn absorb_group(
    root: &Path,
    files: &[PathBuf],
    needle: &[u8],
    re: &Regex,
    max_hits: usize,
    file_cap: usize,
    hits: &mut Vec<SearchHit>,
    unique: &mut HashSet<String>,
) {
    let mut idle = 0usize;
    let mut found = !unique.is_empty();
    for chunk in files.chunks(32) {
        if unique.len() >= file_cap {
            return;
        }
        if found && idle >= 48 {
            return;
        }
        let before = unique.len();
        let part: Vec<SearchHit> = chunk
            .par_iter()
            .flat_map_iter(|path| search_file_prefilter(root, path, needle, re, max_hits))
            .collect();
        for hit in part {
            unique.insert(hit.path.clone());
            hits.push(hit);
        }
        if unique.len() > before {
            idle = 0;
            found = true;
        } else if found {
            idle += chunk.len();
        }
    }
}

fn name_boost(path: &Path, query: &str) -> u8 {
    if query.is_empty() {
        return 4;
    }
    let stem = path
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    if stem.is_empty() {
        return 4;
    }
    let q = query.to_ascii_lowercase();
    if stem.contains(&q) || q.contains(&stem) {
        return 0;
    }
    for part in split_camel(query) {
        let p = part.to_ascii_lowercase();
        if p.len() >= 4 && stem.contains(&p) {
            return 1;
        }
    }
    4
}

fn split_camel(raw: &str) -> Vec<String> {
    let mut parts = Vec::new();
    let mut cur = String::new();
    let chars: Vec<char> = raw.chars().collect();
    for (i, c) in chars.iter().enumerate() {
        if c.is_ascii_uppercase() && !cur.is_empty() {
            let prev_lower = cur.chars().last().map(|x| x.is_ascii_lowercase()).unwrap_or(false);
            let next_lower = chars
                .get(i + 1)
                .map(|x| x.is_ascii_lowercase())
                .unwrap_or(false);
            if prev_lower || next_lower {
                parts.push(std::mem::take(&mut cur));
            }
        }
        cur.push(*c);
    }
    if !cur.is_empty() {
        parts.push(cur);
    }
    parts
}

fn context_files(root: &Path) -> Vec<PathBuf> {
    let robber = root.join("robber");
    if robber.is_dir() {
        collect_sources(&robber)
    } else {
        collect_sources(root)
    }
}

pub(crate) enum Mapped {
    Mmap(Mmap),
    Owned(Vec<u8>),
}

impl std::ops::Deref for Mapped {
    type Target = [u8];
    fn deref(&self) -> &[u8] {
        match self {
            Mapped::Mmap(m) => m,
            Mapped::Owned(v) => v,
        }
    }
}

pub(crate) fn map_or_read(path: &Path) -> Option<Mapped> {
    let file = File::open(path).ok()?;
    let meta = file.metadata().ok()?;
    let len = meta.len();
    if len == 0 || len > MAX_FILE {
        return None;
    }
    if len <= SMALL_READ {
        let data = std::fs::read(path).ok()?;
        return Some(Mapped::Owned(data));
    }
    // Safety: file is not mutated by this process during the search.
    let mmap = unsafe { Mmap::map(&file).ok()? };
    Some(Mapped::Mmap(mmap))
}

fn looks_binary(bytes: &[u8]) -> bool {
    let sample = &bytes[..bytes.len().min(4096)];
    let nuls = sample.iter().filter(|b| **b == 0).count();
    nuls * 5 > sample.len()
}

fn match_offsets(hay: &[u8], re: &Regex, cap: usize) -> Vec<(u32, u32, std::ops::Range<usize>)> {
    let mut out = Vec::new();
    for m in re.find_iter(hay) {
        let (line, col) = line_col(hay, m.start());
        out.push((line, col, m.start()..m.end()));
        if out.len() >= cap {
            break;
        }
    }
    out
}

fn line_col(hay: &[u8], offset: usize) -> (u32, u32) {
    let mut line = 1u32;
    let mut last = 0usize;
    for (i, b) in hay[..offset.min(hay.len())].iter().enumerate() {
        if *b == b'\n' {
            line += 1;
            last = i + 1;
        }
    }
    let col = (offset.saturating_sub(last) as u32) + 1;
    (line, col)
}

/// Copy only the requested line range from an mmap (or a small owned read).
pub fn slice_lines(path: &Path, start_line: u32, end_line: u32, max_bytes: usize) -> String {
    let mapped = match map_or_read(path) {
        Some(m) => m,
        None => return String::new(),
    };
    let (lo, hi) = line_byte_span(&mapped, start_line, end_line);
    let cap = max_bytes.max(1);
    let hi = hi.min(lo.saturating_add(cap)).min(mapped.len());
    if lo >= mapped.len() || lo >= hi {
        return String::new();
    }
    String::from_utf8_lossy(&mapped[lo..hi]).into_owned()
}

fn line_byte_span(hay: &[u8], start_line: u32, end_line: u32) -> (usize, usize) {
    let start_line = start_line.max(1);
    let end_line = end_line.max(start_line);
    let mut starts = Vec::new();
    starts.push(0usize);
    for (i, b) in hay.iter().enumerate() {
        if *b == b'\n' && i + 1 < hay.len() {
            starts.push(i + 1);
        }
    }
    let lo = *starts
        .get((start_line as usize).saturating_sub(1))
        .unwrap_or(&0);
    let hi = starts.get(end_line as usize).copied().unwrap_or(hay.len());
    (lo, hi)
}

fn snippet(hay: &[u8], span: std::ops::Range<usize>) -> String {
    let start = span.start.saturating_sub(24);
    let end = (span.end + 24).min(hay.len());
    let slice = &hay[start..end];
    String::from_utf8_lossy(slice)
        .replace('\n', " ")
        .chars()
        .take(80)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn mmap_regex_hits_line() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!("robber-mmap-{unique}"));
        fs::create_dir_all(&dir).unwrap();
        let file = dir.join("hit.py");
        fs::write(&file, "alpha\nfind_me_please\nomega\n").unwrap();
        let searcher = MmapSearcher::default();
        let hits = searcher.search_tree(&dir, "find_me").unwrap();
        fs::remove_dir_all(&dir).ok();
        assert!(hits.iter().any(|h| h.line == 2 && h.path.ends_with("hit.py")));
    }

    #[test]
    fn slice_lines_copies_only_range() {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!("robber-slice-{unique}"));
        fs::create_dir_all(&dir).unwrap();
        let file = dir.join("hit.py");
        fs::write(&file, "one\ntwo\nthree\nfour\n").unwrap();
        let got = slice_lines(&file, 2, 3, 64);
        fs::remove_dir_all(&dir).ok();
        assert!(got.contains("two"));
        assert!(got.contains("three"));
        assert!(!got.contains("four"));
    }
}
