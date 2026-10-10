//! OAuth sign-in for an MCP server reached by URL, as the MCP authorization
//! spec has a client do it.
//!
//! `jarvis-edge mcp login` (`cli/mcp.rs`) runs the flow: the server's 401
//! names its protected-resource metadata, that names the authorization
//! server, whose metadata gives the endpoints; jarvis registers itself there
//! (dynamic client registration) unless given a client id, sends the person
//! to sign in with PKCE and the `resource` the token is for, and trades the
//! code for tokens. They are kept in `mcp_oauth`, which no query exposes.
//!
//! The manager (`mod.rs`) puts the access token on every connection to the
//! server, refreshing it first when it has expired or the server refused it.

use std::collections::HashMap;
use std::sync::LazyLock;
use std::time::Duration;

use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use chrono::{NaiveDateTime, TimeDelta, Utc};
use reqwest::header::{ACCEPT, WWW_AUTHENTICATE};
use reqwest::{StatusCode, Url};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use sqlx::SqlitePool;

use crate::gql::codec::now_stored;

/// How close to its expiry a token is refreshed rather than sent.
const MARGIN: TimeDelta = TimeDelta::seconds(60);
const STAMP: &str = "%Y-%m-%d %H:%M:%S%.f";

/// One refresh at a time: a server that rotates refresh tokens refuses the
/// second use of one, and may revoke the sign-in for it.
static REFRESHING: LazyLock<tokio::sync::Mutex<()>> = LazyLock::new(|| tokio::sync::Mutex::new(()));

pub fn client() -> reqwest::Client {
    reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(10))
        .timeout(Duration::from_secs(30))
        .build()
        .expect("an HTTP client")
}

fn sent(e: reqwest::Error) -> String {
    crate::discovery::httpx_error(&e).unwrap_or_else(|| e.to_string())
}

// ── discovery ───────────────────────────────────────────────────────────────

/// Where a server sends people to sign in, and what for.
pub struct Discovered {
    /// What the token is for (the `resource` parameter).
    pub resource: String,
    pub authorization_endpoint: String,
    pub token_endpoint: String,
    pub registration_endpoint: Option<String>,
    pub scope: Option<String>,
}

/// `Bearer k="v", k2=v2` → its parameters.
fn challenge(header: &str) -> HashMap<String, String> {
    let rest = header.trim_start().get(6..).unwrap_or("");
    let mut out = HashMap::new();
    let mut chars = rest.chars().peekable();
    loop {
        while chars.peek().is_some_and(|c| *c == ',' || c.is_whitespace()) {
            chars.next();
        }
        let key: String = std::iter::from_fn(|| chars.next_if(|c| *c != '=' && *c != ',')).collect();
        if key.is_empty() || chars.next() != Some('=') {
            break;
        }
        let value: String = if chars.next_if_eq(&'"').is_some() {
            let mut v = String::new();
            while let Some(c) = chars.next() {
                match c {
                    '"' => break,
                    '\\' => v.extend(chars.next()),
                    c => v.push(c),
                }
            }
            v
        } else {
            std::iter::from_fn(|| chars.next_if(|c| *c != ',' && !c.is_whitespace())).collect()
        };
        out.insert(key.trim().to_ascii_lowercase(), value);
    }
    out
}

/// The URL as the token's audience: no fragment.
fn canonical(url: &Url) -> String {
    let mut url = url.clone();
    url.set_fragment(None);
    url.to_string()
}

fn origin(url: &Url) -> String {
    url.origin().ascii_serialization()
}

/// RFC 8414's well-known places for an issuer's metadata, then OpenID's.
fn metadata_urls(issuer: &Url) -> Vec<String> {
    let (origin, path) = (origin(issuer), issuer.path().trim_end_matches('/'));
    if path.is_empty() {
        vec![format!("{origin}/.well-known/oauth-authorization-server"), format!("{origin}/.well-known/openid-configuration")]
    } else {
        vec![
            format!("{origin}/.well-known/oauth-authorization-server{path}"),
            format!("{origin}/.well-known/openid-configuration{path}"),
            format!("{origin}{path}/.well-known/openid-configuration"),
        ]
    }
}

/// The first of `urls` that answers with a JSON object.
async fn first_json(client: &reqwest::Client, urls: &[String]) -> Option<Value> {
    for url in urls {
        let Ok(resp) = client.get(url).header(ACCEPT, "application/json").send().await else { continue };
        if !resp.status().is_success() {
            continue;
        }
        if let Ok(v @ Value::Object(_)) = resp.json::<Value>().await {
            return Some(v);
        }
    }
    None
}

/// Ask the server at `url` how to sign in to it.
pub async fn discover(client: &reqwest::Client, url: &str) -> Result<Discovered, String> {
    let mcp = Url::parse(url).map_err(|e| format!("invalid URL {url:?}: {e}"))?;
    let hello = json!({
        "jsonrpc": "2.0", "id": 0, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "jarvis", "version": "1"}},
    });
    let resp =
        client.post(mcp.clone()).header(ACCEPT, "application/json, text/event-stream").json(&hello).send().await.map_err(sent)?;
    if resp.status() != StatusCode::UNAUTHORIZED {
        return Err(format!(
            "{url} didn't ask for sign-in (HTTP {}). If it takes a token, add it with `--token` instead",
            resp.status().as_u16()
        ));
    }
    let params = resp
        .headers()
        .get_all(WWW_AUTHENTICATE)
        .iter()
        .filter_map(|v| v.to_str().ok())
        .find(|v| v.trim_start().get(..6).is_some_and(|s| s.eq_ignore_ascii_case("bearer")))
        .map(challenge)
        .unwrap_or_default();

    let path = mcp.path().trim_end_matches('/');
    let mut candidates: Vec<String> = params.get("resource_metadata").cloned().into_iter().collect();
    if !path.is_empty() {
        candidates.push(format!("{}/.well-known/oauth-protected-resource{path}", origin(&mcp)));
    }
    candidates.push(format!("{}/.well-known/oauth-protected-resource", origin(&mcp)));
    let resource_meta = first_json(client, &candidates).await;
    // Without protected-resource metadata (the 2025-03-26 spec), the server's
    // own origin is its authorization server.
    let (issuer, resource, scopes) = match &resource_meta {
        Some(m) => {
            let issuer = m["authorization_servers"][0].as_str().ok_or_else(|| format!("{url} names no authorization server"))?;
            let scopes = m["scopes_supported"].as_array().map(|s| s.iter().filter_map(Value::as_str).collect::<Vec<_>>().join(" "));
            (issuer.to_string(), m["resource"].as_str().map(String::from), scopes.filter(|s| !s.is_empty()))
        }
        None => (origin(&mcp), None, None),
    };
    let issuer_url = Url::parse(&issuer).map_err(|e| format!("invalid authorization server {issuer:?}: {e}"))?;
    let meta = first_json(client, &metadata_urls(&issuer_url))
        .await
        .ok_or_else(|| format!("couldn't find the sign-in details of {issuer} (its OAuth metadata)"))?;
    let endpoint = |key: &str| meta[key].as_str().map(String::from);
    Ok(Discovered {
        resource: resource.unwrap_or_else(|| canonical(&mcp)),
        authorization_endpoint: endpoint("authorization_endpoint").ok_or_else(|| format!("{issuer} has no authorization endpoint"))?,
        token_endpoint: endpoint("token_endpoint").ok_or_else(|| format!("{issuer} has no token endpoint"))?,
        registration_endpoint: endpoint("registration_endpoint"),
        scope: params.get("scope").cloned().or(scopes),
    })
}

// ── the client and the code ─────────────────────────────────────────────────

/// Who jarvis is to the authorization server.
pub struct Registration {
    pub client_id: String,
    pub client_secret: Option<String>,
    /// How the token endpoint wants the secret (`client_secret_post`, else Basic).
    pub auth_method: Option<String>,
}

/// Dynamic client registration (RFC 7591) for a sign-in returning to `redirect_uri`.
pub async fn register(client: &reqwest::Client, endpoint: &str, redirect_uri: &str) -> Result<Registration, String> {
    let body = json!({
        "client_name": "Jarvis",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    });
    let resp = client.post(endpoint).json(&body).send().await.map_err(sent)?;
    let status = resp.status();
    let answer: Value = resp.json().await.unwrap_or(Value::Null);
    if !status.is_success() {
        return Err(format!("registering jarvis was refused: {}", refusal(&answer, status)));
    }
    let text = |k: &str| answer[k].as_str().filter(|s| !s.is_empty()).map(String::from);
    Ok(Registration {
        client_id: text("client_id").ok_or("registering jarvis returned no client_id")?,
        client_secret: text("client_secret"),
        auth_method: text("token_endpoint_auth_method"),
    })
}

/// A PKCE verifier and its S256 challenge.
pub struct Pkce {
    pub verifier: String,
    pub challenge: String,
}

pub fn pkce() -> Pkce {
    let verifier = format!("{}{}", uuid::Uuid::new_v4().simple(), uuid::Uuid::new_v4().simple());
    let challenge = URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes()));
    Pkce { verifier, challenge }
}

pub fn state() -> String {
    uuid::Uuid::new_v4().simple().to_string()
}

/// Where to send the person to sign in.
pub fn authorize_url(d: &Discovered, client_id: &str, redirect_uri: &str, pkce: &Pkce, state: &str) -> Result<String, String> {
    let mut params = vec![
        ("response_type", "code"),
        ("client_id", client_id),
        ("redirect_uri", redirect_uri),
        ("code_challenge", &pkce.challenge),
        ("code_challenge_method", "S256"),
        ("state", state),
        ("resource", &d.resource),
    ];
    if let Some(scope) = &d.scope {
        params.push(("scope", scope));
    }
    Url::parse_with_params(&d.authorization_endpoint, &params)
        .map(String::from)
        .map_err(|e| format!("invalid authorization endpoint {:?}: {e}", d.authorization_endpoint))
}

// ── tokens ──────────────────────────────────────────────────────────────────

pub struct Tokens {
    pub access_token: String,
    pub refresh_token: Option<String>,
    pub expires_in: Option<i64>,
    pub scope: Option<String>,
}

/// An OAuth error answer, worded.
fn refusal(answer: &Value, status: StatusCode) -> String {
    let said = |k: &str| answer[k].as_str().filter(|s| !s.is_empty());
    said("error_description").or(said("error")).map_or_else(|| format!("HTTP {}", status.as_u16()), String::from)
}

async fn token_request(
    client: &reqwest::Client,
    endpoint: &str,
    reg: &Registration,
    mut form: Vec<(&str, String)>,
) -> Result<Tokens, String> {
    let mut req = client.post(endpoint).header(ACCEPT, "application/json");
    match (&reg.client_secret, reg.auth_method.as_deref()) {
        (Some(secret), Some("client_secret_post")) => {
            form.push(("client_id", reg.client_id.clone()));
            form.push(("client_secret", secret.clone()));
        }
        (Some(secret), _) => req = req.basic_auth(&reg.client_id, Some(secret)),
        (None, _) => form.push(("client_id", reg.client_id.clone())),
    }
    let resp = req.form(&form).send().await.map_err(sent)?;
    let status = resp.status();
    let answer: Value = resp.json().await.unwrap_or(Value::Null);
    if !status.is_success() {
        return Err(refusal(&answer, status));
    }
    let text = |k: &str| answer[k].as_str().filter(|s| !s.is_empty()).map(String::from);
    Ok(Tokens {
        access_token: text("access_token").ok_or("the token endpoint returned no access_token")?,
        refresh_token: text("refresh_token"),
        expires_in: answer["expires_in"].as_i64().or_else(|| answer["expires_in"].as_f64().map(|f| f as i64)),
        scope: text("scope"),
    })
}

/// The code a sign-in returned, traded for tokens.
pub async fn exchange(
    client: &reqwest::Client,
    d: &Discovered,
    reg: &Registration,
    code: &str,
    verifier: &str,
    redirect_uri: &str,
) -> Result<Tokens, String> {
    let form = vec![
        ("grant_type", "authorization_code".into()),
        ("code", code.into()),
        ("redirect_uri", redirect_uri.into()),
        ("code_verifier", verifier.into()),
        ("resource", d.resource.clone()),
    ];
    token_request(client, &d.token_endpoint, reg, form).await
}

// ── the stored sign-in ──────────────────────────────────────────────────────

#[derive(sqlx::FromRow)]
pub struct Grant {
    pub url: String,
    resource: String,
    token_endpoint: String,
    client_id: String,
    client_secret: Option<String>,
    token_auth_method: Option<String>,
    access_token: String,
    refresh_token: Option<String>,
    pub expires_at: Option<String>,
}

impl Grant {
    fn registration(&self) -> Registration {
        Registration {
            client_id: self.client_id.clone(),
            client_secret: self.client_secret.clone(),
            auth_method: self.token_auth_method.clone(),
        }
    }

    pub fn can_refresh(&self) -> bool {
        self.refresh_token.is_some()
    }

    fn expires(&self) -> Option<chrono::DateTime<Utc>> {
        self.expires_at.as_deref().and_then(|s| NaiveDateTime::parse_from_str(s, STAMP).ok()).map(|t| t.and_utc())
    }

    fn fresh(&self) -> bool {
        self.expires().is_none_or(|at| at - MARGIN > Utc::now())
    }
}

fn expiry(tokens: &Tokens) -> Option<String> {
    tokens.expires_in.map(|s| (Utc::now() + TimeDelta::seconds(s)).format("%Y-%m-%d %H:%M:%S%.6f").to_string())
}

pub async fn load(pool: &SqlitePool, server: &str) -> sqlx::Result<Option<Grant>> {
    sqlx::query_as(
        "SELECT url, resource, token_endpoint, client_id, client_secret, token_auth_method, access_token, refresh_token, \
         expires_at FROM mcp_oauth WHERE server = ?",
    )
    .bind(server)
    .fetch_optional(pool)
    .await
}

/// A finished sign-in, replacing any earlier one for `server`.
pub async fn save(
    pool: &SqlitePool,
    server: &str,
    url: &str,
    d: &Discovered,
    reg: &Registration,
    tokens: &Tokens,
) -> sqlx::Result<()> {
    let now = now_stored();
    sqlx::query(
        "INSERT INTO mcp_oauth (server, url, resource, token_endpoint, client_id, client_secret, token_auth_method, \
         access_token, refresh_token, expires_at, scope, created_at, updated_at) \
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) \
         ON CONFLICT (server) DO UPDATE SET url = excluded.url, resource = excluded.resource, \
         token_endpoint = excluded.token_endpoint, client_id = excluded.client_id, client_secret = excluded.client_secret, \
         token_auth_method = excluded.token_auth_method, access_token = excluded.access_token, \
         refresh_token = excluded.refresh_token, expires_at = excluded.expires_at, scope = excluded.scope, \
         updated_at = excluded.updated_at",
    )
    .bind(server)
    .bind(url)
    .bind(&d.resource)
    .bind(&d.token_endpoint)
    .bind(&reg.client_id)
    .bind(&reg.client_secret)
    .bind(&reg.auth_method)
    .bind(&tokens.access_token)
    .bind(&tokens.refresh_token)
    .bind(expiry(tokens))
    .bind(tokens.scope.as_ref().or(d.scope.as_ref()))
    .bind(&now)
    .bind(&now)
    .execute(pool)
    .await?;
    Ok(())
}

/// Sign out of `server`; whether it was signed in.
pub async fn forget(pool: &SqlitePool, server: &str) -> sqlx::Result<bool> {
    Ok(sqlx::query("DELETE FROM mcp_oauth WHERE server = ?").bind(server).execute(pool).await?.rows_affected() > 0)
}

fn login_again(server: &str) -> String {
    format!("run `jarvis-edge mcp login {server}` to sign in again")
}

/// The access token for `server` at `url`, if it has a sign-in: the stored
/// one, refreshed first when it is about to expire or is `refused` (the one
/// the server just turned away).
pub async fn bearer(pool: &SqlitePool, server: &str, url: &str, refused: Option<&str>) -> Result<Option<String>, String> {
    let stale = |g: &Grant| !g.fresh() || refused == Some(g.access_token.as_str());
    let Some(grant) = load(pool, server).await.map_err(|e| e.to_string())? else { return Ok(None) };
    if grant.url != url {
        return Err(format!("its sign-in was for {} — {}", grant.url, login_again(server)));
    }
    if !stale(&grant) {
        return Ok(Some(grant.access_token));
    }
    let _one = REFRESHING.lock().await;
    // Another caller may have refreshed it while this one waited.
    let Some(grant) = load(pool, server).await.map_err(|e| e.to_string())? else { return Ok(None) };
    if !stale(&grant) {
        return Ok(Some(grant.access_token));
    }
    let Some(refresh_token) = grant.refresh_token.clone() else {
        return Err(format!("its sign-in expired — {}", login_again(server)));
    };
    let form = vec![
        ("grant_type", "refresh_token".into()),
        ("refresh_token", refresh_token.clone()),
        ("resource", grant.resource.clone()),
    ];
    let tokens = token_request(&client(), &grant.token_endpoint, &grant.registration(), form)
        .await
        .map_err(|e| format!("its sign-in couldn't be renewed ({e}) — {}", login_again(server)))?;
    sqlx::query(
        "UPDATE mcp_oauth SET access_token = ?, refresh_token = ?, expires_at = ?, scope = COALESCE(?, scope), \
         updated_at = ? WHERE server = ?",
    )
    .bind(&tokens.access_token)
    .bind(tokens.refresh_token.as_ref().unwrap_or(&refresh_token))
    .bind(expiry(&tokens))
    .bind(&tokens.scope)
    .bind(now_stored())
    .bind(server)
    .execute(pool)
    .await
    .map_err(|e| e.to_string())?;
    Ok(Some(tokens.access_token))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn challenges_parse_quoted_and_bare_values() {
        let p = challenge(r#"Bearer error="invalid_token", resource_metadata="https://x.test/.well-known/a?b=1", scope=mcp"#);
        assert_eq!(p["resource_metadata"], "https://x.test/.well-known/a?b=1");
        assert_eq!(p["scope"], "mcp");
        assert_eq!(p["error"], "invalid_token");
        assert_eq!(challenge(r#"Bearer realm="a \"b\"""#)["realm"], r#"a "b""#);
        assert!(challenge("Bearer").is_empty());
    }

    #[test]
    fn metadata_is_looked_for_where_rfc_8414_says() {
        let at = |s: &str| metadata_urls(&Url::parse(s).unwrap());
        assert_eq!(
            at("https://a.test"),
            ["https://a.test/.well-known/oauth-authorization-server", "https://a.test/.well-known/openid-configuration"]
        );
        assert_eq!(
            at("https://a.test/tenant/"),
            [
                "https://a.test/.well-known/oauth-authorization-server/tenant",
                "https://a.test/.well-known/openid-configuration/tenant",
                "https://a.test/tenant/.well-known/openid-configuration",
            ]
        );
    }

    #[test]
    fn pkce_challenge_is_s256_of_the_verifier() {
        let p = pkce();
        assert_eq!(p.verifier.len(), 64);
        assert_eq!(p.challenge, URL_SAFE_NO_PAD.encode(Sha256::digest(p.verifier.as_bytes())));
        // RFC 7636's example.
        let v = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk";
        assert_eq!(URL_SAFE_NO_PAD.encode(Sha256::digest(v.as_bytes())), "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM");
    }

    #[test]
    fn the_authorize_url_carries_pkce_state_and_resource() {
        let d = Discovered {
            resource: "https://m.test/mcp".into(),
            authorization_endpoint: "https://a.test/authorize?tenant=1".into(),
            token_endpoint: "https://a.test/token".into(),
            registration_endpoint: None,
            scope: Some("mcp read".into()),
        };
        let p = Pkce { verifier: "v".into(), challenge: "c".into() };
        let url = Url::parse(&authorize_url(&d, "id", "http://localhost:5/callback", &p, "s").unwrap()).unwrap();
        let q: HashMap<_, _> = url.query_pairs().into_owned().collect();
        assert_eq!(q["tenant"], "1");
        assert_eq!(q["code_challenge"], "c");
        assert_eq!(q["code_challenge_method"], "S256");
        assert_eq!(q["resource"], "https://m.test/mcp");
        assert_eq!(q["scope"], "mcp read");
        assert_eq!(q["redirect_uri"], "http://localhost:5/callback");
    }
}
