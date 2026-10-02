//! Cron schedules, computed exactly as the Python side computed them:
//! APScheduler 3.11's `CronTrigger.from_crontab`, fed through
//! `core/scheduler.py:normalize_crontab`, in a `zoneinfo` timezone.
//!
//! "Exactly" includes the quirks, because a schedule is a promise the UI
//! shows (`Automation.nextRunAt`) and the scheduler keeps:
//!
//! - the day-of-month and day-of-week fields are ANDed, not ORed;
//! - a fire time inside a spring-forward gap keeps its wall-clock time and
//!   takes the pre-transition offset (`zoneinfo`'s `fold=0`), so `30 2 * * *`
//!   fires at 03:30 local that day and reports `02:30-06:00`;
//! - ambiguous fall-back times resolve by `fold`, as `zoneinfo` does.
//!
//! So this is a port of the algorithm, not a cron library: field by field,
//! with Python's datetime-and-zoneinfo arithmetic modelled by [`Wall`]. It is
//! diffed against APScheduler over thousands of cases in
//! `tests/test_edge_schedule.py`.

use chrono::offset::LocalResult;
use chrono::{DateTime, Datelike, Duration, FixedOffset, NaiveDate, NaiveDateTime, Offset, TimeZone, Timelike, Utc};
use chrono_tz::Tz;

/// A Python aware `datetime` with a `ZoneInfo` tzinfo: wall-clock fields,
/// `fold`, and the zone. Wall fields may name a time that doesn't exist (a
/// spring-forward gap) — Python allows that, and APScheduler produces them.
#[derive(Clone, Copy, Debug)]
pub struct Wall {
    pub naive: NaiveDateTime,
    pub fold: bool,
    pub tz: Tz,
}

impl Wall {
    /// `utcoffset()`, PEP 495 rules: in a fall-back overlap `fold` picks the
    /// occurrence; in a spring-forward gap `fold=0` takes the offset from
    /// before the transition and `fold=1` the one after.
    pub fn offset(&self) -> FixedOffset {
        match self.tz.offset_from_local_datetime(&self.naive) {
            LocalResult::Single(o) => o.fix(),
            LocalResult::Ambiguous(a, b) => {
                let (a, b) = (a.fix(), b.fix());
                // The first occurrence is the one under the larger offset.
                let (first, second) = if a.local_minus_utc() >= b.local_minus_utc() { (a, b) } else { (b, a) };
                if self.fold { second } else { first }
            }
            LocalResult::None => {
                let before = self.tz.offset_from_utc_datetime(&(self.naive - Duration::hours(24))).fix();
                let after = self.tz.offset_from_utc_datetime(&(self.naive + Duration::hours(24))).fix();
                if self.fold { after } else { before }
            }
        }
    }

    pub fn to_utc(self) -> DateTime<Utc> {
        Utc.from_utc_datetime(&(self.naive - Duration::seconds(i64::from(self.offset().local_minus_utc()))))
    }

    /// `instant.astimezone(tz)`: a real wall time, `fold=1` for the second
    /// occurrence of an ambiguous one.
    pub fn from_utc(instant: DateTime<Utc>, tz: Tz) -> Wall {
        let offset = tz.offset_from_utc_datetime(&instant.naive_utc()).fix();
        let naive = instant.naive_utc() + Duration::seconds(i64::from(offset.local_minus_utc()));
        let fold = match tz.offset_from_local_datetime(&naive) {
            LocalResult::Ambiguous(a, b) => {
                let smaller = a.fix().local_minus_utc().min(b.fix().local_minus_utc());
                offset.local_minus_utc() == smaller
            }
            _ => false,
        };
        Wall { naive, fold, tz }
    }

    /// `datetime_utc_add`: arithmetic on the instant, read back as wall time.
    fn utc_add(&self, delta: Duration) -> Wall {
        Wall::from_utc(self.to_utc() + delta, self.tz)
    }

    /// `isoformat()`: the wall time, a fraction only when there is one, and
    /// the offset this wall time resolves to.
    pub fn isoformat(&self) -> String {
        let mut out = self.naive.format("%Y-%m-%dT%H:%M:%S").to_string();
        let micros = self.naive.and_utc().timestamp_subsec_micros();
        if micros != 0 {
            out.push_str(&format!(".{micros:06}"));
        }
        let secs = self.offset().local_minus_utc();
        let sign = if secs < 0 { '-' } else { '+' };
        let secs = secs.abs();
        out.push_str(&format!("{sign}{:02}:{:02}", secs / 3600, secs / 60 % 60));
        if secs % 60 != 0 {
            out.push_str(&format!(":{:02}", secs % 60));
        }
        out
    }
}

// ── fields ─────────────────────────────────────────────────────────────────

const YEAR: usize = 0;
const MONTH: usize = 1;
const DAY: usize = 2;
const WEEK: usize = 3;
const DAY_OF_WEEK: usize = 4;
const HOUR: usize = 5;
const MINUTE: usize = 6;
const SECOND: usize = 7;

const NAMES: [&str; 8] = ["year", "month", "day", "week", "day_of_week", "hour", "minute", "second"];
const MIN: [i64; 8] = [1970, 1, 1, 1, 0, 0, 0, 0];
const MAX: [i64; 8] = [9999, 12, 31, 53, 6, 23, 59, 59];
/// Fields that map onto a datetime attribute; week and weekday are derived.
const REAL: [bool; 8] = [true, true, true, false, false, true, true, true];

const WEEKDAYS: [&str; 7] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const MONTHS: [&str; 12] = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];

fn days_in_month(year: i32, month: u32) -> i64 {
    let (y, m) = if month == 12 { (year + 1, 1) } else { (year, month + 1) };
    let first_next = NaiveDate::from_ymd_opt(y, m, 1).expect("valid month");
    let first = NaiveDate::from_ymd_opt(year, month, 1).expect("valid month");
    (first_next - first).num_days()
}

fn get_value(d: &NaiveDateTime, field: usize) -> i64 {
    match field {
        YEAR => i64::from(d.year()),
        MONTH => i64::from(d.month()),
        DAY => i64::from(d.day()),
        WEEK => i64::from(d.iso_week().week()),
        DAY_OF_WEEK => i64::from(d.weekday().num_days_from_monday()),
        HOUR => i64::from(d.hour()),
        MINUTE => i64::from(d.minute()),
        _ => i64::from(d.second()),
    }
}

fn get_max(d: &NaiveDateTime, field: usize) -> i64 {
    if field == DAY { days_in_month(d.year(), d.month()) } else { MAX[field] }
}

#[derive(Clone, Debug, PartialEq)]
enum Expr {
    /// `*` and `*/step`.
    All { step: Option<i64> },
    /// `a`, `a-b`, `a/step`, `a-b/step` — and month or weekday names.
    Range { first: i64, last: Option<i64>, step: Option<i64> },
    /// `last`, day of month only.
    LastDay,
}

impl Expr {
    fn next_value(&self, d: &NaiveDateTime, field: usize) -> Option<i64> {
        let (min, max, value) = (MIN[field], get_max(d, field), get_value(d, field));
        let next = match *self {
            Expr::LastDay => return Some(days_in_month(d.year(), d.month())),
            Expr::All { step } => {
                let start = value.max(min);
                match step {
                    None => start,
                    Some(step) => start + (step - (start - min)).rem_euclid(step),
                }
            }
            Expr::Range { first, last, step } => {
                let min = min.max(first);
                let max = last.map_or(max, |last| max.min(last));
                let next = min.max(value);
                let next = match step {
                    None => next,
                    Some(step) => next + (step - (next - min)).rem_euclid(step),
                };
                return (next <= max).then_some(next);
            }
        };
        (next <= max).then_some(next)
    }
}

/// `asint`: digits to a number. Too large for an i64 is out of every range,
/// which is the error Python would raise for it.
fn number(digits: &str) -> Result<i64, String> {
    digits.parse().map_err(|_| format!("the value ({digits}) is out of range"))
}

fn all_digits(s: &str) -> bool {
    !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit())
}

/// `AllExpression.value_re`: `\*(?:/(\d+))?$`.
fn parse_all(s: &str) -> Option<Option<&str>> {
    let rest = s.strip_prefix('*')?;
    if rest.is_empty() {
        return Some(None);
    }
    let step = rest.strip_prefix('/')?;
    all_digits(step).then_some(Some(step))
}

/// `RangeExpression.value_re`: `(\d+)(?:-(\d+))?(?:/(\d+))?$`.
fn parse_range(s: &str) -> Option<(&str, Option<&str>, Option<&str>)> {
    let (body, step) = match s.split_once('/') {
        Some((body, step)) => (body, Some(step)),
        None => (s, None),
    };
    if step.is_some_and(|st| !all_digits(st)) {
        return None;
    }
    let (first, last) = match body.split_once('-') {
        Some((first, last)) => (first, Some(last)),
        None => (body, None),
    };
    (all_digits(first) && last.is_none_or(all_digits)).then_some((first, last, step))
}

/// `(?P<first>[a-z]+)(?:-(?P<last>[a-z]+))?`, case-insensitive, matched as
/// a prefix — anything after it (a `/2`) is silently ignored, as there.
fn parse_names(s: &str) -> Option<(&str, Option<&str>)> {
    let letters = |t: &str| t.bytes().take_while(u8::is_ascii_alphabetic).count();
    let n = letters(s);
    if n == 0 {
        return None;
    }
    let first = &s[..n];
    let last = s[n..].strip_prefix('-').and_then(|rest| {
        let m = letters(rest);
        (m > 0).then(|| &rest[..m])
    });
    Some((first, last))
}

fn validate(expr: &Expr, field: usize) -> Result<(), String> {
    let (min, max) = (MIN[field], MAX[field]);
    let range_error = |step: i64, range: i64| {
        format!("the step value ({step}) is higher than the total range of the expression ({range})")
    };
    match *expr {
        Expr::All { step: Some(step) } if step > max - min => Err(range_error(step, max - min)),
        Expr::Range { first, last, step } => {
            if let Some(step) = step.filter(|&s| s > max - min) {
                return Err(range_error(step, max - min));
            }
            if first < min {
                return Err(format!("the first value ({first}) is lower than the minimum value ({min})"));
            }
            if let Some(last) = last.filter(|&l| l > max) {
                return Err(format!("the last value ({last}) is higher than the maximum value ({max})"));
            }
            // `(self.last or MAX) - first`: a last of 0 counts as absent.
            let range = last.filter(|&l| l != 0).unwrap_or(max) - first;
            match step {
                Some(step) if step > range => Err(range_error(step, range)),
                _ => Ok(()),
            }
        }
        _ => Ok(()),
    }
}

fn range_expr(first: i64, last: Option<i64>, step: Option<i64>) -> Result<Expr, String> {
    if step == Some(0) {
        return Err("Increment must be higher than 0".into());
    }
    let last = if last.is_none() && step.is_none() { Some(first) } else { last };
    if last.is_some_and(|last| first > last) {
        return Err("The minimum value in a range must not be higher than the maximum".into());
    }
    Ok(Expr::Range { first, last, step })
}

fn named(names: &[&str], first: &str, last: Option<&str>, base: i64, kind: &str) -> Result<Expr, String> {
    let index = |name: &str| {
        names
            .iter()
            .position(|n| n.eq_ignore_ascii_case(name))
            .map(|i| i as i64 + base)
            .ok_or_else(|| format!("Invalid {kind} name \"{name}\""))
    };
    range_expr(index(first)?, last.map(index).transpose()?, None)
}

/// One comma-separated piece of a field, by the field's compilers in order.
fn compile_one(s: &str, field: usize) -> Result<Expr, String> {
    let expr = if let Some(step) = parse_all(s) {
        let step = step.map(number).transpose()?;
        if step == Some(0) {
            return Err("Increment must be higher than 0".into());
        }
        Expr::All { step }
    } else if let Some((first, last, step)) = parse_range(s) {
        range_expr(number(first)?, last.map(number).transpose()?, step.map(number).transpose()?)?
    } else if let (MONTH, Some((first, last))) = (field, parse_names(s)) {
        named(&MONTHS, first, last, 1, "month")?
    } else if let (DAY_OF_WEEK, Some((first, last))) = (field, parse_names(s)) {
        named(&WEEKDAYS, first, last, 0, "weekday")?
    } else if field == DAY && s.get(..4).is_some_and(|p| p.eq_ignore_ascii_case("last")) {
        Expr::LastDay
    } else {
        return Err(format!("Unrecognized expression \"{s}\" for field \"{}\"", NAMES[field]));
    };
    validate(&expr, field).map_err(|e| format!("Error validating expression '{s}': {e}"))?;
    Ok(expr)
}

fn compile(s: &str, field: usize) -> Result<Vec<Expr>, String> {
    s.trim().split(',').map(|piece| compile_one(piece.trim(), field)).collect()
}

/// A compiled `CronTrigger`.
#[derive(Clone, Debug)]
pub struct Trigger {
    fields: [Vec<Expr>; 8],
    tz: Tz,
}

impl Trigger {
    /// `CronTrigger.from_crontab(normalize_crontab(expr), timezone=tz)` —
    /// what `core/scheduler.py:_cron` builds.
    pub fn parse(expr: &str, tz: Tz) -> Result<Self, String> {
        Self::from_crontab(&normalize_crontab(expr), tz)
    }

    fn from_crontab(expr: &str, tz: Tz) -> Result<Self, String> {
        let v: Vec<&str> = expr.split_whitespace().collect();
        if v.len() != 5 {
            return Err(format!("Wrong number of fields; got {}, expected 5", v.len()));
        }
        // year and week default to `*`; second, after the last given field,
        // to its DEFAULT_VALUE of 0.
        Ok(Trigger {
            fields: [
                compile("*", YEAR)?,
                compile(v[3], MONTH)?,
                compile(v[2], DAY)?,
                compile("*", WEEK)?,
                compile(v[4], DAY_OF_WEEK)?,
                compile(v[1], HOUR)?,
                compile(v[0], MINUTE)?,
                compile("0", SECOND)?,
            ],
            tz,
        })
    }

    fn next_value(&self, d: &NaiveDateTime, field: usize) -> Option<i64> {
        self.fields[field].iter().filter_map(|e| e.next_value(d, field)).min()
    }

    fn wall(&self, values: &[i64; 8], fold: bool) -> Wall {
        let date = NaiveDate::from_ymd_opt(values[YEAR] as i32, values[MONTH] as u32, values[DAY] as u32)
            .expect("fields stay within the month");
        let naive = date
            .and_hms_opt(values[HOUR] as u32, values[MINUTE] as u32, values[SECOND] as u32)
            .expect("fields stay within the day");
        Wall { naive, fold, tz: self.tz }
    }

    /// `_increment_field_value`: bump `fieldnum` (or the nearest real field
    /// above it that isn't at its max), reset everything below, and move by
    /// the naive difference on the UTC timeline.
    fn increment(&self, date: Wall, mut fieldnum: i64) -> (Wall, i64) {
        let mut values = [0i64; 8];
        let mut i: i64 = 0;
        while i < 8 {
            if i < 0 {
                // Past year 9999; Python would wrap its index here.
                return (date, -1);
            }
            let f = i as usize;
            if !REAL[f] {
                if i == fieldnum {
                    fieldnum -= 1;
                    i -= 1;
                } else {
                    i += 1;
                }
                continue;
            }
            if i < fieldnum {
                values[f] = get_value(&date.naive, f);
                i += 1;
            } else if i > fieldnum {
                values[f] = MIN[f];
                i += 1;
            } else {
                let value = get_value(&date.naive, f);
                if value == get_max(&date.naive, f) {
                    fieldnum -= 1;
                    i -= 1;
                } else {
                    values[f] = value + 1;
                    i += 1;
                }
            }
        }
        let difference = self.wall(&values, false).naive - date.naive;
        (date.utc_add(difference), fieldnum)
    }

    /// `_set_field_value`: the wall time with one field set and those below
    /// reset — keeping `fold`, normalizing nothing.
    fn set(&self, date: Wall, fieldnum: usize, value: i64) -> Wall {
        let mut values = [0i64; 8];
        for f in (0..8).filter(|&f| REAL[f]) {
            values[f] = match f.cmp(&fieldnum) {
                std::cmp::Ordering::Less => get_value(&date.naive, f),
                std::cmp::Ordering::Greater => MIN[f],
                std::cmp::Ordering::Equal => value,
            };
        }
        self.wall(&values, date.fold)
    }

    /// `get_next_fire_time(previous_fire_time, now)`.
    pub fn next_fire(&self, previous: Option<&Wall>, now: DateTime<Utc>) -> Option<Wall> {
        let start = match previous {
            Some(prev) => {
                let start = Wall::from_utc(now.min(prev.utc_add(Duration::microseconds(1)).to_utc()), self.tz);
                // Same zone on both sides, so Python compares wall fields.
                if start.naive == prev.naive { start.utc_add(Duration::microseconds(1)) } else { start }
            }
            None => Wall::from_utc(now, self.tz),
        };
        // datetime_ceil
        let micros = i64::from(start.naive.and_utc().timestamp_subsec_micros());
        let mut next = if micros > 0 { start.utc_add(Duration::microseconds(1_000_000 - micros)) } else { start };

        let mut fieldnum: i64 = 0;
        // The algorithm always terminates; this only bounds a bug.
        for _ in 0..100_000 {
            if !(0..8).contains(&fieldnum) {
                break;
            }
            let f = fieldnum as usize;
            let current = get_value(&next.naive, f);
            match self.next_value(&next.naive, f) {
                None => (next, fieldnum) = self.increment(next, fieldnum - 1),
                Some(value) if value > current => {
                    if REAL[f] {
                        next = self.set(next, f, value);
                        fieldnum += 1;
                    } else {
                        (next, fieldnum) = self.increment(next, fieldnum);
                    }
                }
                Some(_) => fieldnum += 1,
            }
        }
        (fieldnum >= 8).then_some(next)
    }
}

// ── normalize_crontab ──────────────────────────────────────────────────────

const DOW_NAMES: [&str; 7] = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"];

/// `_dow_atom`: one Unix weekday, 0=Sunday, or None for syntax not modelled.
fn dow_atom(token: &str) -> Option<i64> {
    let token = token.trim().to_lowercase();
    if let Some(i) = DOW_NAMES.iter().position(|n| *n == token) {
        return Some(i as i64);
    }
    if all_digits(&token) {
        let value: i64 = token.parse().ok()?;
        return (value <= 7).then_some(value % 7);
    }
    None
}

/// `_normalize_dow_field`: a Unix day-of-week field as APScheduler weekday
/// names, or None to pass it through untouched.
fn normalize_dow_field(field: &str) -> Option<String> {
    let mut days = [false; 7];
    for token in field.split(',') {
        let token = token.trim();
        // `if step_raw:` — an empty step is no step.
        let (base, step_raw) = match token.split_once('/') {
            Some((base, step)) if !step.is_empty() => (base, Some(step)),
            Some((base, _)) => (base, None),
            None => (token, None),
        };
        let step = match step_raw {
            Some(raw) => {
                let step: i64 = if all_digits(raw) { raw.parse().ok()? } else { return None };
                if step < 1 {
                    return None;
                }
                step
            }
            None => 1,
        };
        let (lo, hi) = if base == "*" || base == "?" {
            (0, 6)
        } else if base.trim_matches('-').contains('-') {
            let (start, end) = base.split_once('-')?;
            (dow_atom(start)?, dow_atom(end)?)
        } else {
            let single = dow_atom(base)?;
            // A bare `a/n` means `a-6/n`; a bare `a` is just itself.
            (single, if step_raw.is_some() { 6 } else { single })
        };
        let span = (hi - lo).rem_euclid(7);
        for offset in (0..=span).step_by(step as usize) {
            days[((lo + offset) % 7) as usize] = true;
        }
    }
    match days.iter().filter(|&&d| d).count() {
        0 => None,
        7 => Some("*".into()),
        _ => {
            // Monday first, as APScheduler reads it.
            let order = [1, 2, 3, 4, 5, 6, 0];
            Some(order.iter().filter(|&&d| days[d]).map(|&d| DOW_NAMES[d]).collect::<Vec<_>>().join(","))
        }
    }
}

/// `normalize_crontab`: numeric Unix weekdays (0=Sunday) as names, which
/// APScheduler (0=Monday) reads unambiguously. Everything else as given.
pub fn normalize_crontab(expr: &str) -> String {
    let fields: Vec<&str> = expr.split_whitespace().collect();
    if fields.len() != 5 {
        return expr.to_string();
    }
    match normalize_dow_field(fields[4]) {
        Some(dow) => format!("{} {} {} {} {dow}", fields[0], fields[1], fields[2], fields[3]),
        None => expr.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn utc(s: &str) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339(s).unwrap().with_timezone(&Utc)
    }

    fn next(expr: &str, tz: &str, now: &str) -> String {
        let trigger = Trigger::parse(expr, tz.parse().unwrap()).unwrap();
        trigger.next_fire(None, utc(now)).unwrap().isoformat()
    }

    #[test]
    fn numeric_weekdays_are_unix_weekdays() {
        assert_eq!(normalize_crontab("0 9 * * 1-5"), "0 9 * * mon,tue,wed,thu,fri");
        assert_eq!(normalize_crontab("0 9 * * 0"), "0 9 * * sun");
        assert_eq!(normalize_crontab("0 9 * * fri-mon"), "0 9 * * mon,fri,sat,sun");
        assert_eq!(normalize_crontab("0 9 * * */2"), "0 9 * * tue,thu,sat,sun");
        assert_eq!(normalize_crontab("0 9 * * 1#2"), "0 9 * * 1#2");
        // 2026-10-02 is a Friday.
        assert_eq!(next("0 9 * * 1", "UTC", "2026-10-02T12:00:00Z"), "2026-10-05T09:00:00+00:00");
    }

    #[test]
    fn a_gap_time_keeps_its_wall_clock() {
        // 2026-03-08 02:00 doesn't exist in Chicago.
        assert_eq!(next("30 2 * * *", "America/Chicago", "2026-03-08T07:00:00Z"), "2026-03-08T02:30:00-06:00");
    }

    #[test]
    fn invalid_expressions_are_refused() {
        for bad in ["* * * *", "60 * * * *", "*/0 * * * *", "0 0 0 * *", "a * * * *", "0 0 * 13 *", "5-1 * * * *"] {
            assert!(Trigger::parse(bad, Tz::UTC).is_err(), "{bad}");
        }
    }
}
