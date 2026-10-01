//! Wire formats that must match the Python server byte for byte: Relay global
//! IDs, the `DateTime` scalar, and the message-connection cursor. The frontend
//! stores all three and hands them back, and the `jarvis` SDK builds global IDs
//! by hand (`tools/sdk.py:_global_id`), so a drift here breaks clients that
//! never talk to the edge directly.

use async_graphql::{ID, InputValueError, InputValueResult, Scalar, ScalarType, Value};
use base64::Engine;
use base64::engine::general_purpose::{STANDARD, STANDARD_NO_PAD, URL_SAFE};

/// `base64("TypeName:rawId")` — strawberry's `relay.to_base64`.
pub fn global_id(type_name: &str, raw_id: &str) -> ID {
    ID(STANDARD.encode(format!("{type_name}:{raw_id}")))
}

/// Inverse of [`global_id`]: `(type_name, raw_id)`.
pub fn decode_global_id(id: &str) -> Result<(String, String), String> {
    let bytes = STANDARD
        .decode(id)
        .or_else(|_| STANDARD_NO_PAD.decode(id))
        .map_err(|e| format!("invalid global id {id:?}: {e}"))?;
    let text = String::from_utf8(bytes).map_err(|e| format!("invalid global id {id:?}: {e}"))?;
    match text.split_once(':') {
        Some((ty, raw)) => Ok((ty.to_string(), raw.to_string())),
        None => Err(format!("invalid global id {id:?}: expected TypeName:id")),
    }
}

/// strawberry's `DateTime`, which serializes with Python's `isoformat()`.
///
/// Decodes straight from a SQLAlchemy `DATETIME` column, so row structs can
/// hold it directly.
#[derive(Clone, Debug)]
pub struct DateTime(pub String);

impl sqlx::Type<sqlx::Sqlite> for DateTime {
    fn type_info() -> sqlx::sqlite::SqliteTypeInfo {
        <String as sqlx::Type<sqlx::Sqlite>>::type_info()
    }

    fn compatible(ty: &sqlx::sqlite::SqliteTypeInfo) -> bool {
        <String as sqlx::Type<sqlx::Sqlite>>::compatible(ty)
    }
}

impl<'r> sqlx::Decode<'r, sqlx::Sqlite> for DateTime {
    fn decode(value: sqlx::sqlite::SqliteValueRef<'r>) -> Result<Self, sqlx::error::BoxDynError> {
        Ok(iso_from_db(<&str as sqlx::Decode<sqlx::Sqlite>>::decode(value)?))
    }
}

impl DateTime {
    /// The UTC-aware form: `isoformat()` of a datetime given `tzinfo=utc`,
    /// which is what `types/approval.py:_utc` hands strawberry.
    pub fn utc(&self) -> DateTime {
        DateTime(format!("{}+00:00", self.0))
    }
}

#[Scalar(name = "DateTime")]
impl ScalarType for DateTime {
    fn parse(value: Value) -> InputValueResult<Self> {
        match value {
            Value::String(s) => Ok(DateTime(s)),
            other => Err(InputValueError::expected_type(other)),
        }
    }

    fn to_value(&self) -> Value {
        Value::String(self.0.clone())
    }
}

/// A timestamp as SQLAlchemy stores it, split into its parts.
struct Stamp<'a> {
    date: &'a str,
    time: &'a str,
    micros: u32,
}

/// Parse SQLAlchemy's SQLite `DATETIME` text (`YYYY-MM-DD HH:MM:SS.ffffff`).
/// Also takes a `T` separator and a short or missing fraction, as its own
/// result processor does. `None` for anything else.
fn parse_stamp(s: &str) -> Option<Stamp<'_>> {
    let (date, rest) = s.split_at_checked(10)?;
    let rest = rest.strip_prefix([' ', 'T'])?;
    let (time, frac) = match rest.split_once('.') {
        Some((t, f)) => (t, f),
        None => (rest, ""),
    };
    if date.len() != 10 || time.len() != 8 || !frac.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let mut padded = [b'0'; 6];
    for (slot, b) in padded.iter_mut().zip(frac.bytes()) {
        *slot = b;
    }
    let micros = std::str::from_utf8(&padded).ok()?.parse().ok()?;
    Some(Stamp { date, time, micros })
}

/// Stored text → what strawberry emits. Python's `isoformat()` drops the
/// fraction entirely when it is zero, so this does too.
pub fn iso_from_db(stored: &str) -> DateTime {
    match parse_stamp(stored) {
        Some(Stamp { date, time, micros: 0 }) => DateTime(format!("{date}T{time}")),
        Some(Stamp { date, time, micros }) => DateTime(format!("{date}T{time}.{micros:06}")),
        // Unknown shape: pass it through rather than invent a value.
        None => DateTime(stored.replacen(' ', "T", 1)),
    }
}

/// Now, as SQLAlchemy stores a `_now()` default: UTC, space-separated, six
/// fractional digits.
pub fn now_stored() -> String {
    chrono::Utc::now().format("%Y-%m-%d %H:%M:%S%.6f").to_string()
}

/// A fresh row id, as `str(uuid4())`.
pub fn new_id() -> String {
    uuid::Uuid::new_v4().to_string()
}

/// An ISO timestamp (as a cursor carries it) → the stored text it compares
/// against. SQLAlchemy always binds six fractional digits.
pub fn db_from_iso(iso: &str) -> Option<String> {
    let Stamp { date, time, micros } = parse_stamp(iso)?;
    Some(format!("{date} {time}.{micros:06}"))
}

/// Opaque message cursor: urlsafe `base64("{isoformat}|{id}")`, padded —
/// `server/graphql/types/conversation.py:_encode_cursor`.
pub fn encode_cursor(created_at_iso: &str, id: &str) -> String {
    URL_SAFE.encode(format!("{created_at_iso}|{id}"))
}

/// Inverse of [`encode_cursor`]: `(stored created_at, id)`.
pub fn decode_cursor(cursor: &str) -> Result<(String, String), String> {
    let bytes = URL_SAFE.decode(cursor).map_err(|e| format!("invalid cursor: {e}"))?;
    let text = String::from_utf8(bytes).map_err(|e| format!("invalid cursor: {e}"))?;
    let (ts, id) = text.split_once('|').ok_or("invalid cursor")?;
    let stored = db_from_iso(ts).ok_or_else(|| format!("invalid cursor timestamp {ts:?}"))?;
    Ok((stored, id.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn global_id_matches_strawberry() {
        // base64.b64encode(b"Conversation:abc").decode()
        assert_eq!(global_id("Conversation", "abc").0, "Q29udmVyc2F0aW9uOmFiYw==");
        let (ty, raw) = decode_global_id("Q29udmVyc2F0aW9uOmFiYw==").unwrap();
        assert_eq!((ty.as_str(), raw.as_str()), ("Conversation", "abc"));
        // A raw id may itself contain ':' — only the first one splits.
        let id = global_id("Conversation", "automation_x:y");
        assert_eq!(decode_global_id(&id).unwrap().1, "automation_x:y");
    }

    #[test]
    fn datetime_matches_isoformat() {
        assert_eq!(iso_from_db("2026-04-10 03:24:32.660374").0, "2026-04-10T03:24:32.660374");
        // isoformat() omits a zero fraction.
        assert_eq!(iso_from_db("2026-04-10 03:24:32.000000").0, "2026-04-10T03:24:32");
        assert_eq!(iso_from_db("2026-04-10 03:24:32").0, "2026-04-10T03:24:32");
        assert_eq!(iso_from_db("2026-04-10 03:24:32.5").0, "2026-04-10T03:24:32.500000");
    }

    #[test]
    fn cursor_round_trips_to_stored_form() {
        let c = encode_cursor("2026-04-10T03:24:32", "m1");
        assert_eq!(decode_cursor(&c).unwrap(), ("2026-04-10 03:24:32.000000".into(), "m1".into()));
    }
}
