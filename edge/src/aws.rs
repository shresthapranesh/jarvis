//! AWS, as boto3 does it for jarvis: the credential chain, SigV4 signing,
//! and a call's failure spelled the way botocore spells it — so a Bedrock
//! listing or call made here reads the same as one made by Python.
//!
//! The credential chain covers what an install of this kind has: the
//! environment, static keys in the shared files, and an EC2 instance role.
//! The rest of botocore's chain (assume-role, SSO, web identity,
//! `credential_process`, container roles) is [`CredError::Unsupported`]: the
//! call fails, saying which source it found.

use std::collections::HashMap;
use std::path::PathBuf;
use std::time::Duration;

use hmac::{Hmac, Mac};
use percent_encoding::{AsciiSet, NON_ALPHANUMERIC, utf8_percent_encode};
use reqwest::Url;
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::pyjson;

#[derive(Clone, Debug, PartialEq)]
pub struct Credentials {
    pub access_key: String,
    pub secret_key: String,
    pub session_token: Option<String>,
}

#[derive(Debug, PartialEq)]
pub enum CredError {
    /// What botocore raises, word for word (`ProfileNotFound`,
    /// `NoCredentialsError`).
    Failed(String),
    /// A source this module doesn't speak; boto3 does.
    Unsupported(String),
}

impl CredError {
    /// The error a call fails with.
    pub fn message(self) -> String {
        match self {
            CredError::Failed(why) => why,
            CredError::Unsupported(what) => format!(
                "unsupported AWS credentials ({what}) — use access keys (in the environment or a shared \
                 credentials file) or an EC2 instance role"
            ),
        }
    }
}

/// `os.environ.get(k)`, with an empty value as unset (as boto3 reads most).
fn var(k: &str) -> Option<String> {
    std::env::var(k).ok().filter(|v| !v.is_empty())
}

/// The region jarvis's Bedrock code asks for: `AWS_REGION`, then
/// `AWS_DEFAULT_REGION`, then `us-east-1`.
pub fn region() -> String {
    var("AWS_REGION").or_else(|| var("AWS_DEFAULT_REGION")).unwrap_or_else(|| "us-east-1".into())
}

/// The endpoint for a service (`bedrock`, `bedrock-runtime`): botocore's
/// `AWS_ENDPOINT_URL_<SERVICE>`, then `AWS_ENDPOINT_URL`, then the regional
/// default.
pub fn endpoint(service: &str, region: &str) -> String {
    let ignored = var("AWS_IGNORE_CONFIGURED_ENDPOINT_URLS").is_some_and(|v| v.eq_ignore_ascii_case("true"));
    let configured = (!ignored)
        .then(|| var(&format!("AWS_ENDPOINT_URL_{}", service.to_uppercase().replace('-', "_"))).or_else(|| var("AWS_ENDPOINT_URL")))
        .flatten();
    configured
        .map(|u| u.trim_end_matches('/').to_string())
        .unwrap_or_else(|| format!("https://{service}.{region}.amazonaws.com"))
}

// ── Credentials ──────────────────────────────────────────────────────────────

fn home_path(raw: &str) -> PathBuf {
    match raw.strip_prefix("~/") {
        Some(rest) => PathBuf::from(var("HOME").unwrap_or_default()).join(rest),
        None => PathBuf::from(raw),
    }
}

type Section = HashMap<String, String>;

/// `configparser.RawConfigParser`, as far as the shared files use it:
/// sections, `key = value` (or `key: value`), whole-line comments, and
/// indented continuation lines (a nested `s3 =` block).
fn parse_ini(text: &str) -> Vec<(String, Section)> {
    let mut out: Vec<(String, Section)> = vec![];
    let mut last_key: Option<String> = None;
    for line in text.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() || trimmed.starts_with('#') || trimmed.starts_with(';') {
            continue;
        }
        if line.starts_with([' ', '\t']) {
            if let (Some(k), Some((_, sec))) = (&last_key, out.last_mut()) {
                let v = sec.entry(k.clone()).or_default();
                v.push('\n');
                v.push_str(trimmed);
            }
            continue;
        }
        if let Some(name) = trimmed.strip_prefix('[').and_then(|s| s.strip_suffix(']')) {
            out.push((name.to_string(), Section::new()));
            last_key = None;
            continue;
        }
        let at = trimmed.find(['=', ':']);
        if let (Some(at), Some((_, sec))) = (at, out.last_mut()) {
            let key = trimmed[..at].trim().to_lowercase();
            sec.insert(key.clone(), trimmed[at + 1..].trim().to_string());
            last_key = Some(key);
        }
    }
    out
}

fn read_ini(env: &str, default: &str) -> Vec<(String, Section)> {
    let path = home_path(&var(env).unwrap_or_else(|| default.into()));
    std::fs::read_to_string(path).map(|t| parse_ini(&t)).unwrap_or_default()
}

/// A config file's profiles: `[default]`, `[profile name]`.
fn config_profiles(sections: Vec<(String, Section)>) -> HashMap<String, Section> {
    let mut out = HashMap::new();
    for (name, values) in sections {
        let profile = if name == "default" {
            Some("default".to_string())
        } else if name.starts_with("profile") {
            let parts: Vec<&str> = name.split_whitespace().collect();
            (parts.len() == 2 && parts[0] == "profile").then(|| parts[1].trim_matches(['"', '\'']).to_string())
        } else {
            None
        };
        if let Some(p) = profile {
            out.entry(p).or_insert_with(Section::new).extend(values);
        }
    }
    out
}

fn static_keys(sec: &Section) -> Result<Option<Credentials>, CredError> {
    let get = |k: &str| sec.get(k).filter(|v| !v.is_empty()).cloned();
    match (get("aws_access_key_id"), get("aws_secret_access_key")) {
        (Some(access_key), Some(secret_key)) => {
            Ok(Some(Credentials { access_key, secret_key, session_token: get("aws_session_token") }))
        }
        (None, None) => Ok(None),
        _ => Err(CredError::Unsupported("partial credentials in a shared file".into())),
    }
}

/// boto3's chain, the parts an install like this has.
pub async fn credentials() -> Result<Credentials, CredError> {
    let config = config_profiles(read_ini("AWS_CONFIG_FILE", "~/.aws/config"));
    let shared: HashMap<String, Section> = read_ini("AWS_SHARED_CREDENTIALS_FILE", "~/.aws/credentials").into_iter().collect();
    let explicit = var("AWS_PROFILE").or_else(|| var("AWS_DEFAULT_PROFILE"));
    let profile = explicit.clone().unwrap_or_else(|| "default".into());
    // The session is built before any provider runs: a named profile that
    // neither file has fails even with keys in the environment.
    if explicit.is_some() && !config.contains_key(&profile) && !shared.contains_key(&profile) {
        return Err(CredError::Failed(format!("The config profile ({profile}) could not be found")));
    }

    match (var("AWS_ACCESS_KEY_ID"), var("AWS_SECRET_ACCESS_KEY")) {
        (Some(access_key), Some(secret_key)) => {
            let session_token = var("AWS_SESSION_TOKEN").or_else(|| var("AWS_SECURITY_TOKEN"));
            return Ok(Credentials { access_key, secret_key, session_token });
        }
        (None, None) => {}
        _ => return Err(CredError::Unsupported("partial credentials in the environment".into())),
    }
    if var("AWS_WEB_IDENTITY_TOKEN_FILE").is_some() {
        return Err(CredError::Unsupported("web identity credentials".into()));
    }
    let empty = Section::new();
    let conf = config.get(&profile).unwrap_or(&empty);
    let mut merged = conf.clone();
    if let Some(s) = shared.get(&profile) {
        merged.extend(s.clone());
    }
    for key in ["role_arn", "web_identity_token_file", "sso_start_url", "sso_session", "login_session"] {
        if merged.contains_key(key) {
            return Err(CredError::Unsupported(format!("profile {profile}'s {key}")));
        }
    }
    if let Some(c) = static_keys(shared.get(&profile).unwrap_or(&empty))? {
        return Ok(c);
    }
    if merged.contains_key("credential_process") {
        return Err(CredError::Unsupported(format!("profile {profile}'s credential_process")));
    }
    if let Some(c) = static_keys(conf)? {
        return Ok(c);
    }
    if var("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI").is_some() || var("AWS_CONTAINER_CREDENTIALS_FULL_URI").is_some() {
        return Err(CredError::Unsupported("container credentials".into()));
    }
    let imds_off = var("AWS_EC2_METADATA_DISABLED").is_some_and(|v| v.eq_ignore_ascii_case("true"));
    if !imds_off && let Some(c) = instance_role().await {
        return Ok(c);
    }
    Err(CredError::Failed("Unable to locate credentials".into()))
}

/// The EC2 instance role, through IMDSv2 (v1 when the token is refused), with
/// botocore's one attempt and one-second timeout — on a machine that isn't
/// an instance this costs that second, as it does in Python.
async fn instance_role() -> Option<Credentials> {
    let base = var("AWS_EC2_METADATA_SERVICE_ENDPOINT").unwrap_or_else(|| "http://169.254.169.254/".into());
    let base = base.trim_end_matches('/');
    let http = reqwest::Client::builder().timeout(Duration::from_secs(1)).no_proxy().build().ok()?;
    let token = match http
        .put(format!("{base}/latest/api/token"))
        .header("x-aws-ec2-metadata-token-ttl-seconds", "21600")
        .send()
        .await
    {
        Ok(r) if r.status().is_success() => r.text().await.ok(),
        Ok(r) if matches!(r.status().as_u16(), 403..=405) => None,
        Ok(_) => return None,
        Err(_) => None,
    };
    let get = |path: String| {
        let mut req = http.get(format!("{base}{path}"));
        if let Some(t) = &token {
            req = req.header("x-aws-ec2-metadata-token", t);
        }
        async move {
            let r = req.send().await.ok()?;
            if !r.status().is_success() {
                return None;
            }
            r.text().await.ok()
        }
    };
    const ROLES: &str = "/latest/meta-data/iam/security-credentials/";
    let role = get(ROLES.into()).await?;
    let role = role.lines().next()?.trim().to_string();
    let body: Value = serde_json::from_str(&get(format!("{ROLES}{role}")).await?).ok()?;
    let s = |k: &str| body.get(k).and_then(Value::as_str).map(str::to_string);
    Some(Credentials { access_key: s("AccessKeyId")?, secret_key: s("SecretAccessKey")?, session_token: s("Token") })
}

// ── SigV4 ────────────────────────────────────────────────────────────────────

/// RFC 3986's unreserved characters are the only ones left alone.
const UNRESERVED: &AsciiSet = &NON_ALPHANUMERIC.remove(b'-').remove(b'_').remove(b'.').remove(b'~');
/// The canonical path keeps its slashes.
const PATH: &AsciiSet = &UNRESERVED.remove(b'/');

/// A path segment as botocore puts a URI label in the URL.
pub fn encode_label(s: &str) -> String {
    utf8_percent_encode(s, UNRESERVED).to_string()
}

fn hmac(key: &[u8], data: &str) -> Vec<u8> {
    let mut mac = Hmac::<Sha256>::new_from_slice(key).expect("HMAC takes any key length");
    mac.update(data.as_bytes());
    mac.finalize().into_bytes().to_vec()
}

fn sha256_hex(data: &[u8]) -> String {
    hex::encode(Sha256::digest(data))
}

/// The headers to send with a request botocore would sign: `host`,
/// `x-amz-date`, the session token, and `authorization` over them and the
/// caller's `extra` headers (lowercase names).
#[allow(clippy::too_many_arguments)]
pub fn sign(
    creds: &Credentials,
    region: &str,
    service: &str,
    method: &str,
    url: &Url,
    extra: &[(&str, &str)],
    body: &[u8],
    now: chrono::DateTime<chrono::Utc>,
) -> Vec<(String, String)> {
    let amz_date = now.format("%Y%m%dT%H%M%SZ").to_string();
    let day = &amz_date[..8];
    let host = match url.port() {
        Some(p) => format!("{}:{p}", url.host_str().unwrap_or_default()),
        None => url.host_str().unwrap_or_default().to_string(),
    };
    let mut headers: Vec<(String, String)> = vec![("host".into(), host), ("x-amz-date".into(), amz_date.clone())];
    if let Some(t) = &creds.session_token {
        headers.push(("x-amz-security-token".into(), t.clone()));
    }
    headers.extend(extra.iter().map(|(k, v)| (k.to_string(), v.to_string())));
    headers.sort();

    // The path as sent is already encoded; the canonical form encodes it again.
    let path = if url.path().is_empty() { "/" } else { url.path() };
    let canonical_uri = utf8_percent_encode(path, PATH).to_string();
    let mut query: Vec<(String, String)> = url
        .query_pairs()
        .map(|(k, v)| (utf8_percent_encode(&k, UNRESERVED).to_string(), utf8_percent_encode(&v, UNRESERVED).to_string()))
        .collect();
    query.sort();
    let canonical_query = query.iter().map(|(k, v)| format!("{k}={v}")).collect::<Vec<_>>().join("&");
    let canonical_headers: String = headers
        .iter()
        .map(|(k, v)| format!("{k}:{}\n", v.split_whitespace().collect::<Vec<_>>().join(" ")))
        .collect();
    let signed = headers.iter().map(|(k, _)| k.as_str()).collect::<Vec<_>>().join(";");
    let canonical = format!("{method}\n{canonical_uri}\n{canonical_query}\n{canonical_headers}\n{signed}\n{}", sha256_hex(body));

    let scope = format!("{day}/{region}/{service}/aws4_request");
    let to_sign = format!("AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{}", sha256_hex(canonical.as_bytes()));
    let mut key = hmac(format!("AWS4{}", creds.secret_key).as_bytes(), day);
    for part in [region, service, "aws4_request"] {
        key = hmac(&key, part);
    }
    let signature = hex::encode(hmac(&key, &to_sign));
    headers.push((
        "authorization".into(),
        format!("AWS4-HMAC-SHA256 Credential={}/{scope}, SignedHeaders={signed}, Signature={signature}", creds.access_key),
    ));
    headers
}

// ── A call, as botocore makes it ─────────────────────────────────────────────

/// A failed call: botocore's message, or a failure whose wording botocore
/// would choose and this module can't reproduce.
#[derive(Debug, PartialEq)]
pub enum CallError {
    Failed(String),
    Unsupported(String),
}

/// botocore's legacy retry policy (boto3's default): these codes, these
/// statuses, and a dropped connection, for five attempts in all.
const THROTTLES: &[&str] = &[
    "Throttling",
    "ThrottlingException",
    "ThrottledException",
    "RequestThrottledException",
    "TooManyRequestsException",
    "ProvisionedThroughputExceededException",
    "TransactionInProgressException",
    "RequestLimitExceeded",
    "BandwidthLimitExceeded",
    "LimitExceededException",
    "RequestThrottled",
    "SlowDown",
    "PriorRequestNotComplete",
    "EC2ThrottledException",
];
const ATTEMPTS: u32 = 5;

/// `random.random() * 2 ** (attempt - 1)` seconds.
fn backoff(attempt: u32) -> Duration {
    let r = f64::from(uuid::Uuid::new_v4().as_u128() as u32) / f64::from(u32::MAX);
    Duration::from_secs_f64(r * f64::from(1u32 << (attempt - 1)))
}

/// A signed rest-json call: the parsed reply, or botocore's error for
/// `operation`. `body` is JSON for a POST, `None` for a GET.
pub async fn call(
    http: &reqwest::Client,
    creds: &Credentials,
    region: &str,
    service: &str,
    operation: &str,
    url: &Url,
    body: Option<&Value>,
) -> Result<Value, CallError> {
    let payload = body.map(|b| serde_json::to_vec(b).unwrap_or_default()).unwrap_or_default();
    let mut attempt = 1;
    loop {
        let extra: &[(&str, &str)] = if body.is_some() { &[("content-type", "application/json")] } else { &[] };
        let method = if body.is_some() { "POST" } else { "GET" };
        let headers = sign(creds, region, service, method, url, extra, &payload, chrono::Utc::now());
        let mut req = if body.is_some() { http.post(url.clone()).body(payload.clone()) } else { http.get(url.clone()) };
        for (k, v) in &headers {
            if k != "host" {
                req = req.header(k, v);
            }
        }
        let resp = match req.send().await {
            Ok(r) => r,
            Err(e) if e.is_connect() => {
                if attempt < ATTEMPTS {
                    tokio::time::sleep(backoff(attempt)).await;
                    attempt += 1;
                    continue;
                }
                return Err(CallError::Failed(format!("Could not connect to the endpoint URL: \"{url}\"")));
            }
            Err(e) => return Err(CallError::Unsupported(format!("{operation}: {e}"))),
        };
        let status = resp.status().as_u16();
        let error_type = resp.headers().get("x-amzn-errortype").and_then(|v| v.to_str().ok()).map(str::to_string);
        let text = resp.bytes().await.map_err(|e| CallError::Unsupported(format!("{operation}: {e}")))?;
        if (200..300).contains(&status) {
            return serde_json::from_slice(&text).map_err(|e| CallError::Unsupported(format!("{operation} reply: {e}")));
        }
        let (code, message) = error_parts(status, error_type.as_deref(), &text)?;
        let retryable = THROTTLES.contains(&code.as_str()) || matches!(status, 429 | 500 | 502 | 503 | 504 | 509);
        if retryable && attempt < ATTEMPTS {
            tokio::time::sleep(backoff(attempt)).await;
            attempt += 1;
            continue;
        }
        let retries = if retryable && attempt > 1 { format!(" (reached max retries: {})", attempt - 1) } else { String::new() };
        return Err(CallError::Failed(format!(
            "An error occurred ({code}) when calling the {operation} operation{retries}: {message}"
        )));
    }
}

/// botocore's rest-json error parse: the code from `x-amzn-errortype`, the
/// body's `code` or `__type`, or the status; the message from `message`.
pub(crate) fn error_parts(status: u16, error_type: Option<&str>, body: &[u8]) -> Result<(String, String), CallError> {
    let parsed: Value = if body.is_empty() {
        Value::Object(Default::default())
    } else {
        let text = std::str::from_utf8(body).map_err(|_| CallError::Unsupported("an error body that isn't UTF-8".into()))?;
        serde_json::from_str(text).unwrap_or_else(|_| serde_json::json!({ "message": text }))
    };
    let Value::Object(map) = parsed else {
        return Err(CallError::Unsupported("an error body that isn't an object".into()));
    };
    let message = map.get("message").or_else(|| map.get("Message")).map(pyjson::py_str).unwrap_or_default();
    let clean = |c: &str| {
        let c = c.split_once(':').map_or(c, |(a, _)| a);
        c.rsplit_once('#').map_or(c, |(_, b)| b).to_string()
    };
    let code = if let Some(t) = error_type {
        clean(t)
    } else if let Some(c) = map.get("code").or_else(|| map.get("Code")) {
        match c {
            Value::String(s) => clean(s),
            other => pyjson::py_str(other),
        }
    } else {
        match map.get("__type") {
            Some(Value::String(s)) => clean(s),
            Some(_) => return Err(CallError::Unsupported("a non-string __type".into())),
            None => status.to_string(),
        }
    };
    Ok((code, message))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// AWS's published example (the IAM `ListUsers` request of the SigV4
    /// documentation), signed with its example key.
    #[test]
    fn signs_the_documented_example() {
        let creds = Credentials {
            access_key: "AKIDEXAMPLE".into(),
            secret_key: "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY".into(),
            session_token: None,
        };
        let url = Url::parse("https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08").unwrap();
        let now = chrono::DateTime::parse_from_rfc3339("2015-08-30T12:36:00Z").unwrap().with_timezone(&chrono::Utc);
        let headers = sign(
            &creds,
            "us-east-1",
            "iam",
            "GET",
            &url,
            &[("content-type", "application/x-www-form-urlencoded; charset=utf-8")],
            b"",
            now,
        );
        let auth = &headers.iter().find(|(k, _)| k == "authorization").unwrap().1;
        assert_eq!(
            auth,
            "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/iam/aws4_request, \
             SignedHeaders=content-type;host;x-amz-date, \
             Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7"
        );
    }

    #[test]
    fn reads_the_shared_files_like_configparser() {
        let ini = "# c\n[default]\naws_access_key_id = A\nAWS_SECRET_ACCESS_KEY: B\ns3 =\n  max_concurrency = 2\n[profile dev]\nregion=x\n[sso-session s]\n";
        let profiles = config_profiles(parse_ini(ini));
        assert_eq!(profiles["default"]["aws_access_key_id"], "A");
        assert_eq!(profiles["default"]["aws_secret_access_key"], "B");
        assert_eq!(profiles["default"]["s3"], "\nmax_concurrency = 2");
        assert_eq!(profiles["dev"]["region"], "x");
        assert_eq!(profiles.len(), 2);
    }

    #[test]
    fn spells_errors_as_botocore_does() {
        let body = br#"{"message":"The security token included in the request is invalid."}"#;
        assert_eq!(
            error_parts(403, Some("UnrecognizedClientException:http://internal.amazon.com/"), body).unwrap(),
            ("UnrecognizedClientException".into(), "The security token included in the request is invalid.".into())
        );
        assert_eq!(error_parts(400, None, br#"{"__type":"com.amazon#ValidationException"}"#).unwrap(), ("ValidationException".into(), String::new()));
        assert_eq!(error_parts(502, None, b"<html>bad</html>").unwrap(), ("502".into(), "<html>bad</html>".into()));
    }
}
