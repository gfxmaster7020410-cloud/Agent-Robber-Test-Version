//! OWASP-oriented pattern scan over source buffers (no network, no spawn).



use regex::Regex;

use serde::Serialize;



#[derive(Clone, Debug, Serialize)]

pub struct SecurityFinding {

    pub rule: String,

    pub owasp: String,

    pub path: String,

    pub line: u32,

    pub snippet: String,

}



pub fn audit_source(source: &str, path: &str) -> Vec<SecurityFinding> {

    let sqli = Regex::new(

        r#"(?i)(?:execute|executemany)\s*\(\s*(?:f["']|["'][^"']*%|[^)]*(?:\+|%))"#,

    )

    .ok();

    let secret = Regex::new(

        r#"(?i)\b(api_key|apikey|password|secret|token|private_key)\s*=\s*['"][^'"]{8,}['"]"#,

    )

    .ok();

    let unsafe_re = Regex::new(r#"(?i)\b(?:eval|exec|pickle\.loads|yaml\.load)\s*\(|\bunsafe\s*\{"#).ok();

    let input = Regex::new(r#"(?i)request\.(?:GET|POST|args|form|json)\b"#).ok();



    let mut out = Vec::new();

    for (idx, line) in source.lines().enumerate() {

        let trimmed = line.trim();

        if trimmed.starts_with('#') || trimmed.starts_with("//") {

            continue;

        }

        let n = (idx as u32) + 1;

        if let Some(re) = &sqli {

            if re.is_match(line) {

                out.push(finding("sqli", "A03:Injection", path, n, trimmed));

            }

        }

        if let Some(re) = &secret {

            if re.is_match(line) && !line.contains("os.environ") {

                out.push(finding("secret", "A07:Identification", path, n, trimmed));

            }

        }

        if let Some(re) = &unsafe_re {

            if re.is_match(line) {

                out.push(finding("unsafe", "A08:Integrity", path, n, trimmed));

            }

        }

        if let Some(re) = &input {

            if re.is_match(line) && !line.contains("int(") && !line.to_ascii_lowercase().contains("escape") {

                out.push(finding("input", "A04:Insecure Design", path, n, trimmed));

            }

        }

    }

    out

}



fn finding(rule: &str, owasp: &str, path: &str, line: u32, snippet: &str) -> SecurityFinding {

    let clip: String = snippet.chars().take(160).collect();

    SecurityFinding {

        rule: rule.into(),

        owasp: owasp.into(),

        path: path.into(),

        line,

        snippet: clip,

    }

}



#[cfg(test)]

mod tests {

    use super::*;



    #[test]

    fn flags_sql_injection_and_secrets() {

        let src = r#"

def bad(user_id):

    cur.execute("SELECT * FROM t WHERE id=" + user_id)

    password = "super-secret-value"

"#;

        let hits = audit_source(src, "routes.py");

        assert!(hits.iter().any(|h| h.rule == "sqli"));

        assert!(hits.iter().any(|h| h.rule == "secret"));

    }

}

