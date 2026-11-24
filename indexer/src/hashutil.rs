//! SHA-3 file and symbol fingerprints matching Python QuantumSafeChecksum.

use sha3::{Digest, Sha3_256};

pub fn sha3_hex(bytes: &[u8]) -> String {
    let mut hasher = Sha3_256::new();
    hasher.update(bytes);
    hex::encode(hasher.finalize())
}

pub fn sha3_hex_str(text: &str) -> String {
    sha3_hex(text.as_bytes())
}

pub fn row_id(project_id: &str, path: &str) -> String {
    let mut hasher = Sha3_256::new();
    hasher.update(project_id.as_bytes());
    hasher.update([0u8]);
    hasher.update(path.as_bytes());
    let digest = hasher.finalize();
    hex::encode(&digest[..8])
}
