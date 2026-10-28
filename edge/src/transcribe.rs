//! Speech to text for audio attached in chat (`uploads::prompt`), by the
//! model the `transcription.model` setting names: Meta's
//! `meta:muse-voice-transcribe-*` over `POST /asr/transcribe`, or a Gemini
//! model asked to transcribe.
//!
//! The file is decoded in Rust (symphonia: MP3, AAC/M4A, FLAC, Ogg Vorbis,
//! WAV), mixed to mono and resampled to 16 kHz, then sent as WAV chunks of at
//! most `CHUNK_SECS`, each cut at the quietest moment near its end. Meta takes
//! only 16-bit PCM WAV of at most 10 minutes; Gemini takes it inline. One
//! chunk is held at a time, so a long recording costs no more memory than a
//! short one.

use std::path::{Path, PathBuf};
use std::time::Duration;

use base64::Engine;
use base64::engine::general_purpose::STANDARD;
use serde_json::{Value, json};
use sqlx::SqlitePool;
use symphonia::core::codecs::audio::AudioDecoderOptions;
use symphonia::core::errors::Error as DecodeError;
use symphonia::core::formats::probe::Hint;
use symphonia::core::formats::{FormatOptions, TrackType};
use symphonia::core::io::{MediaSourceStream, MediaSourceStreamOptions};
use symphonia::core::meta::MetadataOptions;
use tokio::sync::mpsc;

use crate::llm::shape::SystemBlock;
use crate::llm::transcript::{Content, Media, Message, Part, Role, Typed};
use crate::llm::{self, Endpoints, Prompt, Request};

pub const SETTING: &str = "transcription.model";
/// What every chunk is resampled to: one of the two rates Meta takes.
const RATE: u32 = 16_000;
/// The longest chunk sent: under Meta's 10 minutes, and as base64 well under
/// Gemini's 20 MB inline request.
const CHUNK_SECS: usize = 5 * 60;
/// How far back from a chunk's end its cut may move to find a pause.
const PAUSE_SEARCH_SECS: usize = 10;
/// A chunk whose loudest sample is below this is silence: not sent.
const SILENCE: i16 = 64;
const RETRIES: u32 = 3;

const GEMINI_SYSTEM: &str = "You transcribe recordings. Write down the speech in the recording verbatim, in the \
language it is spoken in. Reply with the transcript alone: no preamble, timestamps, speaker labels or notes. If \
there is no speech, reply with nothing.";

/// The model the setting names, if it names one.
pub async fn model(pool: &SqlitePool) -> Option<String> {
    crate::catalog::setting(pool, SETTING).await.ok().flatten().map(|m| m.trim().to_string()).filter(|m| !m.is_empty())
}

/// Where a file's transcript is kept, beside it.
pub fn transcript_path(audio: &Path) -> PathBuf {
    let mut name = audio.file_name().unwrap_or_default().to_os_string();
    name.push(".transcript.txt");
    audio.with_file_name(name)
}

/// The speech in the file at `path`, as `model` heard it. A transcript kept
/// from an earlier attempt (a re-claimed run) is read instead.
pub async fn transcribe(http: &reqwest::Client, ends: &Endpoints, model: &str, path: &Path) -> Result<String, String> {
    let kept = transcript_path(path);
    if let Ok(text) = tokio::fs::read_to_string(&kept).await {
        return Ok(text);
    }
    let backend = Backend::of(model, ends)?;
    let (tx, mut rx) = mpsc::channel(1);
    let source = path.to_path_buf();
    let decoder = tokio::task::spawn_blocking(move || decode(&source, &tx));
    let mut pieces = vec![];
    while let Some(chunk) = rx.recv().await {
        let text = backend.send(http, ends, &wav(&chunk)).await?;
        if !text.trim().is_empty() {
            pieces.push(text.trim().to_string());
        }
    }
    decoder.await.map_err(|e| format!("the decoder stopped: {e}"))??;
    let text = pieces.join("\n\n");
    if let Err(e) = tokio::fs::write(&kept, &text).await {
        tracing::warn!("transcript {} not kept: {e}", kept.display());
    }
    Ok(text)
}

enum Backend<'a> {
    Meta { name: &'a str, key: &'a str },
    Gemini { model: &'a str },
}

impl<'a> Backend<'a> {
    fn of(model: &'a str, ends: &'a Endpoints) -> Result<Self, String> {
        match model.split_once(':') {
            Some(("meta", name)) => {
                let key = ends.meta_key.as_deref().ok_or_else(|| format!("META_API_KEY is not set (required for '{model}')"))?;
                Ok(Backend::Meta { name, key })
            }
            Some(("google_genai", _)) => Ok(Backend::Gemini { model }),
            _ => Err(format!("{model:?} can't transcribe — use a meta: transcribe model or a google_genai: Gemini model")),
        }
    }

    async fn send(&self, http: &reqwest::Client, ends: &Endpoints, wav: &[u8]) -> Result<String, String> {
        match self {
            Backend::Meta { name, key } => meta(http, &ends.meta_base, key, name, wav).await,
            Backend::Gemini { model } => gemini(http, ends, model, wav).await,
        }
    }
}

/// `POST {base}/asr/transcribe`: a `request` part of settings and the `audio`
/// part; the reply's `transcript`. 429, 5xx and dropped connections are
/// retried after 1s, 2s, 4s.
async fn meta(http: &reqwest::Client, base: &str, key: &str, name: &str, wav: &[u8]) -> Result<String, String> {
    let settings = json!({"model": name, "audioEncoding": "WAV"}).to_string();
    let mut attempt = 0;
    loop {
        let form = reqwest::multipart::Form::new()
            .part("request", reqwest::multipart::Part::text(settings.clone()).mime_str("application/json").expect("a valid type"))
            .part(
                "audio",
                reqwest::multipart::Part::bytes(wav.to_vec()).file_name("audio.wav").mime_str("audio/wav").expect("a valid type"),
            );
        let sent = http
            .post(format!("{base}/asr/transcribe"))
            .bearer_auth(key)
            .header(reqwest::header::ACCEPT, "application/json")
            .multipart(form)
            .send()
            .await;
        let (transient, why) = match sent {
            Ok(resp) if resp.status().is_success() => {
                let body: Value = resp.json().await.map_err(|e| format!("Meta's transcript isn't JSON: {e}"))?;
                return body["transcript"].as_str().map(str::to_string).ok_or_else(|| format!("no transcript in {body}"));
            }
            Ok(resp) => {
                let status = resp.status().as_u16();
                (status == 429 || status >= 500, format!("{status}: {}", resp.text().await.unwrap_or_default()))
            }
            Err(e) => (true, e.to_string()),
        };
        if !transient || attempt >= RETRIES {
            return Err(why);
        }
        tracing::warn!("meta:{name}: {why} — retry {} of {RETRIES}", attempt + 1);
        tokio::time::sleep(Duration::from_secs(1 << attempt)).await;
        attempt += 1;
    }
}

/// The chunk inline, and the model asked for its speech. `llm::complete`
/// retries what's worth retrying.
async fn gemini(http: &reqwest::Client, ends: &Endpoints, model: &str, wav: &[u8]) -> Result<String, String> {
    let audio = Media {
        mime_type: Some("audio/wav".into()),
        blob: None,
        data: Some(STANDARD.encode(wav)),
        url: None,
        extras: Default::default(),
    };
    let ask = Typed::Text { text: "Transcribe this recording.".into(), signature: None, extras: Default::default() };
    let prompt = Prompt {
        system: vec![SystemBlock { text: GEMINI_SYSTEM.into(), breakpoint: false }],
        messages: vec![Message::new(Role::User, Content::Parts(vec![Part::Typed(ask), Part::Typed(Typed::File(audio))]))],
        history_breakpoint: None,
        cached: false,
    };
    let blobs = Default::default();
    let req = Request { model, prompt: &prompt, tools: &[], blobs: &blobs, effort: None, cache_key: None };
    let reply = llm::complete(http, ends, &req, &mut |_| {}).await.map_err(|e| e.to_string())?;
    Ok(match reply.message.content {
        Content::Text(s) => s,
        Content::Parts(parts) => parts
            .into_iter()
            .filter_map(|p| match p {
                Part::Typed(Typed::Text { text, .. }) => Some(text),
                Part::Str(s) => Some(s),
                _ => None,
            })
            .collect(),
    })
}

/// Decode `path` into 16 kHz mono chunks, each sent on `tx` as it fills.
/// Blocking: runs on its own thread.
fn decode(path: &Path, tx: &mpsc::Sender<Vec<i16>>) -> Result<(), String> {
    let file = std::fs::File::open(path).map_err(|e| format!("can't open it: {e}"))?;
    let stream = MediaSourceStream::new(Box::new(file), MediaSourceStreamOptions::default());
    let mut hint = Hint::new();
    if let Some(ext) = path.extension().and_then(|e| e.to_str()) {
        hint.with_extension(ext);
    }
    let unreadable = |e: DecodeError| format!("not an audio format jarvis can read ({e})");
    let mut format = symphonia::default::get_probe()
        .probe(&hint, stream, FormatOptions::default(), MetadataOptions::default())
        .map_err(unreadable)?;
    let track = format.default_track(TrackType::Audio).ok_or("it has no audio track")?;
    let track_id = track.id;
    let params = track.codec_params.as_ref().and_then(|p| p.audio()).ok_or("its audio codec isn't known")?.clone();
    let mut decoder = symphonia::default::get_codecs()
        .make_audio_decoder(&params, &AudioDecoderOptions::default())
        .map_err(unreadable)?;
    let mut mono: Option<(Resampler, Chunker)> = None;
    let mut frames: Vec<f32> = vec![];
    loop {
        let packet = match format.next_packet() {
            Ok(Some(packet)) => packet,
            Ok(None) => break,
            Err(DecodeError::IoError(e)) if e.kind() == std::io::ErrorKind::UnexpectedEof => break,
            Err(e) => return Err(unreadable(e)),
        };
        if packet.track_id != track_id {
            continue;
        }
        let buf = match decoder.decode(&packet) {
            Ok(buf) => buf,
            // A damaged packet: skip it, as a player would.
            Err(DecodeError::DecodeError(_)) => continue,
            Err(e) => return Err(unreadable(e)),
        };
        let channels = buf.spec().channels().count().max(1);
        let rate = buf.spec().rate();
        frames.clear();
        buf.copy_to_vec_interleaved(&mut frames);
        let (resampler, chunker) = mono.get_or_insert_with(|| (Resampler::new(rate, RATE), Chunker::default()));
        let mixed = frames.chunks(channels).map(|f| f.iter().sum::<f32>() / channels as f32);
        for chunk in chunker.push(&resampler.push(mixed)) {
            if tx.blocking_send(chunk).is_err() {
                return Ok(());
            }
        }
    }
    let Some((mut resampler, mut chunker)) = mono else { return Err("it holds no audio".into()) };
    let tail = resampler.finish();
    for chunk in chunker.push(&tail).into_iter().chain(chunker.finish()) {
        if tx.blocking_send(chunk).is_err() {
            break;
        }
    }
    Ok(())
}

/// A streaming windowed-sinc resampler: low-passed below the lower rate's
/// Nyquist, so going down from 44.1 or 48 kHz doesn't fold the highs into
/// the speech band.
struct Resampler {
    /// Input samples per output sample.
    step: f64,
    /// The filter's cutoff, in cycles per input sample.
    cutoff: f64,
    /// The kernel's half width, in input samples.
    half: usize,
    buf: Vec<f32>,
    /// The input index of `buf[0]`.
    base: usize,
    /// Output samples produced so far.
    produced: u64,
    passthrough: bool,
}

impl Resampler {
    fn new(from: u32, to: u32) -> Self {
        let step = from as f64 / to as f64;
        let cutoff = 0.5 * (1.0 / step).min(1.0) * 0.95;
        let half = (16.0 * step.max(1.0)).ceil() as usize;
        Resampler { step, cutoff, half, buf: vec![], base: 0, produced: 0, passthrough: from == to }
    }

    fn push(&mut self, input: impl Iterator<Item = f32>) -> Vec<i16> {
        if self.passthrough {
            return input.map(to_i16).collect();
        }
        self.buf.extend(input);
        self.drain(false)
    }

    /// What's left, the input taken as silent past its end.
    fn finish(&mut self) -> Vec<i16> {
        if self.passthrough {
            return vec![];
        }
        self.drain(true)
    }

    fn drain(&mut self, last: bool) -> Vec<i16> {
        let mut out = vec![];
        let end = self.base + self.buf.len();
        loop {
            let t = self.produced as f64 * self.step;
            let center = t.floor() as usize;
            if center >= end || (!last && center + self.half + 1 >= end) {
                break;
            }
            let lo = center.saturating_sub(self.half).max(self.base);
            let hi = (center + self.half + 1).min(end);
            let mut acc = 0.0f64;
            for i in lo..hi {
                let x = (i as f64 - t) * 2.0 * self.cutoff;
                let sinc = if x.abs() < 1e-9 { 1.0 } else { (std::f64::consts::PI * x).sin() / (std::f64::consts::PI * x) };
                let w = (i as f64 - t) / (self.half as f64 + 1.0);
                let window = if w.abs() >= 1.0 { 0.0 } else { 0.5 * (1.0 + (std::f64::consts::PI * w).cos()) };
                acc += self.buf[i - self.base] as f64 * 2.0 * self.cutoff * sinc * window;
            }
            out.push(to_i16(acc as f32));
            self.produced += 1;
        }
        // Keep only what the next outputs still reach back to.
        let keep_from = ((self.produced as f64 * self.step).floor() as usize).saturating_sub(self.half);
        if keep_from > self.base {
            let n = (keep_from - self.base).min(self.buf.len());
            self.buf.drain(..n);
            self.base += n;
        }
        out
    }
}

fn to_i16(s: f32) -> i16 {
    (s.clamp(-1.0, 1.0) * i16::MAX as f32).round() as i16
}

/// Gathers samples into chunks of at most `CHUNK_SECS`, each ending at the
/// quietest 20 ms of its last `PAUSE_SEARCH_SECS` so no word is split.
#[derive(Default)]
struct Chunker {
    buf: Vec<i16>,
}

impl Chunker {
    const LIMIT: usize = CHUNK_SECS * RATE as usize;

    fn push(&mut self, samples: &[i16]) -> Vec<Vec<i16>> {
        self.buf.extend_from_slice(samples);
        let mut out = vec![];
        while self.buf.len() >= Self::LIMIT {
            let cut = quietest_cut(&self.buf[..Self::LIMIT]);
            let rest = self.buf.split_off(cut);
            let chunk = std::mem::replace(&mut self.buf, rest);
            if !silent(&chunk) {
                out.push(chunk);
            }
        }
        out
    }

    fn finish(&mut self) -> Option<Vec<i16>> {
        let chunk = std::mem::take(&mut self.buf);
        (!silent(&chunk)).then_some(chunk)
    }
}

fn silent(samples: &[i16]) -> bool {
    samples.iter().all(|s| s.unsigned_abs() < SILENCE as u16)
}

/// Where in `samples` to end the chunk: the middle of its quietest 20 ms
/// frame within the last `PAUSE_SEARCH_SECS`.
fn quietest_cut(samples: &[i16]) -> usize {
    const FRAME: usize = RATE as usize / 50;
    let from = samples.len().saturating_sub(PAUSE_SEARCH_SECS * RATE as usize);
    let energy = |start: usize| samples[start..start + FRAME].iter().map(|&s| (s as i64) * (s as i64)).sum::<i64>();
    (from..samples.len().saturating_sub(FRAME))
        .step_by(FRAME)
        .min_by_key(|&start| energy(start))
        .map_or(samples.len(), |start| start + FRAME / 2)
}

/// 16-bit mono PCM at `RATE` in a RIFF/WAVE container.
fn wav(samples: &[i16]) -> Vec<u8> {
    let data_len = (samples.len() * 2) as u32;
    let mut out = Vec::with_capacity(44 + data_len as usize);
    out.extend_from_slice(b"RIFF");
    out.extend_from_slice(&(36 + data_len).to_le_bytes());
    out.extend_from_slice(b"WAVEfmt ");
    out.extend_from_slice(&16u32.to_le_bytes());
    out.extend_from_slice(&1u16.to_le_bytes()); // PCM
    out.extend_from_slice(&1u16.to_le_bytes()); // mono
    out.extend_from_slice(&RATE.to_le_bytes());
    out.extend_from_slice(&(RATE * 2).to_le_bytes());
    out.extend_from_slice(&2u16.to_le_bytes());
    out.extend_from_slice(&16u16.to_le_bytes());
    out.extend_from_slice(b"data");
    out.extend_from_slice(&data_len.to_le_bytes());
    for s in samples {
        out.extend_from_slice(&s.to_le_bytes());
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tone(rate: u32, secs: f64, hz: f64) -> Vec<f32> {
        (0..(rate as f64 * secs) as usize).map(|i| (0.5 * (2.0 * std::f64::consts::PI * hz * i as f64 / rate as f64).sin()) as f32).collect()
    }

    fn rms(s: &[i16]) -> f64 {
        (s.iter().map(|&x| (x as f64).powi(2)).sum::<f64>() / s.len() as f64).sqrt()
    }

    #[test]
    fn resampling_keeps_the_length_and_the_speech_band() {
        let mut r = Resampler::new(48_000, RATE);
        let input = tone(48_000, 1.0, 440.0);
        // Fed in uneven pieces, as packets come.
        let mut out = vec![];
        for piece in input.chunks(1153) {
            out.extend(r.push(piece.iter().copied()));
        }
        out.extend(r.finish());
        assert!((out.len() as i64 - RATE as i64).abs() <= 1, "{} samples", out.len());
        // A 440 Hz tone at 0.5 keeps its level (rms of 0.5·sin ≈ 0.354).
        let level = rms(&out[1000..15000]) / i16::MAX as f64;
        assert!((level - 0.354).abs() < 0.01, "{level}");
    }

    #[test]
    fn resampling_filters_what_would_fold_into_the_band() {
        // 12 kHz is above the 8 kHz Nyquist of 16 kHz: it must go, not alias to 4 kHz.
        let mut r = Resampler::new(48_000, RATE);
        let mut out = r.push(tone(48_000, 1.0, 12_000.0).into_iter());
        out.extend(r.finish());
        assert!(rms(&out[1000..15000]) / (i16::MAX as f64) < 0.01);
    }

    #[test]
    fn the_same_rate_passes_through() {
        let mut r = Resampler::new(RATE, RATE);
        assert_eq!(r.push([0.0f32, 0.5, -1.0].into_iter()), vec![0, 16384, -32767]);
        assert!(r.finish().is_empty());
    }

    #[test]
    fn chunks_end_at_a_pause_and_silence_is_dropped() {
        let loud = |n: usize| vec![8000i16; n];
        // A pause 3 s before the limit: the cut lands in it.
        let pause_at = Chunker::LIMIT - 3 * RATE as usize;
        let mut samples = loud(pause_at);
        samples.extend(vec![0i16; RATE as usize / 10]);
        samples.extend(loud(Chunker::LIMIT / 2));
        let mut c = Chunker::default();
        let chunks = c.push(&samples);
        assert_eq!(chunks.len(), 1);
        assert!(chunks[0].len() > pause_at && chunks[0].len() < pause_at + RATE as usize / 10);
        assert!(c.finish().is_some());
        let mut quiet = Chunker::default();
        assert!(quiet.push(&vec![3i16; Chunker::LIMIT + 10]).is_empty());
        assert!(quiet.finish().is_none());
    }

    #[test]
    fn wav_is_16_bit_mono_pcm() {
        let w = wav(&[1, -1]);
        assert_eq!(&w[..4], b"RIFF");
        assert_eq!(u32::from_le_bytes(w[4..8].try_into().unwrap()), 40);
        assert_eq!(u16::from_le_bytes(w[22..24].try_into().unwrap()), 1);
        assert_eq!(u32::from_le_bytes(w[24..28].try_into().unwrap()), RATE);
        assert_eq!(u16::from_le_bytes(w[34..36].try_into().unwrap()), 16);
        assert_eq!(&w[44..], &[1, 0, 0xff, 0xff]);
    }

    #[test]
    fn only_meta_and_gemini_transcribe() {
        let ends = Endpoints { meta_key: Some("k".into()), ..Endpoints::from_env() };
        assert!(matches!(Backend::of("meta:muse-voice-transcribe-1.0", &ends), Ok(Backend::Meta { name: "muse-voice-transcribe-1.0", .. })));
        assert!(matches!(Backend::of("google_genai:gemini-3.1-flash-lite", &ends), Ok(Backend::Gemini { .. })));
        assert!(Backend::of("anthropic:claude-haiku-5-5", &ends).is_err());
        let keyless = Endpoints { meta_key: None, ..Endpoints::from_env() };
        assert_eq!(
            Backend::of("meta:m", &keyless).err().unwrap(),
            "META_API_KEY is not set (required for 'meta:m')"
        );
    }

    #[test]
    fn transcripts_are_kept_beside_the_file() {
        assert_eq!(transcript_path(Path::new("/u/c/clip.mp3")), PathBuf::from("/u/c/clip.mp3.transcript.txt"));
    }
}
